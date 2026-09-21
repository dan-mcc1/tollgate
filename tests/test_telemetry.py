"""Tracing: the shape of a trace, and what it is allowed to carry.

The shape matters because the point of the trace is to answer "why was that request
slow" without opening a log file, and that answer is the root's duration minus the
upstream span's. If those two are not both present and correctly nested, the dashboard
built on them is measuring something else.
"""

import json
import logging
from collections import defaultdict
from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import ROOT, Keys, make_settings
from tests.test_limits import auth
from tests.test_streaming import queue_script
from tollgate.config import Settings
from tollgate.logs import JsonFormatter
from tollgate.telemetry import outcome_for, tracer

MODEL = "gemini-3.7-flash"
URL = f"/v1beta/models/{MODEL}:generateContent"
STREAM_URL = f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"
BODY = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
Sessionmaker = async_sessionmaker[AsyncSession]


@pytest.fixture
def upstream_transport() -> None:
    """Reach the mock over loopback rather than in process.

    httpx's ASGI transport collects a whole response before returning it, so with it in
    place a stream is not a stream and a test asserting that the upstream span covers
    the whole relay would pass without ever having relayed anything.
    """
    return None


@pytest.fixture
def settings(live_upstream: str) -> Settings:
    return make_settings().model_copy(update={"upstream_base_url": live_upstream})


