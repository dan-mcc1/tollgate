"""The classifier tier: a published model, run locally through ONNX Runtime.

Nothing here is trained. A published sequence classifier is downloaded at build time, run
on the CPU beside the gateway, and measured - which is the interesting engineering problem,
because the model is not the hard part. Getting inference to sit inside a request path
without becoming the request's dominant cost is.

**Two models, and the eval picks.** Both are in the registry below, and
bench/detection_eval.py runs both over the same corpus: a 17 MB BERT-tiny derivative, and
the 738 MB DeBERTa-v3-base model that most published prompt-injection work cites. The
README prints a row per model with its precision, recall and added latency, and the
Dockerfile bakes in whichever one that table justifies. Keeping both is what makes the
choice arguable instead of asserted.

**Where the time goes, and what is done about it.**

  * *Inference blocks.* ONNX Runtime's `Run` is a synchronous C++ call. Awaiting it on the
    event loop would stall every other request in the process for the duration, so it runs
    on a worker thread. Run releases the GIL, so the loop really does continue.
  * *It can be slow.* A transformer on a shared Fargate vCPU is tens to hundreds of
    milliseconds, and a request waiting on the gateway is not waiting on the model it asked
    for. Inference therefore has a deadline, and a miss is a failure to inspect rather than
    a reason to make the caller wait longer.
  * *Cold start is a deploy's problem, not a caller's.* The session is built and warmed at
    startup, because the first `Run` allocates the arena and materialises the optimised
    graph - tens of milliseconds to seconds that would otherwise be paid by whichever
    unlucky request arrived first after a rollout.

**Truncation is an evasion, so the prompt is windowed.** These models see 512 tokens. A
prompt that is 5,000 tokens long with the payload at the end would pass a detector that
simply truncated - and "put it at the end" is the cheapest evasion there is. The text is
therefore cut into overlapping windows and scored window by window, with the highest score
deciding. The window count is capped, because an unbounded prompt would otherwise be an
unbounded amount of inference; past the cap the classifier has not seen everything, and it
is the regex baseline - which reads the whole prompt for microseconds - that covers the
tail. That division of labour is the reason both tiers stay in the request path.

**The label order is pinned here, not read from the model.** `INJECTION` is index 1 in both
models' `config.json` at the revisions below. Reading it from the file at startup would look
more careful and be less safe: a revision that swapped the labels would quietly invert every
verdict, where a pinned index disagrees loudly the first time the eval runs.
"""

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

logger = logging.getLogger("tollgate.detect.classifier")

# The two files a model needs to run: the graph, and the tokenizer that has to match it
# exactly. Everything else in a model repository is for training or for other runtimes.
MODEL_FILE = "model.onnx"
TOKENIZER_FILE = "tokenizer.json"


@dataclass(frozen=True)
class ModelSpec:
    """One classifier this gateway knows how to run, pinned to exact bytes.

    `revision` is a commit sha and never a branch name, and every file carries a SHA-256.
    A model is a dependency like any other: "whatever main points at today" is not a
    version, and a detector whose behaviour changes without a commit is a detector whose
    published precision means nothing.
    """

    name: str
    repo: str
    revision: str
    # Local filename -> (path within the repository, expected SHA-256).
    files: dict[str, tuple[str, str]]
    # Which output index means "this is an injection attempt". See the module docstring.
    injection_index: int
    # The model's own context limit, in tokens, which is what makes windowing necessary.
    max_tokens: int
    # The cut-off between flagged and clean, for this model and no other. A threshold is a
    # property of the model it was measured against, so it lives on the spec rather than in a
    # global setting, and bench/detection_eval.py is what sets it.
    default_threshold: float
    # What the graph weighs on disk, for the table in the README: the trade-off being
    # measured is precision against latency *and* image size.
    size_bytes: int

    def directory(self, root: Path | str) -> Path:
        return Path(root) / self.name


