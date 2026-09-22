from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Set by the deploy pipeline and reported by /livez, so a smoke test can tell the new
    # version from an old task still draining.
    app_version: str = "dev"

    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/example_db"
    upstream_base_url: str = "http://localhost:8001"
    # SecretStr prints as '**********', so the key cannot leak through a log or a repr.
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

    # Retries on 429, 5xx and failed connections, with full jitter: a random wait between
    # 0 and min(max, base * 2**attempt).
    upstream_max_retries: int = 2
    upstream_backoff_base_s: float = 0.25
    upstream_backoff_max_s: float = 4.0

    # Longest a non-streaming request may take in total, retries included. Keep it below the
    # load balancer's idle timeout so the gateway returns the error rather than the balancer
    # cutting the connection. Streaming requests keep sending, so the read timeout governs them.
    request_deadline_s: float = 120.0

    # Settings rather than the SDK's own OTEL_* variables, because the headers carry a
    # credential and a SecretStr cannot be printed by accident.
    otel_enabled: bool = False
    otel_endpoint: str = ""  # OTLP/HTTP base, e.g. https://otlp-gateway-....grafana.net/otlp
    otel_headers: SecretStr = SecretStr("")  # "Authorization=Basic <base64>"
    otel_service_name: str = "tollgate"
    otel_environment: str = "development"
    otel_console: bool = False  # print spans locally instead of shipping them
    otel_sample_ratio: float = 1.0
    otel_export_interval_ms: int = 15_000
    # GET /metrics lists tenant names and their spend, and the load balancer is public, so
    # this stays off in production. Production ships metrics by OTLP, which needs no route in.
    metrics_endpoint_enabled: bool = False

    # "memory" keeps a bucket per container, which multiplies a tenant's effective limit by
    # the container count; "redis" is correct on more than one task. Both exist so the
    # difference can be measured.
    limiter_backend: Literal["memory", "redis"] = "memory"
    redis_url: SecretStr = SecretStr("")
    redis_connect_timeout_s: float = 1.0
    redis_command_timeout_s: float = 0.5
    # Allow the request when Redis cannot answer. See the module docstring in limits.py.
    rate_limit_fail_open: bool = True

    # A reservation is a lease. This is how long one survives a gateway that dies
    # mid-request, so it must comfortably exceed request_deadline_s.
    budget_lease_s: float = 300.0
    # Assumed response length when the request names no maxOutputTokens. Generous, because
    # it is reconciled to the real figure moments later.
    budget_default_max_output_tokens: int = 8192
    # The month counter outlives its month, so a request on the 1st finds December's total
    # where it left it rather than reseeding from the ledger.
    budget_month_ttl_s: float = 40 * 24 * 60 * 60

    # Entries are always scoped to one tenant; there is no setting for sharing them.
    # See docs/threat-model.md.
    cache_enabled: bool = True
    cache_ttl_s: float = 24 * 60 * 60
    # The highest temperature a request may name and still be cached. Above it the request
    # bypasses the cache entirely - not looked up, not stored. The default refuses everything
    # sampled, which is the only default that cannot change a tenant's product behind them.
    cache_max_temperature: float = 0.0
    # What a request naming no temperature is served at. A fact about the upstream rather than
    # a preference: Gemini samples at 1.0 when asked for nothing, so assuming 0 here would
    # cache sampled output under a ceiling meant to forbid it.
    cache_assumed_temperature: float = 1.0
    # The bound on the copy kept alongside a relayed stream in order to cache it. A response
    # that outgrows it is relayed as normal and simply not stored.
    cache_max_response_bytes: int = 256 * 1024

    # Answering a request from a *similar* one rather than an identical one. A wrong semantic
    # hit is a correctness bug, and the threshold that avoids one is a property of the
    # embedding model rather than of this gateway, so the tier ships off.
    cache_semantic_enabled: bool = False
    # Cosine similarity, not distance: 1.0 is identical. Below this a stored answer would be
    # served for a different question.
    #
    # There is no default, and that is the point. The two sweeps under bench/results/ disagree
    # completely: the mock's feature hashing yields 0.93 with no false hits, while
    # gemini-embedding-001 yields no safe value at all - "Convert JSON to YAML" and "Convert
    # YAML to JSON" score 0.9913, above every genuine paraphrase in the set. Enabling the tier
    # therefore requires stating one, and bench/cache_sweep.py is what produces it, or reports
    # that there is none.
    cache_semantic_threshold: float | None = None
    cache_embedding_model: str = "gemini-embedding-001"
    # 768 rather than the 3072 the model can produce: pgvector indexes up to 2000 dimensions,
    # this is a supported truncation rather than a cut-down model, and it is a quarter of the
    # storage and distance arithmetic per comparison.
    cache_embedding_dimensions: int = 768
    # An embedding call sits between a caller and the answer they are waiting for, and is
    # spent whether or not anything is found. A timeout is reported as a miss.
    cache_embedding_timeout_s: float = 2.0
    # How hard pgvector looks before giving up. Too low silently returns nothing for a
    # filtered search; see the note on filtered vector search in semantic.py.
    cache_hnsw_ef_search: int = 100

    # The fleet-wide switch for inspecting input. What happens to a flagged request is
    # `tenants.detection_mode` instead, because refusing a customer's traffic is a
    # per-customer decision rather than a property of this deployment.
    detection_enabled: bool = True
    # A classifier name from detect/classifier.py's registry ("tiny", "deberta-base"), or empty
    # for the regex baseline alone. Empty by default because a checkout has no model file in
    # it; the Dockerfile fetches one and sets this. A name set but not present on disk refuses
    # to start, rather than serving a regex while the dashboard says otherwise.
    detection_classifier_model: str = ""
    detection_model_dir: str = "models"
    # The cut-off between flagged and clean. None means the value measured for that model and
    # recorded on its registry entry, which is where a threshold belongs.
    detection_threshold: float | None = None
    # How long inference may take before the request gives up on it. A miss is recorded as a
    # failure to inspect and the request is forwarded.
    detection_timeout_ms: float = 250.0
    # ONNX Runtime threads per session. One, because a Fargate task runs dozens of requests on
    # a couple of vCPUs: four threads make one request faster and every other one slower.
    detection_threads: int = 1
    # These models read 512 tokens, so a long prompt is scored in overlapping windows;
    # truncating would mean "put the payload at the end" always worked. The cap bounds what an
    # enormous prompt can cost, and past it the regex baseline covers the tail.
    detection_max_windows: int = 4
    detection_window_stride_tokens: int = 64
    # Output scanning on a stream, in `block` mode only: how far behind the upstream the relay
    # runs, so a credential can be found while it is still held. This is the length of leak the
    # gateway can contain, paid for in time to first token. A kilobyte exceeds the longest
    # pattern in detect/scanner.py, which is what matters: a private key is kilobytes long but
    # announces itself in its first forty bytes. In `monitor` mode the window is zero.
    detection_stream_holdback_bytes: int = 1024

    # How long a price inserted by another container takes to come into force here. Prices
    # change a few times a year, so a stale minute saves a round trip on every request.
    price_refresh_s: float = 300.0

    # Keep these below the load balancer's health check timeout.
    readiness_db_timeout_s: float = 2.0
    readiness_upstream_timeout_s: float = 3.0
    readiness_upstream_cache_s: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
