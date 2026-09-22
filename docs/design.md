# Design notes

Why Tollgate behaves the way it does, decision by decision. The short version is in the
[README](../README.md).

## Design decisions

What the gateway does where there was a choice, and why it does that:

| Decision | Why |
|---|---|
| Byte-compatible with the provider API | Adoption is a base URL change. It also means streaming has to be relayed event by event, not rebuilt. |
| Provider credential stays server side | Tenants authenticate to Tollgate with their own keys and never see the upstream key. |
| Tenant keys stored only as SHA-256 hashes | A database leak doesn't expose live credentials. A short plaintext prefix identifies a key in logs. |
| Money in integer micro-cents | Floating-point cost errors are small, silent and impossible to reconstruct later, and a cent is too coarse to hold a request: one flash call costs a few hundred micro-cents. A test fails the build on any float or decimal column. |
| Budgets reserved before the upstream call | A streamed response's cost is unknown when it starts, so an estimate is reserved from the requested maximum and reconciled when the stream ends. |
| A reservation is a lease, not a lock | A gateway killed mid-stream would otherwise hold part of a tenant's budget until someone noticed. Reservations carry an expiry and the next request drops the ones that ran out. |
| Nothing in Redis is a source of truth | Every key is a cache of the ledger or in-flight bookkeeping, each with a TTL. That is what makes it safe to run Redis with eviction enabled. |
| Rate limits fail open, budgets do not | A limiter protects the upstream, so its outage must not become a total outage. A budget is money, and Postgres already holds the ledger, so losing Redis costs precision rather than enforcement. |
| Out of budget is 402, not 429 | Retrying will not work until the month turns over. A `Retry-After` measured in weeks is worse than no hint at all. |
| Caches scoped per tenant, with no opt-out | A shared cache leaks one tenant's traffic to another, and the leak would be one configuration mistake away. There is no setting for it. |
| Cache key is a deny-list, not an allow-list | The whole request body is hashed except a named few transport-only fields. A field the gateway has never heard of then causes a miss rather than a collision: extra misses cost money, wrong hits cost trust. |
| Nothing sampled is cached by default | A request naming no temperature is served at the provider's default of 1.0, so it is bypassed. Caching it would change what a tenant's product does without changing a line of their code. |
| A cache hit is billed at zero, and the saving stored beside it | `cost_microcents` is zero and `cost_avoided_microcents` holds what the call would have cost. Folding a notional saving into the money column would inflate every bill and every budget check that reads it. |
| Cached responses replayed as real streams | One entry serves both methods, because they differ in framing rather than in content. A streaming caller gets events, a `responseId`, and a `finishReason` on the last one. |
| Semantic cache threshold chosen from a measured curve | A wrong semantic hit is a correctness bug, not a performance trade-off. The sweep is what turned "0.93 looks safe" into "no value is safe for these embeddings". |
| The semantic tier ships off, with no threshold at all | Not a conservative default — an absent one. The two committed sweeps disagree completely, so any number here would be derived from a model the operator is probably not using. Enabling the tier without stating one refuses to start. |
| Similarity covers the prompt and nothing else | Everything else that changes an answer is hashed into `params_key` and must match exactly first, so the same question under two different system instructions is never one entry. |
| A conversation with a non-text part is never matched semantically | `contents` is excluded from the parameters hash and the embedding only sees text, so an image would be covered by neither. |
| Embedding spend recorded in its own column | The tier pays for a lookup whether or not it finds anything. Folding that into the tenant's cost would hide a tier spending more than it saves. |
| Detection defaults to monitor mode | Blocking is opt-in per tenant, which is how detection is rolled out in practice: nothing is refused until somebody has read the false positive rate against real traffic. |
| One policy covers both directions | A tenant's mode governs what is inspected on the way in and what may be delivered on the way back. Two settings would double the matrix to describe a combination nobody has asked for. |
| Detection fails open; budgets do not | A budget protects money, so losing the ability to check it stops the request. Detection is advisory, so a bug in a regex must not become an outage for every tenant in block mode. The verdict `error` makes that failure a series on a dashboard rather than a silence. |
| A refusal names nothing it found | Both 403s say only that policy refused the request. Naming the rule would hand an attacker an oracle: send a prompt, read the rule, reword until nothing fires. The rule id goes to the ledger and the span instead. |
| The regex baseline stays in the repository | It exists to be beaten, and deleting it once the classifier wins would delete the comparison. Every eval run scores both, and the README prints both rows. |
| The classifier is pinned to a commit and a hash | A model is a dependency. "Whatever main points at today" is not a version, and a detector whose behaviour changes without a commit makes every published precision figure unreproducible. |
| The model is baked into the image | Fetching at boot would make every cold start depend on a model host being reachable. A readiness probe failing because huggingface.co is having an afternoon is a bad trade for a few tens of megabytes. |
| A configured-but-missing model refuses to start | The worst state is a gateway that believes it is running a classifier, is running a regex, and reports it to a dashboard nobody reads. |
| A long prompt is windowed, never truncated | These models read 512 tokens, and "put the payload at the end" is the cheapest evasion there is. The classifier scores overlapping windows up to a cap; past it the regex baseline, which reads every byte, is what covers the tail. |
| The inference budget is spent window by window | One deadline over the whole call throws away the windows that did finish. The larger model takes ~300 ms on a full window, so "long prompt" and "over budget" would be the same thing, and every such request would be reported as uninspected. |
| Output scanning never redacts | A finding stops a response; it never edits one. Rewriting response text would break the byte-compatibility the whole gateway rests on, and a redacted answer looks like the model behaving oddly rather than like a policy decision. |
| Streams are held back only in block mode | A fixed window of bytes is scanned before it is released, so a credential found inside it never leaves. Monitor mode holds nothing back, because delaying a stream in order to do nothing about what is found buys latency and no containment. |
| A response with findings is never cached | Caching a leak replays it to everybody who later asks the same question, which turns one bad response into a permanent one. |
| The eval corpus is committed, not fetched | A benchmark whose data is downloaded at run time cannot be reproduced once the source moves. Both public sets are Apache-2.0, and the sampling is seeded. |
| No prompt or response text in telemetry | Enforced by a test, not by convention: the check searches every span, metric label and log line for a canary. |
| Instrumentation written out rather than installed | The FastAPI auto-instrumentation records the query string, and a tenant may put its key in `?key=`. That would write a live credential into every trace, and traces leave the building. |
| The alert watches overhead, not total latency | Total latency is mostly the provider generating tokens. An alert on that fires whenever the model has a slow afternoon, and is muted within a week. |
| Metrics emitted in one place, at the end | A refusal never reaches the proxy, so metrics emitted there would omit every 401, 429 and 402 - exactly the requests a rejection rate is meant to count. |
| `/metrics` off in production | It lists tenant names and their spend, and the load balancer is public. Production pushes over OTLP, which needs no inbound route. |
| Every benchmark runs against a local mock | Tests and load tests cost nothing and don't depend on provider variability. |
| Load offered at an arrival rate, never by a fixed pool of workers | A pool that waits for each reply slows down when the service does, so the throughput it reports is a measurement of its own patience. Only an open model finds a ceiling. |
| Overhead under load read from the gateway's own histogram | A load generator sees the provider's 100 ms as well as the gateway's cost, and recovering one by subtracting distributions is wrong at the tail. The gateway already records total-minus-upstream per request, so the benchmark and the alert read the same series and cannot disagree. |
| A bucketed percentile printed as its bucket | Interpolating inside a 100–250 ms bucket and printing `175.0ms` claims three significant figures the histogram never had, and makes two unrelated metrics landing in one bucket look like a suspicious coincidence. The exact mean goes beside it. |
| The load test's ceiling is the whole stack's, and says so | Five processes share one laptop, and above 60 requests a second the mock slows with the gateway. Calling that the gateway's ceiling would be the one flattering benchmark on the page. |

