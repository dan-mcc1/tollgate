"""OpenTelemetry wiring: one trace per request, exported over OTLP.

**Why the instrumentation is written out rather than installed.** The obvious move is
`opentelemetry-instrumentation-fastapi`, which produces a server span for free. It also
records `url.full` and `http.target`, both of which carry the query string - and a tenant
is invited to send its key as `?key=`, because that is what the Gemini SDK does. Auto
instrumentation would therefore write a live credential into every trace, and traces go
to a third party. logs.py already refuses to log query strings for exactly this reason;
this module holds the same line. The spans below record `url.path` and never `url.full`.

**What a trace looks like.** One span per request, started in the middleware so it covers
everything, with a child for each stage that can be slow or can refuse:

    POST /v1beta/models/{model}:{method}
      authenticate          hashed key to tenant, one query
      rate_limit            token bucket, memory or Redis
      budget.reserve        ledger read or Redis script
      upstream              the provider call, retries included
      ledger.write          the usage row, priced
      budget.settle         reservation released

The one that matters is `upstream`: subtract it from the root and what is left is the
gateway's own overhead, which is the number this project is judged on and the number the
phase 5 alert watches.

**Attributes are metadata only.** Tenant, model, method, token counts, cost, outcome.
Never a prompt, never a response, never a header, never a query string. tests/
test_telemetry.py fails the build if any of those appear.

**Metrics come out of one place.** Everything a request learns is collected on a
`RequestFacts` held in a context variable, and the middleware emits the whole set once,
at the end. A refusal never reaches the proxy, so metrics emitted from the proxy would
silently omit every 401, 429 and 402 - which are exactly the requests a rejection rate is
meant to count. One emission point makes that impossible.

**Bucket boundaries are chosen, not inherited.** The default histogram buckets step
0, 5, 10, 25, 50 ms, which is fine for a web request and useless here: the gateway's
overhead was measured at 10.4 ms median and 14.1 ms at the 99th, so a p99 estimate would
be "somewhere between 10 and 25" and the alert built on it would be measuring the bucket
edge rather than the service. Overhead therefore gets millisecond-scale boundaries, while
the upstream call - which runs from 100 ms to a minute - gets ones spread over seconds.
A percentile is only ever as precise as the bucket it lands in.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from tollgate.config import Settings

logger = logging.getLogger("tollgate.telemetry")

SCOPE = "tollgate"
tracer = trace.get_tracer(SCOPE)
meter = metrics.get_meter(SCOPE)

# What the gateway itself adds, in milliseconds. Single-digit territory, so the
# boundaries are too: a p99 that lands between 10 and 25 tells nobody anything.
OVERHEAD_BUCKETS_MS = [1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 50.0, 100.0, 250.0]
# A model call. Hundreds of milliseconds to a minute, so seconds-scale boundaries.
UPSTREAM_BUCKETS_MS = [
    50.0,
    100.0,
    250.0,
    500.0,
    1_000.0,
    2_500.0,
    5_000.0,
    10_000.0,
    20_000.0,
    30_000.0,
    60_000.0,
]

# Named once. The views below and the dashboard's queries both key on these, and a
# literal repeated in three places is a rename that half works.
REQUESTS = "tollgate.requests"
REQUEST_DURATION = "tollgate.request.duration"
UPSTREAM_DURATION = "tollgate.upstream.duration"
OVERHEAD = "tollgate.overhead"
TOKENS = "tollgate.tokens"
SPEND = "tollgate.spend"
UPSTREAM_ATTEMPTS = "tollgate.upstream.attempts"

INSTRUMENT_NAMES = frozenset(
    {REQUESTS, REQUEST_DURATION, UPSTREAM_DURATION, OVERHEAD, TOKENS, SPEND, UPSTREAM_ATTEMPTS}
)

requests_total = meter.create_counter(
    REQUESTS, unit="1", description="Requests by tenant, model and outcome."
)
request_duration = meter.create_histogram(
    REQUEST_DURATION, unit="ms", description="Total time, as the caller saw it."
)
upstream_duration = meter.create_histogram(
    UPSTREAM_DURATION, unit="ms", description="Time spent waiting on the provider."
)
overhead_duration = meter.create_histogram(
    OVERHEAD, unit="ms", description="Total minus upstream: the gateway's own cost."
)
tokens_total = meter.create_counter(
    TOKENS, unit="1", description="Tokens by tenant, model and kind."
)
spend_total = meter.create_counter(
    SPEND, unit="1", description="Spend in micro-cents, by tenant and model."
)
upstream_attempts = meter.create_counter(
    UPSTREAM_ATTEMPTS, unit="1", description="Upstream sends, retries included."
)


@dataclass
class Telemetry:
    """Whatever was started, so lifespan can shut it down in one place."""

    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None

    def shutdown(self) -> None:
        # Both flush what they are still holding. Without this a task stopped by ECS
        # loses the telemetry for the requests that were in flight when it was told to
        # stop, which are the ones most worth having.
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown()


def parse_headers(raw: str) -> dict[str, str]:
    """`key=value,key2=value2`, the format the OTLP specification uses."""
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {key.strip(): value.strip() for key, value in pairs}


def span_processors(settings: Settings) -> list[SpanProcessor]:
    processors: list[SpanProcessor] = []
    endpoint = settings.otel_endpoint
    if endpoint:
        processors.append(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{endpoint.rstrip('/')}/v1/traces",
                    headers=parse_headers(settings.otel_headers.get_secret_value()),
                )
            )
        )
    if settings.otel_console:
        processors.append(BatchSpanProcessor(ConsoleSpanExporter()))
    return processors


def metric_readers(settings: Settings) -> list[MetricReader]:
    """Push to the backend, and optionally serve a scrape endpoint as well.

    Push, because a Fargate task has no stable address and is reachable only through
    the load balancer: a Prometheus server would need service discovery to find one,
    and would lose whatever a task recorded between the last scrape and its shutdown.
    The scrape endpoint is a development convenience, off by default; see main.py for
    why it stays off in production.
    """
    readers: list[MetricReader] = []
    if settings.otel_endpoint:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/metrics",
                    headers=parse_headers(settings.otel_headers.get_secret_value()),
                ),
                export_interval_millis=settings.otel_export_interval_ms,
            )
        )
    if settings.metrics_endpoint_enabled:
        readers.append(PrometheusMetricReader())
    return readers


def histogram_views() -> list[View]:
    """Bucket boundaries, chosen per instrument. See the module docstring."""
    return [
        View(
            instrument_name=OVERHEAD,
            aggregation=ExplicitBucketHistogramAggregation(OVERHEAD_BUCKETS_MS),
        ),
        View(
            instrument_name=UPSTREAM_DURATION,
            aggregation=ExplicitBucketHistogramAggregation(UPSTREAM_BUCKETS_MS),
        ),
        View(
            instrument_name=REQUEST_DURATION,
            aggregation=ExplicitBucketHistogramAggregation(UPSTREAM_BUCKETS_MS),
        ),
    ]


def setup_telemetry(settings: Settings) -> Telemetry:
    """Install the global tracer and meter providers. A no-op when telemetry is switched
    off, in which case every span becomes a cheap non-recording stub and every
    instrument a no-op."""
    if not settings.otel_enabled:
        return Telemetry()

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.app_version,
            "deployment.environment.name": settings.otel_environment,
        }
    )

    tracer_provider = TracerProvider(
        resource=resource,
        # ParentBased, so a sampled request stays sampled for its whole life rather
        # than being decided again at every span and arriving as a broken trace.
        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
    )
    for processor in span_processors(settings):
        tracer_provider.add_span_processor(processor)
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource, metric_readers=metric_readers(settings), views=histogram_views()
    )
    metrics.set_meter_provider(meter_provider)

    logger.info(
        "telemetry started",
        extra={
            "fields": {
                "endpoint": settings.otel_endpoint or "none",
                "sample_ratio": settings.otel_sample_ratio,
                "metrics_endpoint": settings.metrics_endpoint_enabled,
            }
        },
    )
    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)


# --- using it ---------------------------------------------------------------------------


@contextmanager
def stage(name: str, **attributes: Any) -> Iterator[Span]:
    """A child span for one stage of the request."""
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span


def annotate(**attributes: Any) -> None:
    """Add attributes to whichever span is current, ignoring the ones not known yet.

    Most of what is worth recording - token counts, cost, the outcome - is only learned
    after the upstream has answered, by which point the code that knows it is a long way
    from the code that opened the span.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def record_usage(record: Any) -> None:
    """Copy a finished usage row onto the root span and into this request's metrics.

    One function rather than two calls, so a path that records the trace but not the
    metric - or the other way round - is not something a call site can get wrong.
    """
    annotate(
        **{
            "tollgate.model": record.model,
            "tollgate.method": record.method,
            "tollgate.streamed": bool(record.streamed),
            "tollgate.tokens.input": record.input_tokens,
            "tollgate.tokens.output": record.output_tokens,
            "tollgate.tokens.thoughts": record.thoughts_tokens,
            "tollgate.cost_microcents": record.cost_microcents,
            "tollgate.upstream.attempts": record.upstream_attempts,
            "tollgate.upstream.latency_ms": record.upstream_latency_ms,
            "tollgate.upstream.ttfb_ms": record.upstream_ttfb_ms,
            "tollgate.error.source": record.error_source,
            "tollgate.error.code": record.error_code,
            "tollgate.client_disconnected": bool(record.client_disconnected),
        }
    )
    current = facts()
    current.model = record.model
    current.method = record.method
    # bool(), not the raw value: a column default is applied by the database at
    # INSERT, so this attribute is still None on an unflushed row and would reach
    # the metric as the label "null" rather than "false".
    current.streamed = bool(record.streamed)
    current.attempts = record.upstream_attempts
    current.upstream_ms = record.upstream_latency_ms
    current.cost_microcents = record.cost_microcents or 0
    current.outcome = outcome_for(record.status_code, record.error_source)
    for kind, count in (
        ("input", record.input_tokens),
        ("output", record.output_tokens),
        ("thoughts", record.thoughts_tokens),
    ):
        if count:
            current.tokens[kind] = count