KNOWN_MODELS: dict[str, ModelSpec] = {
    # 17 MB, a 4-layer BERT. Small enough to commit into an image without thinking about
    # it and fast enough that its latency is a rounding error next to a model call.
    "tiny": ModelSpec(
        name="tiny",
        repo="testsavantai/prompt-injection-defender-tiny-v0-onnx",
        revision="fef37c0dc81859a9bd34c2f6e1cbf631fb36cb39",
        files={
            MODEL_FILE: (
                "model.onnx",
                "77821e472a9665839c0b125ffcdc3d6f1036039e11d0f24c917b79d1ff643650",
            ),
            TOKENIZER_FILE: (
                "tokenizer.json",
                "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
            ),
        },
        injection_index=1,
        max_tokens=512,
        # Swept, not guessed. F1 peaks at 0.15 on the eval corpus and gets there with a 24%
        # false positive rate - 54% on the prompts that only look hostile - so F1 is the wrong
        # thing to maximise when one of its halves lands on a customer's traffic. At 0.5 the
        # measurement is 0.658 recall at 0.790 precision and a 13% false positive rate, which
        # is the balance a tenant could actually be put in block mode on.
        default_threshold=0.5,
        size_bytes=17_605_150,
    ),
    # 738 MB of DeBERTa-v3-base, and the model most published prompt-injection work cites.
    # It is here to be the expensive end of the comparison: if the small one is close, the
    # small one wins, and the only way to say "close" is to run both.
    "deberta-base": ModelSpec(
        name="deberta-base",
        repo="protectai/deberta-v3-base-prompt-injection-v2",
        revision="90c9989b1a342275dd0d1a95aad283c04e075671",
        files={
            MODEL_FILE: (
                "onnx/model.onnx",
                "f0ea7f239f765aedbde7c9e163a7cb38a79c5b8853d3f76db5152172047b228c",
            ),
            TOKENIZER_FILE: (
                "onnx/tokenizer.json",
                "752fe5f0d5678ad563e1bd2ecc1ddf7a3ba7e2024d0ac1dba1a72975e26dff2f",
            ),
        },
        injection_index=1,
        max_tokens=512,
        # This model barely has a threshold: its scores saturate at zero and one, so recall
        # moves only from 0.692 to 0.640 across the whole range from 0.05 to 0.95. 0.5 is as
        # good as any number here, which is worth knowing - the same sweep is decisive for the
        # small model above and irrelevant for this one.
        default_threshold=0.5,
        size_bytes=738_563_188,
    ),
}


def digest_of(path: Path) -> str:
    """SHA-256 of a file, read in chunks so a 738 MB graph is not held in memory twice."""
    hashed = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hashed.update(chunk)
    return hashed.hexdigest()


# --- what the runtime needs, and nothing more ---------------------------------------------


class Encoding(Protocol):
    """One tokenized window, as `tokenizers` returns it."""

    @property
    def ids(self) -> Sequence[int]: ...
    @property
    def attention_mask(self) -> Sequence[int]: ...
    @property
    def type_ids(self) -> Sequence[int]: ...
    @property
    def overflowing(self) -> Sequence["Encoding"]: ...


class Tokenizer(Protocol):
    def encode(self, sequence: str) -> Encoding: ...


class Session(Protocol):
    """The two things this module uses from an ONNX Runtime session.

    Narrow on purpose: the tests drive a `Classifier` with a stub in place of a real
    session, so the windowing, the arithmetic, the deadline and the failure handling are all
    exercised by a suite that touches no model file and needs no network.
    """

    def get_inputs(self) -> Sequence[Any]: ...
    def run(self, output_names: list[str] | None, input_feed: dict[str, Any]) -> list[Any]: ...


@dataclass(frozen=True)
class Windows:
    encodings: list[Encoding]
    # False when the prompt was longer than `max_windows` windows, so the classifier has
    # not read all of it. Recorded rather than hidden: it is the one case where the
    # classifier's silence means "did not look" rather than "looked and found nothing".
    complete: bool


@dataclass(frozen=True)
class Score:
    """What the classifier made of one prompt."""

    value: float  # probability of the injection class, 0 to 1
    windows: int  # how many windows were scored to get there
    # Whether those windows covered the whole prompt. False when it needed more windows than
    # the cap allows, or than the time budget paid for. A low score from an incomplete read
    # is weaker evidence than a low score from a complete one, and the ledger keeps the
    # difference.
    complete: bool