### The provider credential never leaves the gateway

Tenants authenticate with Tollgate keys, sent in the same `x-goog-api-key` header the Gemini SDK
already uses. The gateway removes that key and attaches the provider key, which exists only in the
gateway's environment. Only an allowlist of request headers is forwarded upstream, a `?key=` query
parameter is stripped, and the key is held as a `SecretStr` so it can't appear in a log line or a
traceback. Tenants never hold a credential that works against the provider directly, so a leaked
tenant key can be revoked in one place without rotating the provider key for everyone.

### Telling a gateway failure from an upstream failure

Upstream errors pass through untouched, in Google's shape. Errors raised by the gateway itself
carry `"source": "gateway"`:

```json
{"error": {"code": 429, "message": "...", "status": "RESOURCE_EXHAUSTED"}}
{"error": {"source": "gateway", "code": "upstream_timeout", "message": "..."}}
```

Rate limits (429), server errors (5xx) and failed connections are retried with jittered
exponential backoff. Read timeouts are not: by then the upstream may already be generating, and
billing for, the response.

### Caching, and the two ways it can be wrong

A cache that misses when it could have hit costs money. A cache that hits when it should have
missed returns one request's answer to a different question, which is a correctness bug wearing a
performance improvement's clothes. Every decision below makes the first mistake in order to avoid
the second.

