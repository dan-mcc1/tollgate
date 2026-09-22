from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The git commit this process was built from. Set by the deploy pipeline, reported by
    # /livez, so a smoke test can tell the new version from an old task still draining.
    app_version: str = "dev"

    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/example_db"
    upstream_base_url: str = "http://localhost:8001"
    # SecretStr prints as '**********', so the provider key can't leak through a log or repr.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.7-flash"
    mock_gemini: bool = True

    # Upstream HTTP client. Each timeout guards a different stage of a request.
    upstream_connect_timeout_s: float = 5.0  # opening the TCP + TLS connection
    upstream_write_timeout_s: float = 10.0  # sending the request body
    upstream_read_timeout_s: float = 60.0  # max silence between response bytes; models are slow
    upstream_pool_timeout_s: float = 5.0  # waiting for a free connection from the pool
    upstream_max_connections: int = 100
    upstream_max_keepalive_connections: int = 20

    # Retries on 429, 5xx and failed connections. Delays use exponential backoff with full
    # jitter: a random wait between 0 and min(max, base * 2**attempt).
    upstream_max_retries: int = 2
    upstream_backoff_base_s: float = 0.25
    upstream_backoff_max_s: float = 4.0

    # Longest a non-streaming request may take in total, including retries. Streaming
    # requests aren't capped: they keep sending, so the read timeout above governs them.
    # Keep this below the load balancer's idle timeout, so the gateway returns a clean
    # error instead of the load balancer cutting the connection.
    request_deadline_s: float = 120.0

    # Telemetry. The endpoint and headers are settings rather than the SDK's own
    # OTEL_* environment variables, for the same reason the provider key is: the
    # headers carry a credential, and a SecretStr cannot be printed by accident.
    otel_enabled: bool = False
    otel_endpoint: str = ""  # OTLP/HTTP base, e.g. https://otlp-gateway-....grafana.net/otlp
    otel_headers: SecretStr = SecretStr("")  # "Authorization=Basic <base64>"
    otel_service_name: str = "tollgate"
    otel_environment: str = "development"
    otel_console: bool = False  # print spans locally instead of shipping them
    # Every request, at this volume. Sampling exists so the knob is there before it is
    # needed, not because anything here is expensive.
    otel_sample_ratio: float = 1.0
    otel_export_interval_ms: int = 15_000
    # GET /metrics, for local development. Off by default and left off in production:
    # the endpoint lists tenant names and their spend, and the load balancer is public.
    # Production ships metrics by OTLP instead, which needs no inbound route at all.
    metrics_endpoint_enabled: bool = False

    # Rate limiting. "memory" keeps a bucket per container, which multiplies a
    # tenant's effective limit by the number of containers; "redis" is the one that is
    # correct on more than one task. Both are kept so the difference can be measured.
    limiter_backend: Literal["memory", "redis"] = "memory"
    redis_url: SecretStr = SecretStr("")
    redis_connect_timeout_s: float = 1.0
    redis_command_timeout_s: float = 0.5
    # Allow the request when Redis can't answer. See the module docstring in limits.py.
    rate_limit_fail_open: bool = True

    # Budgets. A reservation is a lease: this is how long one survives a gateway that
    # dies mid-request, so it must comfortably exceed request_deadline_s.
    budget_lease_s: float = 300.0
    # What to assume a response will run to when the request names no maxOutputTokens.
    # Generous on purpose - it is reconciled to the real figure moments later.
    budget_default_max_output_tokens: int = 8192
    # The month counter outlives its month, so a request on the 1st still finds
    # December's total where it left it rather than reseeding from the ledger.
    budget_month_ttl_s: float = 40 * 24 * 60 * 60

    # Caching. Entries are always scoped to one tenant; there is no setting for sharing
    # them, because there is no safe value for it. See the threat model in the README.
    cache_enabled: bool = True
    cache_ttl_s: float = 24 * 60 * 60
    # The highest temperature a request may name and still be cached. Above it the
    # request bypasses the cache entirely - not looked up, not stored. The default
    # refuses everything sampled, which is the only default that cannot change what a
    # tenant's product does behind their back; raising it is a decision with a number
    # attached, which is why it is a knob rather than a constant.
    cache_max_temperature: float = 0.0
    # What a request that names no temperature will be served at. This is a fact about
    # the upstream, not a preference: Gemini samples at 1.0 when asked for nothing, so
    # assuming 0 here would cache sampled output under a ceiling meant to forbid it.
    cache_assumed_temperature: float = 1.0
    # The most of a streamed response that may be held in order to cache it. A stream is
    # relayed without buffering; this is the bound on the copy kept alongside for the
    # cache, and a response that outgrows it is relayed as normal and simply not stored.
    cache_max_response_bytes: int = 256 * 1024

    # The semantic tier: answering a request from a *similar* one rather than an
    # identical one. Off by default, and deliberately so. A wrong semantic hit is a
    # correctness bug, the threshold that avoids one is a property of the embedding
    # model rather than of this gateway, and no number is safe to ship as a default for
    # a model nobody has measured. bench/cache_sweep.py is how the number below is
    # chosen; turn the tier on once it has been run against the embeddings in use.
    cache_semantic_enabled: bool = False
    # Cosine similarity, not distance: 1.0 is identical. Below this a stored answer would
    # be served for a different question.
    #
    # There is no default, and that is the point. A threshold is a property of the
    # embedding model, not of this gateway, and the two sweeps committed under
    # bench/results/ disagree completely about it: the mock's feature hashing yields
    # 0.93 with no false hits, while gemini-embedding-001 yields *no* safe value at all -
    # "Convert JSON to YAML" and "Convert YAML to JSON" score 0.9913, above every genuine
    # paraphrase in the set. Shipping either number would hand somebody a figure derived
    # from a model they are not using. Turning the tier on therefore requires stating
    # one, and bench/cache_sweep.py is what produces it - or reports that there is none.
    #
    # The same reasoning as monthly_budget_microcents on Tenant: where there is no honest
    # value to invent, the setting is empty and the caller has to decide.
    cache_semantic_threshold: float | None = None
    cache_embedding_model: str = "gemini-embedding-001"
    # 768 rather than the 3072 the model can produce. pgvector indexes up to 2000
    # dimensions, the smaller output is a supported truncation rather than a cut-down
    # model, and it is a quarter of the storage and distance arithmetic per comparison.
    cache_embedding_dimensions: int = 768
    # An embedding call sits between a caller and the answer it is waiting for, and it
    # is spent whether or not anything is found. Short, because a slow one has already
    # cost more than the call it was trying to avoid; a timeout is reported as a miss.
    cache_embedding_timeout_s: float = 2.0
    # How hard pgvector looks before giving up. Higher finds more true neighbours and
    # costs more per lookup; too low silently returns nothing for a filtered search.
    # See the note on filtered vector search in semantic.py.
    cache_hnsw_ef_search: int = 100

    # Detection: inspecting what a tenant's users are sending, on the way in. This is the
    # fleet-wide switch, for turning inspection off everywhere without editing a row per
    # tenant. What happens to a flagged request is not here: it is `tenants.detection_mode`,
    # because refusing a customer's traffic is a per-customer decision rather than a
    # property of this deployment, and the default there is monitor.
    detection_enabled: bool = True
    # Which classifier to run, by name from detect/classifier.py's registry ("tiny",
    # "deberta-base"), or empty for the regex baseline alone. Empty is the default because a
    # checkout has no model file in it: the Dockerfile fetches one and sets this, so the
    # deployed gateway runs the classifier and a test run needs neither the file nor a
    # network. A name that is set but not present on disk refuses to start, rather than
    # quietly serving a regex while the dashboard says otherwise.
    detection_classifier_model: str = ""
    detection_model_dir: str = "models"
    # The cut-off between flagged and clean. None means the value measured for that model
    # and recorded on its registry entry, which is where a threshold belongs: it is a
    # property of the model, as phase 6 learned the hard way about embedding similarity.
    detection_threshold: float | None = None
    # How long inference may take before the request gives up on it. A miss is recorded as a
    # failure to inspect and the request is forwarded: the caller is waiting for a model
    # response, and making them wait longer for a second opinion about their own prompt is
    # the wrong way to spend their patience.
    detection_timeout_ms: float = 250.0
    # ONNX Runtime threads per session. One, because a Fargate task runs dozens of requests
    # on a couple of vCPUs: four threads make one request faster and every other one slower.
    detection_threads: int = 1
    # These models read 512 tokens, and a long prompt is therefore scored in overlapping
    # windows - truncating instead would mean "put the payload at the end" always worked.
    # The cap bounds what an enormous prompt can cost; past it the classifier has not seen
    # everything and the regex baseline, which reads all of it, is what covers the tail.
    detection_max_windows: int = 4
    detection_window_stride_tokens: int = 64
    # Output scanning on a stream, in `block` mode only: how far behind the upstream the relay
    # runs, so that a credential can be found while it is still held rather than after it has
    # been delivered. This number is the length of leak the gateway can still contain, and it
    # is paid for in time to first token - nothing is released until this much has arrived.
    # A kilobyte comfortably exceeds the longest pattern in detect/scanner.py, which is what
    # matters: a private key is kilobytes long but announces itself in its first forty bytes.
    # In `monitor` mode the window is zero, because delaying a stream in order to do nothing
    # about what is found would buy latency and no containment.
    detection_stream_holdback_bytes: int = 1024

    # How long a price inserted by another container can take to come into force here.
    # Prices change a few times a year, so a stale minute costs nothing and saves a
    # database round trip on every single request.
    price_refresh_s: float = 300.0

    # Readiness checks. Keep the timeouts below the load balancer's health check timeout.
    readiness_db_timeout_s: float = 2.0
    readiness_upstream_timeout_s: float = 3.0
    readiness_upstream_cache_s: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