def by_name(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    return next(span for span in spans.get_finished_spans() if span.name == name)


def root_of(spans: InMemorySpanExporter) -> ReadableSpan:
    """The server span. Found by kind rather than by having no parent, because when a
    caller sends a traceparent this span does have one - a remote one."""
    return next(span for span in spans.get_finished_spans() if span.kind is SpanKind.SERVER)


def number(span: ReadableSpan, key: str) -> float:
    assert span.attributes is not None
    value = span.attributes[key]
    assert isinstance(value, int | float)
    return value


def duration_ms(span: ReadableSpan) -> float:
    assert span.end_time is not None and span.start_time is not None
    return (span.end_time - span.start_time) / 1_000_000


# --- shape ---------------------------------------------------------------------------------


async def test_a_request_is_one_trace_with_a_span_for_each_stage(
    gateway: httpx.AsyncClient, keys: Keys, spans: InMemorySpanExporter
) -> None:
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    finished = spans.get_finished_spans()
    assert {span.name for span in finished} >= {
        "authenticate",
        "rate_limit",
        "upstream",
        "ledger.write",
    }
    # One trace, not several: a stage that started its own would be invisible from the
    # request it belongs to, which is the failure this catches.
    assert len({span.context.trace_id for span in finished if span.context}) == 1
    assert root_of(spans).name == f"POST /v1beta/models/{MODEL}:generateContent"


async def test_upstream_time_is_separable_from_the_gateways_own(
    gateway: httpx.AsyncClient, keys: Keys, spans: InMemorySpanExporter
) -> None:
    """The number this whole phase exists to produce: root minus upstream is overhead."""
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    root, upstream = root_of(spans), by_name(spans, "upstream")

    assert upstream.parent is not None
    assert duration_ms(upstream) <= duration_ms(root)
    assert duration_ms(root) - duration_ms(upstream) >= 0


async def test_a_streamed_upstream_span_covers_the_whole_relay(
    live_gateway: httpx.AsyncClient,
    keys: Keys,
    spans: InMemorySpanExporter,
    live_upstream: str,
) -> None:
    """Not just the moment before the first byte. A span closed when the stream opened
    would report a fast upstream and a gateway that mysteriously took seconds.

    The second event is held back 300 ms, so a span that stopped early cannot reach it.
    """
    await queue_script(live_upstream, [{"text": "first"}, {"delayMs": 300, "text": "second"}])

    async with live_gateway.stream(
        "POST", STREAM_URL, json=BODY, headers=auth(keys.live)
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    upstream = by_name(spans, "upstream")
    assert upstream.attributes is not None
    assert upstream.attributes["tollgate.streamed"] is True
    assert upstream.attributes["http.response.status_code"] == 200
    assert duration_ms(upstream) >= 300


# --- what the attributes carry ------------------------------------------------------------


async def test_the_root_span_carries_the_facts_worth_having(
    gateway: httpx.AsyncClient, keys: Keys, spans: InMemorySpanExporter
) -> None:
    """On the root, so a slow trace answers "which tenant, which model, how many tokens"
    without anyone opening a child span."""
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    attributes = root_of(spans).attributes
    assert attributes is not None
    assert attributes["tollgate.tenant"] == "acme"
    assert attributes["tollgate.model"] == MODEL
    assert attributes["http.response.status_code"] == 200
    assert attributes["url.path"] == f"/v1beta/models/{MODEL}:generateContent"
    assert number(root_of(spans), "tollgate.tokens.output") > 0
    assert number(root_of(spans), "tollgate.cost_microcents") > 0


async def test_a_refusal_is_recorded_without_being_an_error(
    gateway: httpx.AsyncClient, sessionmaker: Sessionmaker, keys: Keys, spans: InMemorySpanExporter
) -> None:
    """A 401 is the caller being told something. Marking it an error would bury the
    traces that are actually the gateway's fault."""
    response = await gateway.post(URL, json=BODY, headers=auth("tg_not_a_real_key"))

    assert response.status_code == 401
    root = root_of(spans)
    assert root.attributes is not None
    assert root.attributes["http.response.status_code"] == 401
    assert root.status.is_ok


# --- correlation ------------------------------------------------------------------------------


def test_log_lines_carry_the_trace_they_were_written_inside() -> None:
    """So a slow request found on a dashboard and the lines explaining it can be put
    side by side, rather than matched up by timestamp and hope."""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)

    with tracer.start_as_current_span("unit") as span:
        inside = JsonFormatter().format(record)
        expected = format(span.get_span_context().trace_id, "032x")
    outside = JsonFormatter().format(record)

    assert f'"trace_id": "{expected}"' in inside
    assert "trace_id" not in outside


async def test_an_inbound_traceparent_continues_the_callers_trace(
    gateway: httpx.AsyncClient, keys: Keys, spans: InMemorySpanExporter
) -> None:
    """A client that is already instrumented should see the gateway inside its own
    request, not as a gap followed by an unrelated trace."""
    caller_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    await gateway.post(
        URL,
        json=BODY,
        headers={
            **auth(keys.live),
            "traceparent": f"00-{caller_trace}-00f067aa0ba902b7-01",
        },
    )

    root = root_of(spans)
    assert root.context is not None and root.parent is not None
    assert format(root.context.trace_id, "032x") == caller_trace
    assert format(root.parent.span_id, "016x") == "00f067aa0ba902b7"


async def test_tracing_off_costs_nothing_and_breaks_nothing(
    gateway: httpx.AsyncClient, keys: Keys
) -> None:
    """With no provider configured every span is a non-recording stub, which is what
    the default settings give a developer who has not set up a backend."""
    with trace.use_span(trace.INVALID_SPAN, end_on_exit=False):
        response = await gateway.post(URL, json=BODY, headers=auth(keys.live))

    assert response.status_code == 200


# --- metrics ---------------------------------------------------------------------------------


def collect(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    """Every data point recorded so far, by instrument name.

    Collected once and handed back as a whole, because reading the reader is itself a
    collection: under delta temporality a second read returns only what arrived since
    the first, so a test that looked up two instruments in turn would always find the
    second one empty.
    """
    recorded: dict[str, list[Any]] = defaultdict(list)
    data = reader.get_metrics_data()
    for resource in getattr(data, "resource_metrics", []):
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                recorded[metric.name].extend(metric.data.data_points)
    return recorded


async def test_every_request_is_counted_once_with_its_outcome(
    gateway: httpx.AsyncClient, keys: Keys, meter: InMemoryMetricReader
) -> None:
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    [point] = collect(meter)["tollgate.requests"]
    assert point.value == 1
    assert point.attributes["outcome"] == "ok"
    assert point.attributes["tenant"] == "acme"
    assert point.attributes["model"] == MODEL


async def test_a_request_refused_before_the_proxy_is_still_counted(
    gateway: httpx.AsyncClient, keys: Keys, meter: InMemoryMetricReader
) -> None:
    """The reason metrics are emitted from the middleware. A 401 never reaches the
    proxy, so a rejection rate built from the proxy would always read zero."""
    await gateway.post(URL, json=BODY, headers=auth("tg_not_a_real_key"))

    [point] = collect(meter)["tollgate.requests"]
    assert point.attributes["outcome"] == "unauthenticated"
    assert point.attributes["tenant"] == "anonymous"


async def test_overhead_is_recorded_separately_from_upstream_time(
    gateway: httpx.AsyncClient, keys: Keys, meter: InMemoryMetricReader
) -> None:
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    recorded = collect(meter)
    [total] = recorded["tollgate.request.duration"]
    [upstream] = recorded["tollgate.upstream.duration"]
    [overhead] = recorded["tollgate.overhead"]

    assert total.sum >= upstream.sum
    assert overhead.sum >= 0
    # The whole point of choosing boundaries: overhead is measured in milliseconds.
    assert overhead.explicit_bounds[0] == 1.0
    assert upstream.explicit_bounds[0] == 50.0


async def test_tokens_and_spend_are_counted_by_kind(
    gateway: httpx.AsyncClient, keys: Keys, meter: InMemoryMetricReader
) -> None:
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    recorded = collect(meter)
    kinds = {point.attributes["kind"]: point.value for point in recorded["tollgate.tokens"]}
    [spend] = recorded["tollgate.spend"]

    assert kinds["input"] > 0 and kinds["output"] > 0
    assert spend.value > 0


async def test_labels_are_never_null(
    gateway: httpx.AsyncClient, keys: Keys, meter: InMemoryMetricReader
) -> None:
    """A column default is applied by the database at INSERT, so `streamed` is still
    None on the record in memory. Passed through it becomes the label "null", which is
    a third value nobody's dashboard query accounts for."""
    await gateway.post(URL, json=BODY, headers=auth(keys.live))

    [point] = collect(meter)["tollgate.requests"]

    assert point.attributes["streamed"] is False
    assert None not in point.attributes.values()


# --- what telemetry is never allowed to carry -----------------------------------------------

# Distinctive enough that finding it anywhere is unambiguous.
CANARY = "zqx-canary-prompt-8f3a1e"
# AWS's own documented example key, as the mock's [[mock:leak]] reply returns it.
LEAKED_SECRET = "AKIAIOSFODNN7EXAMPLE"


def telemetry_text(
    spans: InMemorySpanExporter, meter: InMemoryMetricReader, log_lines: list[str]
) -> str:
    """Everything telemetry would send anywhere, as one searchable blob.

    Spans are serialised whole - name, attributes, events, status, resource - rather
    than attribute by attribute, so a leak into a span event or an error description is
    caught as readily as one into an attribute.
    """
    parts = [span.to_json() for span in spans.get_finished_spans()]
    for name, points in collect(meter).items():
        parts.append(name)
        parts += [json.dumps(dict(point.attributes or {}), default=str) for point in points]
    return "\n".join(parts + log_lines)


@pytest.mark.parametrize(
    ("directive", "description"),
    [
        ("", "an ordinary request"),
        ("[[mock:leak]]", "a response full of fake secrets"),
        ("[[mock:error=400]]", "an upstream error, whose message could quote the prompt"),
        ("[[mock:tokens=200]]", "a long response"),
    ],
)
async def test_no_prompt_or_response_text_reaches_telemetry(
    gateway: httpx.AsyncClient,
    keys: Keys,
    spans: InMemorySpanExporter,
    meter: InMemoryMetricReader,
    log_lines: list[str],
    directive: str,
    description: str,
) -> None:
    """The design decision the README makes, enforced rather than asserted.

    The key is sent in the query string as well as the header, because that is what the
    Gemini SDK permits and it is the leak that auto-instrumentation would have caused.
    """
    body = {"contents": [{"parts": [{"text": f"{CANARY} {directive}"}]}]}

    response = await gateway.post(f"{URL}?key={keys.live}", json=body, headers=auth(keys.live))

    # The canary really was in flight: it went up, and for a 200 it came back.
    assert CANARY in response.request.content.decode()
    haystack = telemetry_text(spans, meter, log_lines)
    assert CANARY not in haystack, f"prompt text leaked from {description}"
    assert keys.live not in haystack, f"the tenant's key leaked from {description}"
    assert LEAKED_SECRET not in haystack, f"response content leaked from {description}"


async def test_no_prompt_text_reaches_telemetry_from_a_stream(
    live_gateway: httpx.AsyncClient,
    keys: Keys,
    spans: InMemorySpanExporter,
    meter: InMemoryMetricReader,
    log_lines: list[str],
) -> None:
    """A streamed response is relayed chunk by chunk and parsed on the way past, which
    is more handling of the content than the unary path does, not less."""
    body = {"contents": [{"parts": [{"text": f"{CANARY} [[mock:leak]]"}]}]}

    async with live_gateway.stream(
        "POST", STREAM_URL, json=body, headers=auth(keys.live)
    ) as response:
        received = b"".join([chunk async for chunk in response.aiter_bytes()])

    assert LEAKED_SECRET in received.decode()  # it really did pass through the gateway
    haystack = telemetry_text(spans, meter, log_lines)
    assert CANARY not in haystack
    assert LEAKED_SECRET not in haystack


async def test_a_key_in_the_query_string_never_reaches_a_span(
    gateway: httpx.AsyncClient, keys: Keys, spans: InMemorySpanExporter
) -> None:
    """The specific reason this instrumentation is written out by hand: the FastAPI
    auto-instrumentation records `url.full` and `http.target`, both of which carry the
    query string, and a tenant is invited to put its key there."""
    await gateway.post(f"{URL}?key={keys.live}", json=BODY, headers=auth(keys.live))

    root = root_of(spans)
    assert root.attributes is not None
    assert "?" not in str(root.attributes["url.path"])
    assert "key=" not in root.name
    assert "url.full" not in root.attributes


def test_logging_config_keeps_third_party_request_logging_quiet() -> None:
    """httpx logs every request at INFO, URL and query string included, and the gateway
    uses httpx to reach the provider. Production is quiet because the root logger sits at
    WARNING, which is true by accident until something pins it - so this pins it."""
    config = json.loads((ROOT / "logging.json").read_text(encoding="utf-8"))

    assert config["root"]["level"] == "WARNING"
    for library in ("httpx", "httpcore"):
        assert config["loggers"][library]["level"] == "WARNING"


@pytest.mark.parametrize("path", ["/livez", "/readyz", "/metrics"])
async def test_the_gateway_watching_itself_is_not_tenant_traffic(
    gateway: httpx.AsyncClient, meter: InMemoryMetricReader, path: str
) -> None:
    """Health checks and the metrics scrape arrive every few seconds and carry no tenant
    or model. Counted, they add an "anonymous"/"none" series to every panel that is the
    gateway watching itself, and at scrape frequency it outruns the real traffic."""
    await gateway.get(path)

    assert collect(meter)["tollgate.requests"] == []


async def test_the_spend_endpoint_is_counted_and_says_what_it_is(
    gateway: httpx.AsyncClient, keys: Keys, meter: InMemoryMetricReader
) -> None:
    """Unlike a scrape, this is a tenant using the service, so it belongs on the panels -
    labelled, rather than arriving as an unexplained method="none"."""
    await gateway.get("/v1/spend", headers=auth(keys.live))

    [point] = collect(meter)["tollgate.requests"]
    assert point.attributes["method"] == "spend"
    assert point.attributes["tenant"] == "acme"


@pytest.mark.parametrize(
    ("status", "source", "expected"),
    [
        (429, None, "rate_limited"),  # our token bucket refused a tenant
        (429, "upstream", "upstream_error"),  # the provider throttled us
        (402, None, "budget_exhausted"),
        (401, None, "unauthenticated"),
        (503, "upstream", "upstream_error"),
        (500, None, "gateway_error"),
    ],
)
def test_a_refusal_is_attributed_to_whoever_made_it(
    status: int, source: str | None, expected: str
) -> None:
    """A 429 from the provider and a 429 from the token bucket share a status code and
    nothing else. Filing both under "rate_limited" says a tenant is over its limit when
    the truth is the upstream is over ours, and the two call for opposite responses."""
    assert outcome_for(status, source) == expected


async def test_an_upstream_rate_limit_is_not_reported_as_ours(
    gateway: httpx.AsyncClient, keys: Keys, meter: InMemoryMetricReader
) -> None:
    """End to end: the tenant's own limit is far from reached, so a 429 arriving on this
    panel could only have come from the provider."""
    body = {"contents": [{"parts": [{"text": "[[mock:error=429]]"}]}]}

    response = await gateway.post(URL, json=body, headers=auth(keys.live))

    assert response.status_code == 429
    [point] = collect(meter)["tollgate.requests"]
    assert point.attributes["outcome"] == "upstream_error"