**The key.** A SHA-256 over the whole normalised request — provider, tenant, model, query string
and body — rather than over a named list of fields that matter. Hashing a list of known fields is
the obvious design and the wrong way round: a provider that adds an output-affecting field tomorrow
would be silently ignored, and two requests differing only in that field would share an entry.
Hashing everything but a named few inverts the failure, so an unrecognised field changes the key
and the request goes upstream. Only `alt` and `key` are excluded, because one picks the response
framing and the other is the caller's own credential.

**Normalisation is structural only.** Object keys are sorted, because JSON objects are unordered by
definition. Numbers whose value is whole are written as integers, so `"temperature": 0` and
`"temperature": 0.0` — which an encoder chooses between on the client's behalf — are one request.
Nothing else is touched. Prompt text is never trimmed, cased or collapsed: `"Hello"` and `"hello "`
are different inputs to a model, and a cache that treats them as one has made that decision on the
model's behalf. Arrays keep their order, because the order tool declarations are given in is part
of the prompt. The key carries a version, so changing any of this retires the entries it
invalidates rather than reinterpreting them.

**Non-deterministic requests are refused, by default all of them.** `CACHE_MAX_TEMPERATURE`
defaults to `0.0`, and a request above it is bypassed entirely — not looked up, not stored — with
`cache_status` recording why. A request that names no temperature counts as sampled, because Gemini
samples at 1.0 when asked for nothing; reading an absent temperature as zero would cache sampled
output under a ceiling written to forbid exactly that. This costs hit rate, and it is the setting
that cannot change a tenant's product behind their back. Responses are refused too, unless they
finished with `STOP`: a `SAFETY` or `RECITATION` stop is a policy decision the provider revisits,
and freezing one into a cache is how a product acquires a refusal nobody can explain or clear.

**Why entries are scoped per tenant, and why that is not configurable.** Sharing across tenants
would raise the hit rate considerably, and the [threat model](threat-model.md) is the reason it is refused rather than
made opt-in. A shared cache gives one tenant a timing oracle over another's traffic: a hit returns
in 14.6 ms and a miss in 122.5 ms, so anyone able to send a request can learn whether some other
customer has recently asked a given question. That is enough to confirm a guess about a
competitor's prompts, their product's features, or which of their customers they are working with.
Beyond timing, a shared entry hands one tenant content generated for another's prompt, which may
quote it. Both risks are structural rather than incidental, so `tenant_id` is a column the lookup
filters on *and* part of the hashed material, which means the keys of two tenants asking the same
question do not collide even if a future query forgets the filter. There is no setting to turn this
off, because a setting is one mistake away from being turned on.

**What is stored.** The response, whole, and nothing of the request but its hash, which cannot be
read back. A dump of `cache_entries` shows what was answered and never what was asked. Entries
carry a TTL, checked by Postgres so one clock decides for every container, and `tollgate cache`,
`tollgate cache-prune` and `tollgate cache-clear` are the operator's view of it — counts only.

**Replaying a stream.** A cached response is re-emitted as real events: the same framing, the same
`responseId`, cumulative `usageMetadata`, and `finishReason` on the last event only. The stored
form is the unary response object, so `assemble()` and `stream_events()` are inverses of each other
and one entry can serve both methods. Caching a streamed response means holding a copy of it, which
is the one thing the relay otherwise never does — so the copy is bounded at
`CACHE_MAX_RESPONSE_BYTES`, and a response that outgrows it is relayed exactly as before and simply
not stored. A partial answer is never stored at all: a caller that hangs up, or a stream that
fails, leaves nothing behind, because half an answer replayed as though it were whole is the worst
thing this cache could do.

**The semantic tier, and why it is off.** The second tier answers a request from a *similar* one
rather than an identical one. It runs only after the exact tier has missed, because it costs an
embedding call — an upstream round trip, paid whether or not anything is found. Similarity is
measured over the prompt and nothing else: everything else that shapes an answer is hashed into
`params_key`, which has to match exactly before two entries are ever compared, so the same question
asked under "answer in one word" and "answer at length" are never one entry. A conversation
carrying anything but text is refused outright, because `contents` is excluded from that hash and
the embedding cannot see an image, so such a request would be covered by neither.

Storage is pgvector with an HNSW index. IVFFlat partitions the space by clustering a sample of the
rows, so an index built on an empty table sorts everything into one list and has to be rebuilt once
there is data — the wrong shape for a cache that starts empty on every deploy. HNSW builds its
graph incrementally and is correct from the first row. The lookup is a filtered vector search,
which is the thing to know about pgvector: the index answers "nearest overall" while the query
wants "nearest among this tenant's entries with these parameters", so a true neighbour can be
discarded by the filter. `hnsw.iterative_scan` keeps scanning until enough rows survive it, and
where that still falls short the failure is a miss — the request goes upstream and the caller gets
a correct answer. Recall lost costs money; precision lost costs trust.

