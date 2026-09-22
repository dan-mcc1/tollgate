"""Input inspection as the proxy sees it: one call, before the cache.

    verdict = await detection.inspect(mode=tenant.detection_mode, body=body)
    apply_verdict(record, verdict)
    if verdict.blocked:
        raise prompt_blocked()

**Where this sits, and why.** After authentication, the rate limit and the budget
reservation; before the cache lookup. Before the cache, because a request the gateway is
about to refuse has no business being answered from a stored response - and because a
blocked request should not be able to plant an entry for the next caller to hit. After the
budget, because inspection is compute the gateway pays for and a tenant at its cap should
not be able to spend it.

**Three modes, per tenant, monitor by default.** `off` skips inspection entirely, `monitor`
inspects and records and forwards anyway, `block` refuses with a 403. Monitor is the
default because that is how a detector is actually rolled out: it runs against a tenant's
real traffic until somebody has read its false positive rate, and only then is it allowed
to refuse anybody. Shipping `block` as the default would mean the first thing this gateway
ever did to a new customer was reject a request on the strength of an unmeasured number.

**Two tiers, and a request is flagged if either says so.** The baseline runs first, because
it costs microseconds and reads the whole prompt; the classifier runs only when the baseline
found nothing, so an obvious injection never pays for inference. The verdict records which
tier decided, and bench/detection_eval.py reports the tiers separately as well as together -
"flagged if either" is a choice with a cost, since a pair of detectors cannot be more precise
than its worse member, and the table is where that shows up.

The split is not arbitrary. The classifier reads 512 tokens at a time and a bounded number of
windows, so on a very long prompt it has not seen all of it; the baseline has, for the price
of a regex sweep. One tier covers depth, the other covers the tail.

**Detection fails open. Budgets do not.** If inspection raises, the request is recorded
with the verdict `error` and forwarded. That is the opposite of the choice budgets make,
and the difference is what the control protects: a budget protects money the operator will
be billed for, so losing the ability to check it has to stop the request. Detection is
advisory - it protects the tenant's own application from its own users - so a bug in a
regex, or a model that will not load, must not become a total outage for every tenant
running in `block` mode. The verdict `error` exists so that failure is a series on a
dashboard rather than a silence.

**Nothing here sees the prompt except the rules and the model.** The verdict carries a rule
id and a score. No matched text, no excerpt, no truncated prompt: those are enough to explain
a refusal to an operator, and the alternative is prompt content in the ledger, in the span
and in the log line. See the canary test in tests/test_detection.py.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from tollgate.cache.keys import prompt_text, request_payload
from tollgate.cache.replay import parts_of
from tollgate.config import Settings
from tollgate.db.models import UsageRecord
from tollgate.detect import baseline, scanner
from tollgate.detect.classifier import Classifier, build_classifier
from tollgate.errors import GatewayError
from tollgate.telemetry import facts, stage

logger = logging.getLogger("tollgate.detect")

# What a tenant's policy can be. A closed set: `tenants.detection_mode` carries a check
# constraint naming exactly these three.
MODE_OFF = "off"
MODE_MONITOR = "monitor"
MODE_BLOCK = "block"
MODES = frozenset({MODE_OFF, MODE_MONITOR, MODE_BLOCK})
DEFAULT_MODE = MODE_MONITOR

# What inspection concluded. Ledger column values and metric label values, so the set is
# small, closed and named once.
CLEAN = "clean"
FLAGGED = "flagged"
ERROR = "error"  # inspection itself failed; see the module docstring on failing open

# What was then done about it. Recorded rather than derived from the verdict and the
# tenant's mode, because the mode is a column somebody can change this afternoon.
ALLOWED = "allowed"
BLOCKED = "blocked"

# Which tier reached the verdict. `scanner` is the output direction, and it is a tier here
# rather than a separate metric so that "what did inspection cost" has one answer with a
# breakdown, instead of two numbers somebody has to remember to add up.
TIER_BASELINE = "baseline"
TIER_CLASSIFIER = "classifier"
TIER_SCANNER = "scanner"
TIERS = frozenset({TIER_BASELINE, TIER_CLASSIFIER, TIER_SCANNER})

# What was done about a response with findings in it. `blocked` is a response the caller never
# saw at all; `truncated` is a stream that was cut off part way, which is a materially weaker
# outcome and says so. See proxy/streaming.py.
TRUNCATED = "truncated"


@dataclass(frozen=True)
class Verdict:
    """What inspection made of one request.

    `verdict` is None when nothing looked - detection switched off for the fleet, or `off`
    for this tenant - which is different from a request that was inspected and found clean.
    The ledger keeps that distinction, the same way `cache_status` does. `action` is None
    in the same case: nothing was done to a request nobody inspected, and recording it as
    positively allowed would say otherwise.
    """

    verdict: str | None
    tier: str | None = None
    rule: str | None = None
    score: float | None = None
    action: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == BLOCKED

    @property
    def flagged(self) -> bool:
        return self.verdict == FLAGGED


OFF = Verdict(None)


def prompt_blocked() -> GatewayError:
    """403, and deliberately uninformative.

    403 rather than 400: the request is well formed, and the gateway understood it
    perfectly. It is refused by policy, which is what 403 means.

    The body names neither the rule that fired nor the text that matched it. Saying which
    rule fired would hand an attacker an oracle - send a prompt, read the rule, reword
    until nothing fires - and evasion is cheap enough already. The rule id goes to the
    ledger and the span, where the operator can see it and the caller cannot.
    """
    return GatewayError(
        403,
        "prompt_blocked",
        "The request was refused by the gateway's input policy.",
    )


@dataclass(frozen=True)
class OutputVerdict:
    """What the scanner made of one response.

    `findings` are rule ids, sorted, and never the text that matched them - the same rule the
    input direction keeps, for the same reason: a finding is evidence about a response, and
    the response belongs to the customer.
    """

    verdict: str | None
    findings: tuple[str, ...] = ()
    action: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == BLOCKED

    @property
    def flagged(self) -> bool:
        return self.verdict == FLAGGED

    def joined(self) -> str | None:
        return ",".join(self.findings) or None


OUTPUT_OFF = OutputVerdict(None)


def response_blocked() -> GatewayError:
    """403, and a different code from the input direction.

    The same class of refusal - policy, not a malformed request - so the same status. The code
    is what distinguishes them, because "the gateway would not deliver what the model said" and
    "the gateway would not send what you asked" are different problems for whoever is reading
    the error, and only one of them is about their prompt.

    Like the input refusal, it names nothing it found. A caller who could iterate until the
    message changed would have a working oracle for what the scanner looks for.
    """
    return GatewayError(
        403,
        "response_blocked",
        "The gateway withheld the response under its output policy.",
    )


def response_text(payload: dict[str, Any]) -> str:
    """The text a response is delivering, as one string.

    Text parts only. A response's other parts - an inline blob, a function call - are not
    scanned, and that is a stated gap rather than an oversight: these rules are text shapes,
    and a base64 image would match several of them by accident. The gap is in the README.
    """
    return "".join(part["text"] for part in parts_of(payload) if isinstance(part.get("text"), str))


def apply_verdict(record: UsageRecord, verdict: Verdict) -> None:
    """Copy a verdict onto the row that will be written for this request.

    One function rather than five assignments at each call site, so a path that recorded
    the rule but not the action - or the verdict but not the tier - is not something the
    proxies can get subtly wrong in only one of the two of them.
    """
    record.input_verdict = verdict.verdict
    record.input_tier = verdict.tier
    record.input_rule = verdict.rule
    record.input_score = verdict.score
    record.input_action = verdict.action


def apply_output_verdict(record: UsageRecord, verdict: OutputVerdict) -> None:
    """Copy an output verdict onto the row, for the same reason as the input one."""
    record.output_verdict = verdict.verdict
    record.output_findings = verdict.joined()
    record.output_action = verdict.action


class ResponseInspection:
    """Scans one streamed response as it is relayed, and says when the rest must not go.

    The bargain, and it is the interesting part of this phase: a response cannot be inspected
    before it is delivered, because it is delivered as it is generated. So in `block` mode the
    relay runs `holdback_bytes` behind the upstream - bytes are scanned, then released only
    once that much more has arrived behind them - and a finding inside the unreleased region
    stops the stream before the caller ever sees it. The window is therefore exactly the length
    of leak the gateway can still contain, and anything found earlier than that is already
    gone: recorded, alerted on, not undone.

    In `monitor` mode the window is zero. Nothing is delayed, everything is scanned, and a
    finding is a ledger row and a metric rather than an intervention. That is the honest
    version of "monitor", and it is why the two modes are measured separately in the bench:
    one of them costs time to first token and the other cannot stop anything.
    """

    def __init__(self, *, blocking: bool, holdback_bytes: int) -> None:
        self._scanner = scanner.Incremental()
        self._blocking = blocking
        # Zero unless blocking: holding bytes back in order to do nothing about them would
        # buy a slower stream and no containment at all.
        self.holdback_bytes = holdback_bytes if blocking else 0
        self.stopped = False
        self.failed = False

    def feed(self, text: str) -> bool:
        """Scan the next piece of response text. True means: relay no more of it.

        Never raises. A scanner that fell over mid-stream would otherwise take a response the
        caller is already reading with it, and the response is not the thing at fault.
        """
        started = time.perf_counter()
        try:
            fresh = self._scanner.feed(text)
        except Exception:
            logger.exception("output scanning failed")
            self.failed = True
            return False
        finally:
            facts().detect_ms[TIER_SCANNER] = (
                facts().detect_ms.get(TIER_SCANNER, 0.0) + (time.perf_counter() - started) * 1000
            )
        if fresh:
            logger.info(
                "output findings",
                extra={"fields": {"findings": fresh, "blocking": self._blocking}},
            )
        if fresh and self._blocking:
            self.stopped = True
        return self.stopped

    @property
    def findings(self) -> list[str]:
        return self._scanner.findings

    def verdict(self) -> OutputVerdict:
        findings = tuple(self._scanner.findings)
        if self.failed and not findings:
            return OutputVerdict(ERROR, action=ALLOWED)
        if not findings:
            return OutputVerdict(CLEAN, action=ALLOWED)
        return OutputVerdict(FLAGGED, findings, TRUNCATED if self.stopped else ALLOWED)


class DetectionService:
    """Inspects what a tenant is sending, according to that tenant's policy."""

    def __init__(
        self,
        *,
        enabled: bool,
        classifier: Classifier | None = None,
        holdback_bytes: int = 1024,
    ) -> None:
        self.enabled = enabled
        self._classifier = classifier
        self._holdback_bytes = holdback_bytes

    @property
    def classifier_name(self) -> str | None:
        """Which model is loaded, or None when the baseline is all there is.

        Written to the startup log, and deliberately not to /livez: that endpoint is public
        through the load balancer, and naming the detector is the same mistake as naming the
        rule in a refusal - it tells whoever is probing which model to craft against.
        """
        return self._classifier.name if self._classifier is not None else None

    async def inspect(self, *, mode: str, body: bytes) -> Verdict:
        """Inspect one request body. Never raises; see the module docstring."""
        if not self.enabled or mode == MODE_OFF:
            return OFF

        started = time.perf_counter()
        with stage("detect.input") as span:
            try:
                verdict = await self._inspect(mode, body)
            except Exception:
                # Fail open, loudly. The request is forwarded, the row says `error`, and
                # the exception is in the log with a stack trace - which is more than the
                # alternative would leave behind.
                logger.exception("input inspection failed")
                verdict = Verdict(ERROR, tier=TIER_BASELINE, action=ALLOWED)
            span.set_attribute("tollgate.detect.verdict", verdict.verdict or "")
            span.set_attribute("tollgate.detect.action", verdict.action or "")
            if verdict.rule is not None:
                span.set_attribute("tollgate.detect.rule", verdict.rule)
            if verdict.score is not None:
                span.set_attribute("tollgate.detect.score", round(verdict.score, 5))

        # Charged to the tier that reached the verdict, and left inside the gateway's own
        # overhead rather than netted off it like the embedding call in cache/service.py.
        # That call is a round trip to the provider; this is work this service chose to do,
        # and hiding it would be hiding the cost of the feature.
        tier = verdict.tier or TIER_BASELINE
        spent_ms = (time.perf_counter() - started) * 1000
        facts().detect_ms[tier] = facts().detect_ms.get(tier, 0.0) + spent_ms
        return verdict

    async def _inspect(self, mode: str, body: bytes) -> Verdict:
        payload = request_payload(body)
        # The same text the semantic cache embeds: the whole conversation, roles written
        # in, non-text parts left out. One definition of "the prompt" for both features,
        # so a request that is inspected and a request that is cached are agreed about
        # what was asked.
        text = prompt_text(payload) if payload is not None else ""
        if not text.strip():
            # Nothing to inspect is not the same as nothing found, and it is recorded as
            # clean anyway. A fourth verdict would have to be learned by every panel and
            # every query, to describe a request the upstream is about to reject on its
            # own - an unparseable body, or a conversation made only of images.
            return Verdict(CLEAN, tier=TIER_BASELINE, action=ALLOWED)

        rule = baseline.scan(text)
        if rule is not None:
            return self._flagged(mode, TIER_BASELINE, rule=rule)
        if self._classifier is None:
            return Verdict(CLEAN, tier=TIER_BASELINE, action=ALLOWED)
        return await self._classify(mode, text)

    async def _classify(self, mode: str, text: str) -> Verdict:
        """The second tier, once the baseline has found nothing.

        The folded text is what the model sees, for the same reason the rules do: a prompt
        padded with zero-width characters tokenizes into something no training set contains,
        and folding costs one pass over a string that has already been built.
        """
        classifier = self._classifier
        assert classifier is not None  # only reached with one loaded
        score = await classifier.score(baseline.fold(text))
        if score is None:
            # Inference failed outright. The baseline's silence is not the answer this
            # tenant's policy asked for, so this is recorded as a failure to inspect rather
            # than as a clean request - and the request is forwarded, because detection
            # fails open.
            return Verdict(ERROR, tier=TIER_CLASSIFIER, action=ALLOWED)
        if score.value >= classifier.threshold:
            return self._flagged(mode, TIER_CLASSIFIER, score=score.value)
        if not score.complete:
            # Clean, but only across what the model got to read: a prompt longer than the
            # window cap, or than the time budget paid for. The rest was covered by the
            # baseline alone, and this line is how a tenant whose traffic is all very long
            # prompts gets found rather than assumed.
            logger.info(
                "classifier read part of the prompt",
                extra={"fields": {"windows": score.windows, "model": classifier.name}},
            )
        return Verdict(CLEAN, tier=TIER_CLASSIFIER, score=score.value, action=ALLOWED)

    # --- the other direction ---------------------------------------------------------------

    def scan_response(self, *, mode: str, payload: dict[str, Any] | None) -> OutputVerdict:
        """Inspect a complete response before any of it is delivered. Never raises.

        The easy direction: a whole body is in hand, so a finding means the caller gets a
        refusal instead of the response rather than a truncated one. Used for the unary path
        and for a cached answer replayed as a stream, which is also whole before it starts.

        A cache hit is scanned again rather than trusted. The entry was scanned when it was
        stored, but under whatever policy was in force that day, and the answer is being
        delivered now.
        """
        if not self.enabled or mode == MODE_OFF:
            return OUTPUT_OFF
        started = time.perf_counter()
        try:
            findings = tuple(scanner.findings_in(response_text(payload or {})))
        except Exception:
            logger.exception("output scanning failed")
            return OutputVerdict(ERROR, action=ALLOWED)
        finally:
            facts().detect_ms[TIER_SCANNER] = (
                facts().detect_ms.get(TIER_SCANNER, 0.0) + (time.perf_counter() - started) * 1000
            )
        if not findings:
            return OutputVerdict(CLEAN, action=ALLOWED)
        action = BLOCKED if mode == MODE_BLOCK else ALLOWED
        logger.info(
            "output findings", extra={"fields": {"findings": list(findings), "action": action}}
        )
        return OutputVerdict(FLAGGED, findings, action)

    def response_stream(self, *, mode: str) -> ResponseInspection | None:
        """An inspection for one streamed response, or None when nothing is watching."""
        if not self.enabled or mode == MODE_OFF:
            return None
        return ResponseInspection(blocking=mode == MODE_BLOCK, holdback_bytes=self._holdback_bytes)

    def _flagged(
        self, mode: str, tier: str, *, rule: str | None = None, score: float | None = None
    ) -> Verdict:
        # Only `block` blocks. An unrecognised mode - a column widened later, a value
        # written by hand - is therefore monitored rather than either enforced or ignored,
        # which is the safe way round for a setting nobody in this process understands.
        action = BLOCKED if mode == MODE_BLOCK else ALLOWED
        logger.info(
            "input flagged",
            extra={"fields": {"rule": rule, "tier": tier, "action": action, "score": score}},
        )
        return Verdict(FLAGGED, tier=tier, rule=rule, score=score, action=action)


def build_detection(settings: Settings) -> DetectionService:
    """The detector this process will use.

    `detection_enabled` is the fleet-wide switch, for turning inspection off everywhere
    without editing every tenant's row. What happens to a flagged request is the tenant's
    policy and lives in the database, because it is a per-customer decision rather than a
    property of this deployment.

    Loading the classifier - reading a graph off disk and running one inference to warm it -
    happens here, at startup, and raises rather than degrades. A process that was told to run
    a classifier and could not find one must not come up serving a regex while every panel
    reports what it believes it is running.
    """
    detection = DetectionService(
        enabled=settings.detection_enabled,
        classifier=build_classifier(
            model=settings.detection_classifier_model,
            model_dir=settings.detection_model_dir,
            threshold=settings.detection_threshold,
            timeout_ms=settings.detection_timeout_ms,
            threads=settings.detection_threads,
            max_windows=settings.detection_max_windows,
            stride_tokens=settings.detection_window_stride_tokens,
        ),
        holdback_bytes=settings.detection_stream_holdback_bytes,
    )
    logger.info(
        "detection ready",
        extra={
            "fields": {
                "enabled": detection.enabled,
                "classifier": detection.classifier_name or "none",
            }
        },
    )
    return detection