def softmax_injection(logits: Sequence[float], index: int) -> float:
    """The probability of one class, from raw logits, without pulling in scipy.

    The maximum is subtracted before exponentiating. Without that, a confident model's
    logits overflow `math.exp` and a detector starts raising instead of deciding.
    """
    largest = max(logits)
    weights = [math.exp(value - largest) for value in logits]
    return weights[index] / sum(weights)


class Classifier:
    """One loaded model, scoring prompts.

    One session, shared by every request in the process, exactly like the upstream HTTP
    client: building it costs a second for the larger model, and ONNX Runtime sessions are
    documented as safe to call from several threads at once.
    """

    # How much longer than its own budget a call may take before the backstop gives up on
    # it. Generous, because tripping it means a native call has wedged rather than that the
    # machine was briefly busy, and a false backstop reports a working detector as broken.
    BACKSTOP_GRACE_S = 5.0

    def __init__(
        self,
        spec: ModelSpec,
        session: Session,
        tokenizer: Tokenizer,
        *,
        threshold: float,
        timeout_s: float,
        max_windows: int,
    ) -> None:
        self.spec = spec
        self.threshold = threshold
        self._session = session
        self._tokenizer = tokenizer
        self._timeout_s = timeout_s
        self._max_windows = max(1, max_windows)
        # Which tensors this graph actually wants. BERT asks for token_type_ids, DeBERTa
        # does not, and feeding a model an input it never declared is an error rather than
        # something it ignores - so the feed is built from what the session reports instead
        # of from what the architecture is assumed to be.
        self._input_names = {tensor.name for tensor in session.get_inputs()}

    @property
    def name(self) -> str:
        return self.spec.name

    async def score(self, text: str) -> Score | None:
        """Score one prompt off the event loop, or None if it could not be scored at all.

        None is not "clean". The caller records it as a failure to inspect, because a tenant
        that asked for classifier-grade inspection and got an exception has not had its
        request inspected - and a failure quietly reported as clean is how a detector comes
        to be trusted for something it never did.

        Running out of time is *not* that case: the deadline is spent window by window
        inside `score_now`, which stops early and says so on the `Score`. `wait_for` here is
        only the backstop for a single window wedging, which the budget cannot interrupt
        because ONNX Runtime's `Run` is one uninterruptible native call.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.score_now, text, self._timeout_s),
                self._timeout_s * 2 + self.BACKSTOP_GRACE_S,
            )
        except TimeoutError:
            logger.warning(
                "classifier inference wedged past its backstop",
                extra={"fields": {"model": self.name, "timeout_s": self._timeout_s}},
            )
            return None
        except Exception:
            logger.exception("classifier inference failed")
            return None

    def score_now(self, text: str, budget_s: float | None = None) -> Score:
        """The blocking part: tokenize, score window by window, keep the worst.

        `budget_s` is spent as it goes rather than enforced at the end. The alternative - one
        deadline over the whole call - throws away the windows that did finish, and on a long
        prompt with an expensive model it would report every such request as uninspected: the
        larger model here takes about 20 ms on a short prompt and about 300 ms on a full
        512-token window, so "long prompt" and "over budget" are the same thing. Here a
        prompt too long for its budget is inspected as far as the budget reached, `complete`
        says it was not all of it, and the regex baseline has meanwhile read every byte.

        The first window is always scored, whatever the budget. A classifier that can return
        no opinion at all under load stops working exactly when traffic is heaviest, and the
        first window is where a direct injection almost always is.

        Public, and callable without a budget, because the warm-up and
        bench/detection_eval.py both want the model's own answer rather than this gateway's
        patience with it.
        """
        started = time.perf_counter()
        windows = self._windows(text)
        scores: list[float] = []
        for index, window in enumerate(windows.encodings):
            if index and budget_s is not None and time.perf_counter() - started >= budget_s:
                # Out of time, with at least one window scored. What was read was read.
                return Score(value=max(scores), windows=len(scores), complete=False)
            scores.append(self._run(window))
        return Score(value=max(scores, default=0.0), windows=len(scores), complete=windows.complete)

    def _windows(self, text: str) -> "Windows":
        """The prompt as overlapping windows the model can actually read.

        The tokenizer does the splitting, because it is the only component that knows where a
        token boundary is; `stride` is configured where the tokenizer is built, so the windows
        overlap and a payload straddling a boundary is whole in one of them.
        """
        first = self._tokenizer.encode(text)
        encodings = [first, *list(first.overflowing)]
        complete = len(encodings) <= self._max_windows
        return Windows(encodings[: self._max_windows], complete)

    def _run(self, encoding: Encoding) -> float:
        feed: dict[str, Any] = {
            "input_ids": np.asarray([list(encoding.ids)], dtype=np.int64),
            "attention_mask": np.asarray([list(encoding.attention_mask)], dtype=np.int64),
            "token_type_ids": np.asarray([list(encoding.type_ids)], dtype=np.int64),
        }
        outputs = self._session.run(None, {k: v for k, v in feed.items() if k in self._input_names})
        logits = [float(value) for value in outputs[0][0]]
        return softmax_injection(logits, self.spec.injection_index)


def build_classifier(
    *,
    model: str,
    model_dir: str,
    threshold: float | None,
    timeout_ms: float,
    threads: int,
    max_windows: int,
    stride_tokens: int,
) -> Classifier | None:
    """Load the configured classifier, or None when none is configured.

    Every failure here is a refusal to start. A gateway that believes it is running a
    classifier and is silently running a regex is the worst of the three possible states:
    the dashboard says `tier=baseline`, nobody reads it, and the published recall figure
    describes software that is not deployed. Missing files, an unknown model name and an
    unreadable graph therefore all stop the process with a message that says what to run.
    """
    if not model:
        return None
    spec = KNOWN_MODELS.get(model)
    if spec is None:
        raise RuntimeError(
            f"DETECTION_CLASSIFIER_MODEL={model!r} is not a model this gateway knows. "
            f"Known models: {', '.join(sorted(KNOWN_MODELS))}."
        )

    directory = spec.directory(model_dir)
    missing = [name for name in spec.files if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(
            f"{spec.name} is missing {', '.join(missing)} in {directory}. "
            f"Fetch it with: uv run python -m tollgate.detect.fetch {spec.name}"
        )

    # Imported here rather than at module scope: onnxruntime is a large native library and
    # tokenizers a Rust one, and a deployment running the baseline alone - or a test run that
    # never touches this tier - has no reason to load either. numpy is at the top, because
    # onnxruntime brings it in regardless and the request path builds arrays with it.
    import onnxruntime
    from tokenizers import Tokenizer as HfTokenizer

    tokenizer = HfTokenizer.from_file(str(directory / TOKENIZER_FILE))
    # Truncation with a stride is what produces the overlapping windows above. Without the
    # stride the windows would abut, and a payload lying across a boundary would be split
    # in half in both of them.
    tokenizer.enable_truncation(max_length=spec.max_tokens, stride=stride_tokens)

    options = onnxruntime.SessionOptions()
    # One thread by default. A Fargate task has a fraction of a vCPU to spare and dozens of
    # requests in flight, so a model that parallelises across four threads makes one
    # request faster and every other request slower.
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = threads
    options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = onnxruntime.InferenceSession(
        str(directory / MODEL_FILE), sess_options=options, providers=["CPUExecutionProvider"]
    )

    classifier = Classifier(
        spec,
        session,
        tokenizer,
        threshold=spec.default_threshold if threshold is None else threshold,
        timeout_s=timeout_ms / 1000,
        max_windows=max_windows,
    )
    warm_up(classifier)
    return classifier


def warm_up(classifier: Classifier) -> None:
    """One inference at startup, so the first real request does not pay for the last mile
    of loading: the arena allocation and the optimised graph are both materialised by the
    first `Run` rather than by the constructor."""
    started = time.perf_counter()
    try:
        classifier.score_now("warm up")
    except Exception:
        # Not fatal. The session loaded, so the next attempt may well work, and the request
        # path already treats a failed inference as a failure to inspect rather than a 500.
        logger.exception("classifier warm-up failed")
        return
    logger.info(
        "classifier ready",
        extra={
            "fields": {
                "model": classifier.name,
                "threshold": classifier.threshold,
                "warm_up_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        },
    )