The threshold is the whole of the decision, so it comes from `bench/cache_sweep.py` rather than
from a number that looks about right. Against real embeddings that sweep found no safe number at
all, which is why `CACHE_SEMANTIC_ENABLED` defaults to `False`, why `CACHE_SEMANTIC_THRESHOLD` has
no default, and why enabling the first without setting the second refuses to start. The Results
section has the working.

The tier is off because of what the sweep measures, not because anything is missing: it is
implemented and tested on both paths. A gateway in front of an embedding model that *can* separate
those pairs turns it on with two settings and gets the hit rates in `cache_savings_semantic.txt`.

### Detection, and what a detector is worth

Two directions, one policy. On the way in, the gateway decides whether a request looks like an
attempt to take over the model it is in front of. On the way back, whether the response is
carrying something that must not leave. A tenant's `detection_mode` is `off`, `monitor` or
`block`, and **monitor is the default**, because that is how detection is actually rolled out:
against real traffic, until somebody has read the false positive rate, and only then allowed to
refuse anybody.

**Two tiers on the way in, and a request is flagged if either says so.** The regex baseline runs
first: it costs tens of microseconds and it reads the whole prompt. The classifier runs only on
what the baseline cleared, so an obvious injection never pays for inference. The split is not
arbitrary — the model reads 512 tokens at a time and a bounded number of windows, so on a very
long prompt it has not seen all of it, and the baseline has.

**Normalisation here is the inverse of the cache's.** The cache refuses to touch prompt text,
because `"Hello"` and `"hello "` are different inputs to a model. Detection has to do exactly what
the cache must not, because here a spelling difference *is* the attack: a zero-width space inside
`ig<U+200B>nore`, fullwidth characters, five spaces between words. The folded copy is used for
matching and for nothing else — the request that goes upstream is the one the tenant sent, byte for
byte.

**The classifier is a dependency, pinned like one.** A commit sha and a SHA-256 per file, fetched
at build time by `python -m tollgate.detect.fetch` and verified before it is allowed into the
image. Nothing is trained here; a published model is integrated, measured, and enabled or swapped
by one `ARG` in the Dockerfile - and the default is none, for reasons the Results section gives
with numbers. Inference runs on a worker thread because ONNX Runtime's `Run` is one
uninterruptible native call, and the session is built and warmed at startup, because a cold start
is a deploy's problem rather than the first caller's.

**The inference budget is spent window by window rather than enforced at the end.** The larger
model takes about 20 ms on a short prompt and about 300 ms on a full 512-token window, so a single
deadline over the whole call would mean every long prompt was reported as uninspected. Instead the
first window always runs, later ones run while time remains, and `complete` records that the model
did not read all of it. A timeout is not a failure; only an exception is, and that is recorded as
the verdict `error`, never as `clean`.

**On the way back, shapes rather than secrets.** Fifteen rules over the response text - eleven
credential formats and four kinds of personal data: credential
formats (including this gateway's own tenant key format and its provider key's — the two that are
certainly credentials and should never appear at all), and personal data, with Luhn on card numbers
and issuance rules on social security numbers, because cheap arithmetic removes whole classes of
false positive that a longer regex cannot.

**The streaming problem, and the honest answer to it.** A response cannot be inspected before it is
delivered, because it is delivered as it is generated. In `monitor` mode the relay scans what
passes and records what it finds — after the fact, which is a real limitation and not a
configuration detail. In `block` mode the relay runs `DETECTION_STREAM_HOLDBACK_BYTES` behind the
upstream: bytes are scanned and released only once that much more has arrived behind them, so a
credential found inside the unreleased window is dropped and the stream is cut off with an error
event. That window is the length of leak that can still be contained, and it is paid for in time to
first token. A finding earlier than the window is already gone — recorded, alerted on, not undone.
The ledger keeps `blocked` and `truncated` apart for exactly that reason: one is a response the
caller never saw, the other is a stream that stopped part way.

A private key is kilobytes long and announces itself in its first forty bytes, which is why a
kilobyte of hold-back is enough for the case that matters most.

**Nothing that is found is ever quoted.** A verdict carries a rule id and a score; a finding
carries a rule id. Never the matched text, in the ledger, in a span, in a metric label or in a log
line — the same canary test that guards prompts in telemetry is pointed at both detectors, because
the component whose job is to read responses is the one most likely to be helpful about it.