def record_failure(span: Span, source: str, code: str) -> None:
    """Mark a span as failed. The message is a code, never a provider message, which
    could quote the prompt back."""
    span.set_attribute("tollgate.error.source", source)
    span.set_attribute("tollgate.error.code", code)
    span.set_status(Status(StatusCode.ERROR, code))


def current_ids() -> tuple[str, str] | None:
    """The trace and span this log line belongs to, as the hex strings every backend
    expects, or None when nothing is being recorded."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


def header_carrier(headers: Sequence[tuple[bytes, bytes]]) -> dict[str, str]:
    """Raw ASGI headers as the propagator wants them, so an inbound `traceparent`
    continues the caller's trace instead of starting a second one beside it."""
    return {name.decode("latin-1"): value.decode("latin-1") for name, value in headers}


# --- what a request turned out to be -------------------------------------------------------


@dataclass
class RequestFacts:
    """Collected as the request goes, emitted once by the middleware at the end.

    Held in a context variable and mutated in place rather than rebound, so the shielded
    task that finishes an abandoned stream updates the same object the middleware will
    read. If that task lands after the response has closed the metrics miss its token
    counts; the ledger row is still written and is still correct, which is the copy that
    has to be.
    """

    outcome: str | None = None
    model: str | None = None
    method: str | None = None
    streamed: bool = False
    upstream_ms: float | None = None
    attempts: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    cost_microcents: int = 0


facts_var: ContextVar[RequestFacts | None] = ContextVar("request_facts", default=None)


def facts() -> RequestFacts:
    """This request's facts, creating them if the middleware has not yet."""
    current = facts_var.get()
    if current is None:
        current = RequestFacts()
        facts_var.set(current)
    return current


def outcome_for(status_code: int, error_source: str | None) -> str:
    """One label, low cardinality, covering every way a request can end.

    Derived from the status rather than from a string each call site invents, so a new
    refusal cannot quietly add a new value that no dashboard panel knows about.
    """
    if status_code < 400:
        return "upstream_error_in_stream" if error_source == "upstream" else "ok"
    if error_source == "upstream":
        # Whose refusal it was decides this, before what the status code says. The
        # provider returns 429 when it is throttling the gateway, and the gateway
        # returns 429 when its token bucket is throttling a tenant. Reading the status
        # first files both under "rate_limited", which are opposite problems: one is the
        # limiter working as designed, the other is a capacity problem upstream that no
        # amount of tenant configuration will fix. The exact code is on the ledger row.
        return "upstream_error"
    return {
        401: "unauthenticated",
        402: "budget_exhausted",
        429: "rate_limited",
        501: "unsupported",
    }.get(status_code, "gateway_error")


def emit_request_metrics(tenant: str | None, status_code: int, total_ms: float) -> None:
    """Every request, whatever happened to it, exactly once."""
    current = facts_var.get() or RequestFacts()
    labels: dict[str, AttributeValue] = {
        "tenant": tenant or "anonymous",
        "model": current.model or "none",
        "method": current.method or "none",
        "outcome": current.outcome or outcome_for(status_code, None),
        "streamed": current.streamed,
    }
    requests_total.add(1, labels)
    request_duration.record(total_ms, labels)

    if current.upstream_ms is not None:
        upstream_duration.record(current.upstream_ms, labels)
        # Clamped at zero: the two are measured by different clocks at different
        # points, and a negative overhead would poison the histogram it feeds.
        overhead_duration.record(max(0.0, total_ms - current.upstream_ms), labels)
    if current.attempts:
        upstream_attempts.add(current.attempts, labels)
    for kind, count in current.tokens.items():
        tokens_total.add(count, {**labels, "kind": kind})
    if current.cost_microcents:
        spend_total.add(current.cost_microcents, labels)
