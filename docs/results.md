# Results in full

Every measurement, what it was compared against, and how to reproduce it. The headline
figures are in the [README](../README.md).

## Results

Every number below is produced by the command beside it, and the full output of each is committed
under `bench/results/`. The ones that came out badly are here too.

| Measurement | Compared against | Measured by | Result |
|---|---|---|---|
| Gateway overhead, p50 and p99 | Same call made directly to the provider | `baseline_latency.py`, `load_test.py` | **+10.4 ms** / **+14.1 ms** sequential; **12.0 ms** mean and flat from 20 to 60 requests a second |
| Added time to first token, streamed | Direct streaming call | `stream_ttft.py` | **+5.2 ms** p50, **+5.4 ms** p99 (local) |
| Rate limit accuracy across containers | In-process counters vs Redis | `limit_accuracy.py` | in-process **+95%** at 2 tasks, **+388%** at 5; Redis **0%** at any count |
| Budget reservation error, streamed | Reserved estimate vs reconciled actual | `reservation_error.py` | estimate overshoots **14x** (median); reconciled error **0 micro-cents** |
| Usage rollup query time | With and without the index, with plans | `rollup_plan.py` | **7.7 ms → 0.2 ms**, 8,334 → 33 buffers |
| Cache hit rate, exact | The repeat rate of the traffic it was offered | `cache_savings.py` | **61.5%** to **89.2%**, against a repeat ceiling of 62% to 89% |
| Cache hit rate, both tiers | Exact tier alone, on identical traffic | `cache_savings.py` | **68.7%** to **96.0%** — the semantic tier adds **6.8** points |
| Cached hit latency | The upstream call it replaces | `cache_savings.py` | **14.6 ms** against **121 ms**, 8x |
| Cost of a semantic miss | A miss with the tier off | `cache_savings.py` | **230 ms** against **121 ms**: every miss pays an embedding round trip |
| Spend avoided per 1,000 requests | Identical traffic with caching disabled | `cache_savings.py` | **$0.077** to **$0.118**, saving **59%** to **96%** |
| Semantic tier, net of embedding spend | What it spent looking, found or not | `cache_savings.py` | **+$0.084** to **+$0.117** per 1,000, against **$0.0011–0.0014** spent |
| Semantic false hit rate | Full threshold sweep, 51 labelled pairs, two embedding models | `cache_sweep.py` | **No safe threshold exists** for `gemini-embedding-001`; the mock's embeddings give **0%** at 0.93 |
| Injection recall and precision | Regex baseline on identical data, 1,200 cases | `detection_eval.py` | baseline **0.374** recall at **0.908** precision; `deberta-base` **0.666** at **0.938**; the pair **0.714** at **0.911** |
| False positives on benign security-related prompts | The same detectors on ordinary benign traffic | `detection_eval.py` | **23.2%** of the adversarial-looking set for the baseline and **39.1%** for the pair, against **2.7%** and **5.0%** overall - and on 28 ordinary short app prompts, **0%** for the baseline and `deberta-base` against **42.9%** for `tiny` |
| Added latency per detection layer | Detection disabled | `detection_eval.py` | regex **0.04 ms** p50 / **1.2 ms** p99; `tiny` **0.34 / 5.9 ms**; `deberta-base` **35.3 / 634 ms**; output scan **0.31 ms** for a 2 KB answer whole, **1.24 ms** for the same answer streamed in 26 events, **4.8 ms** at 32 KB |
| Throughput ceiling and failure mode | Steady vs burst vs degraded upstream | `load_test.py` | overhead flat at **12 ms** to **60 rps**, then the whole single-laptop stack saturates together; a 4x burst leaves p99 at **471 ms** for one quiet period after it ends, against **127 ms** before it; a degraded upstream costs the gateway **nothing** — overhead stays at 15–20 ms and **0** requests end without a status |
| Cache, warm against cold, under concurrency | The same 400 prompts, twice | `load_test.py` | **9.1 s** to **2.4 s** of wall clock, p50 **211 ms** to **34 ms** |
| Deploy time, merge to live | Pipeline runs | GitHub Actions | **~5 min** (checks, build, migrate, roll out, smoke test) |
| Monthly infrastructure cost | Measured spend for a full day up | AWS billing | **$1.22/day**, about **$37/month** running continuously |

The sequential baseline is measured on one Windows laptop: gateway and mock upstream on localhost,
Postgres in Docker, 300 sequential requests alternating direct and through the gateway, with the
mock holding a steady 100 ms response time. The overhead covers the key lookup, one usage row insert
and the extra hop, with every feature that does real work either absent from the path or missing.
It is the floor, and the figure the load test's concurrent number should be read against.

Time to first token was measured the same way, with `bench/stream_ttft.py`: 60 streamed
requests each way, alternating. The first token arrives about 30% of the way through the
response, which is the check that matters. A gateway that buffered would show the first
token arriving at the end, and its added time to first token would equal the whole
response time rather than 5 ms.

`bench/limit_accuracy.py` gives one tenant 600 requests a minute with a burst of 10, offers
twelve times that for three seconds, and counts what gets through. One container allows the
40 it should. Two allow 78, three allow 117, five allow 195 — each container refilling a
bucket of its own, and nothing in the code looking broken. With the buckets in Redis the
answer is 39 whatever the container count, because there is one bucket. (39 rather than 40:
the window closes a fraction before the last token refills.)

`bench/reservation_error.py` streams 21 requests with `maxOutputTokens` cycling from 64 to
4096 and compares each reservation with what the request actually cost. The estimate
overshoots by 14x at the median and 110x at worst — a request that asked for 4096 tokens and
got a short answer — which is budget a tenant cannot spend while the request is in flight.
After settlement the month's counter in Redis and the sum over the ledger agree exactly: an
error of zero micro-cents, not "within a cent".

`bench/cache_savings.py` sends 1,000 requests drawn from a pool of N distinct prompts with a Zipf
exponent of 1.1, from a cold cache, and sweeps N from 2,000 down to 40 — traffic that almost never
repeats itself, through to traffic that asks the same forty questions all day. Nothing about the
gateway changes between those rows; only the traffic does, and the hit rate moves from 68.7% to
96.0%. That is the honest way to read any cache benchmark, and the reason the repeat rate is
printed beside the hit rate: the cache converted essentially every repeat available to it, 68.7%
against a ceiling of 69% and 96.0% against a ceiling of 96%, which is the only part of the number
that belongs to this gateway.

What does belong to it is the latency. A hit answers in 14.6 ms where the call it replaces takes
122.5 ms, about eight times faster against a mock holding a steady 100 ms. A hit is not free: the
sequential baseline puts the gateway's own overhead at 10.4 ms, so the lookup costs roughly 4 ms,
which is one Postgres round trip for the entry plus the same ledger write every request pays.

The saving is measured from the ledger rather than from a second run with caching switched off.
Every row carries both what the request cost and — on a hit — what it would have cost, so the two
columns sum to the bill the identical traffic would have run up with no cache at all. That is an
exact comparison against the same requests rather than an approximate one against a second sample.

`bench/cache_sweep.py` is the one that decides whether the semantic tier may be switched on at
all. It embeds 51 labelled prompt pairs — 22 that may share an answer, 29 that must not — sweeps a
cosine threshold across them, and reports precision against recall. The negatives are the point:
`Is this configuration safe?` against `Is this configuration not safe?`, `Convert 10 kilometres to
miles` against `Convert 100`, `reverse a list` against `reverse a linked list`. A set of obviously
unrelated pairs would certify any threshold at all.

It was run twice, against two embedding models, and the two runs disagree so completely that the
disagreement is the result.

Against the **mock's** feature hashing the curve behaves the way the textbook says it should. At
0.85 — a number that looks entirely reasonable — precision is 0.818 and 49% of the negative pairs
still come back as hits. The lowest threshold with no false hits is 0.91, and 0.93 after a safety
margin gives 27% recall. What that 27% consists of is worth stating: punctuation, capitalisation
and whitespace variants, which are precisely the normalisations the exact tier refuses to make on
principle. At a safe threshold the tier recovers the hits that principle gave up, and not much
else.

Against **`gemini-embedding-001`** there is no safe threshold at all:

```
must not match, highest:  0.9913  'Convert JSON to YAML.'  /  'Convert YAML to JSON.'
must match, lowest:       0.8611  'What does SSE stand for?'  /  'What does server-sent events...'
```

The classes overlap completely, so every cut-off between them is wrong in one direction and every
cut-off outside them is useless. The false hit rate only reaches zero at 1.00, where recall is
`0.000` — the only safe threshold is the one that finds nothing. By kind, `direction_swap` averages
0.9866 and `negation` 0.9411, both above genuine paraphrase pairs. These are good embeddings doing
exactly what they are built to do: `Convert JSON to YAML` and `Convert YAML to JSON` are about the
same subject. Cosine similarity measures topic, and a cache needs truth-conditional meaning, and no
threshold converts one into the other.

That is why `CACHE_SEMANTIC_THRESHOLD` has **no default** and why turning the tier on without one
refuses to start. A number here would have come from whichever model happened to be swept first.
The same reasoning as `monthly_budget_microcents`: where there is no honest value to invent, the
setting is empty and the caller has to decide.

The tempting response is to drop `direction_swap` from the labelled set, at which point a threshold
reappears. Those pairs stay. `Convert JSON to YAML` and `Convert YAML to JSON` genuinely need
different answers, and a benchmark edited until it agrees with you has stopped being a measurement.
Both curves are committed as `bench/results/cache_sweep.svg` and `cache_sweep_gemini.svg`; the mock
run is the one anybody can reproduce without a key.

`bench/results/cache_savings_semantic.txt` is the same traffic with the tier on, and it is the
honest pair of numbers. The tier lifts the hit rate from 61.5% to 68.7% on traffic where a quarter
of requests are surface rewordings, and from 89.2% to 96.0% on the most repetitive shape. It spends
$0.0011 to $0.0014 per thousand requests on embeddings to do it — against $0.085 saved, a return of
roughly sixty to one. The cost that does not show up in the money column is latency: a miss now
carries an embedding round trip, so miss p50 goes from 121 ms to 230 ms. The tier makes the requests
it helps free and the requests it fails to help nearly twice as slow, and which of those dominates
is a question about traffic rather than about code.

### What the detector is actually worth

`bench/detection_eval.py` runs every arrangement over one committed corpus: 1,200 cases, 503 of
them injections, from `deepset/prompt-injections`, `jackhhao/jailbreak-classification`, 69 prompts
written for this project that look hostile and are not, and 28 that are simply what an application
sends. Full output, including the threshold sweeps, is in `bench/results/detection_eval.txt`.

| Detector | Precision | Recall | F1 | FPR | FPR, looks hostile | FPR, ordinary app prompts | p50 | p99 |
|---|---|---|---|---|---|---|---|---|
| **shipped: regex baseline** | 0.908 | 0.374 | 0.530 | 0.027 | 0.232 | **0.000** | 0.04 ms | 1.20 ms |
| classifier, `tiny` (18 MB) | 0.768 | 0.658 | 0.709 | 0.143 | 0.290 | **0.429** | 0.34 ms | 5.94 ms |
| classifier, `deberta-base` (739 MB) | 0.938 | 0.666 | 0.779 | 0.032 | 0.246 | **0.000** | 35.33 ms | 634 ms |
| baseline + `tiny` | 0.763 | 0.724 | 0.743 | 0.162 | 0.435 | 0.429 | 0.28 ms | 6.80 ms |
| baseline + `deberta-base` | 0.911 | 0.714 | 0.800 | 0.050 | 0.391 | 0.000 | 22.98 ms | 617 ms |

Accuracy is deliberately absent. The corpus is 43% positive, so "always benign" scores 57% and
reads like a pass.

**The classifier beats the baseline by 28 points of recall** (29 for the larger model), which is
the case for having one at all. It does not beat it on false positives,
and the combination beats neither: "flagged if either says so" buys recall and spends precision,
because a false positive from one tier is added to the other's and never cancelled by it. That is
why all five rows are here, and why CI has a floor under each.

**The false positive rate on prompts that only look hostile is 23% for the baseline and 43.5% for
the pair with `tiny`**, against 2.7% and 16.2% on benign traffic overall — an order of magnitude worse
on exactly the traffic a developer using this gateway generates all day. Nobody publishes this
number. It is the reason `monitor` is the default and blocking is per tenant: on this evidence a
tenant whose users are engineers cannot be put in `block` mode, and a tenant whose users are the
public can be. The breakdown by category is in the results file, and the worst two are
`logs_and_traces` and `security_work` — a developer pasting a log line that quotes an attack, and a
security engineer doing their job.

**The eval changed the code once, which is the point of having one.** The first run put 35 of the
baseline's 51 false positives on one rule: `role_override` matched any "act as a ...", so "act as a
technical editor" and "you are a helpful assistant that summarises meeting notes" were both
injection attempts. Requiring the persona to be a *model* rather than a profession cost 6 points of
recall and bought 10 of precision — 0.811 to 0.908, with the false positive rate falling from 0.076
to 0.028. That trade is only arguable because it was measured; without the harness it would have
shipped.

**Four rules contribute nothing on this corpus and stay anyway.** `delimiter_spoof`,
`exfiltration`, `credential_fishing` and `encoded_payload` between them catch zero true positives
here and cause four false ones. They stay because the corpus contains no indirect injection and no
exfiltration payloads at all — these public sets are direct jailbreaks, mostly of one family — and
deleting a rule because the benchmark lacks the attack it covers is how a detector comes to score
well on a benchmark and badly in production. Their measured contribution is recorded rather than
hidden.

**What ships is the regex baseline, and the classifier is opt-in per deployment.** That is a
measurement rather than caution, and it took two of them.

The first killed `tiny`. It flags **43% of ordinary short application prompts** - "Summarise
this.", "Fix this SQL.", "What is a reverse proxy?" - where the regex baseline flags none. That
number does not appear in any published benchmark, because published benign sets are full
sentences and roleplay prompts, and a four-layer BERT scores respectably on those while treating
a terse imperative as an instruction override. It was found by pointing the demo traffic generator
at it and watching one tenant get refused 114 times out of 114. At a threshold where its false
positives are tolerable (0.95) its recall is 0.390, which the free regex baseline matches at
0.374 - so it is dominated everywhere, and it stays in the repository as the evidence for that
rather than as something anybody should run.

The second is about `deberta-base`, which is a genuinely good detector: 0.938 precision, 0.666
recall, and none of those app prompts. It costs 739 MB of image and 35 ms at the median, 634 ms at
the 99th - and inspection time is deliberately inside gateway overhead, because the gateway chose
to spend it. So enabling it means raising the p99 overhead alert from 100 ms to about 750 ms, and
an alert at 750 ms is a far blunter instrument than one at 100 ms: the thing it was built to catch
is this service adding a tenth of a second, and it would no longer notice. That is the real cost
of the feature, and it is not one this project can pay by default while claiming a 10 ms overhead.

So the default build runs the baseline, the classifier is one build argument away with the numbers
above in view, and both are honest:

```sh
docker build --build-arg DETECTION_MODEL=deberta-base .   # then DETECTION_CLASSIFIER_MODEL=deberta-base
```

The alternative was to ship a classifier that flags "Summarise this." and quote its 0.72 F1, which
is how a detector ends up switched off in its first week by somebody who never saw a benchmark.

The public datasets are small and noisy, and that is the state of this benchmark landscape rather
than a shortcut taken here: `deepset/prompt-injections` is 662 rows, and `jackhhao`'s positives are
mostly long roleplay jailbreaks that a detector can spot by length. Neither contains the case that
decides whether this feature is usable, which is why the third set was written by hand.
`bench/data/SOURCES.md` has the provenance and the licences, and `tests/test_detection_eval.py`
re-runs the arithmetic on every build so a rule that stops matching, a model that loads but answers
nothing, or a threshold typo fails CI rather than quietly lowering a number in this table.

`bench/rollup_plan.py` builds a throwaway database of 400,000 ledger rows across 25 tenants
and four months, then runs the budget check and the spend rollup under three index variants.
A sequential scan takes 7.7 ms and touches 8,334 buffers; `(tenant_id, created_at)` takes
1.1 ms and 2,508; adding `INCLUDE` for the columns both queries read takes 0.2 ms and 33,
because it becomes an index-only scan. The last figure is a best case — an index-only scan
still visits the heap for rows whose page is not yet marked all-visible, and on an
append-only table those are the newest rows, which is exactly what a current-month query
reads.

The cost is measured AWS spend for one full day with the stack up: the load balancer and
its IPv4 addresses are most of it, then Fargate, then pennies for everything else. The
stack is normally destroyed between work sessions (`terraform destroy` in `infra/main`,
about 5 minutes to rebuild), which brings AWS spend to roughly zero while keeping the
database, images, secrets and DNS. Neon is billed separately for the hours it is awake.

### What it does under load

`bench/load_test.py` drives four k6 scenarios and writes `bench/results/load_test.txt`. k6
runs in a container, so nothing has to be installed, and everything points at the mock, so
a benchmark costs nothing and does not depend on a provider's afternoon.

**The overhead numbers do not come from k6.** What a load generator measures is dominated
by the mock holding each request for 100 ms, and recovering the gateway's share by
subtracting one distribution from another is wrong exactly at the tail, which is the end
worth knowing. The gateway records total-minus-upstream per request, so the runner scrapes
`/metrics` either side of each run and diffs the histogram. That is the
same series the dashboard's overhead panel reads and the same one the alert fires on, which
means this benchmark and that alert cannot disagree about what overhead means. It also
means the percentiles are bucketed, so they are printed as the bucket they landed in
wherever the bucket is wide, and the exact mean is printed beside them.

Load is offered at an arrival rate rather than by a fixed pool of workers. A pool that
sends the next request when the last one returns cannot overload anything: when the service
slows, the generator slows with it, and the throughput it reports is a measurement of its
own patience.

**Steady load**, swept for a ceiling, one 45-second run per rate:

```
 offered  achieved  client p50  upstream mean  overhead mean  overhead p50  overhead p99
      20    20.0/s     120.8ms        107.9ms         12.0ms       10-15ms       15-20ms
      40    39.9/s     120.7ms        107.9ms         12.0ms       10-15ms       15-20ms
      60    59.9/s     121.0ms        107.7ms         12.4ms       10-15ms       20-30ms
      80    79.1/s     122.8ms        151.1ms         65.5ms       10-15ms        >250ms
     100    99.4/s     356.6ms        196.2ms        143.8ms     100-250ms        >250ms
     120   119.1/s     323.4ms        180.0ms        141.0ms     100-250ms        >250ms
```

Overhead is flat at 12 ms from 20 to 60 requests a second — the sequential baseline was
10.4 ms, so concurrency costs the gateway almost nothing until it costs it everything. Past
that it degrades, and the honest reading of how is in the `upstream mean` column: the mock
slows at the same time, from 108 ms to 151 ms and then 196 ms. The gateway, the mock,
Postgres, Redis and the load generator are five processes on one laptop, and above about 60
requests a second they are competing for the same cores. **So 60/s is this stack's ceiling,
not this gateway's.** A number for the gateway alone would need the load generator and the
upstream on different machines, which is a bigger claim than a laptop can support, and
inventing one from these rows would be exactly the flattering benchmark this project has
avoided elsewhere.

What the rows do support is the shape, and the shape is the useful part: overhead is
constant until saturation and then collapses rather than degrading gracefully, and nothing
is shed on the way. Not one request in the 18,866 this sweep sent was refused — the error
rate is 0.0% at every rate, including the two where p99 is above a quarter of a second. A
gateway that queues instead of shedding turns a capacity problem into a latency problem for
every caller at once. (Request counts and error rates per row are in
[`bench/results/load_test.txt`](../bench/results/load_test.txt), which this table is trimmed
from.)

**A burst** shows what that costs. Forty a second for twenty seconds, then 160 a second for
ten, then quiet again:

```
   phase  requests       p50       p95       p99
  before       801   120.3ms   122.3ms   127.1ms
   spike      1515   739.9ms  1491.6ms  1890.9ms
   after       798   120.1ms   127.4ms   470.6ms
```

The median recovers completely the moment the spike ends. The tail does not: p99 is still
471 ms through the whole quiet period afterwards, against 127 ms before, because the queue
the spike built is still draining through requests that arrived after it. Ten seconds of
overload is paid for over the following twenty by callers who had nothing to do with it.

**A degraded upstream** — the mock restarted with a 20% failure rate, and a tenth of
requests asking it to take three seconds — is the scenario the gateway comes out of best:

```
                        requests   failed  unexpected  overhead p99
      healthy upstream      1801     0.0%           0       15-20ms
     degraded upstream      1800    11.1%           0       15-20ms

     200      1601    88.9%   served
     429       198    11.0%   rate limited, the provider's own, relayed
     503         1     0.1%   upstream overloaded
```

A fifth of upstream calls failed and 11% of requests did; the retry absorbed the difference.
The gateway's own overhead is unchanged at 15–20 ms, which is the point — it did not get
slower because the provider did, and it did not buffer anything waiting. No request ended
without a status. Every refusal carried the provider's own code, so a caller can still tell
a provider problem from a gateway problem by shape alone, which is what the error mapping is
for.

**Warm against cold cache**, the same 400 prompts twice with ten workers, the tenant's
entries deleted first: 9.1 seconds of wall clock against 2.4, and a p50 of 211 ms against
34. That is the single-request cache result holding up under concurrency, and it is the one
thing in this gateway that makes it faster rather than safer.

**One caveat covers all four scenarios.** The load generator, the gateway, the mock, Postgres
and Redis share a laptop, so the scenarios measure this stack and not this gateway alone, and
the numbers above are read with that in mind rather than quoted as a service's capacity.

### Regenerating the numbers

Every figure in [Results](#results) comes from a script in `bench/`, and `bench/run_all.py`
runs all of them in order with the arguments the committed results were produced with:

```sh
uv run python bench/run_all.py tg_...              # ~40 minutes if nothing is skipped
uv run python bench/run_all.py tg_... --dry-run    # the plan, and what would be skipped and why
```

It refuses to produce a row it cannot measure honestly. The semantic cache tier ships off,
the classifier tier ships unset and the limiter ships in memory, so a run against a gateway
without them skips those rows, names the setting that was missing, and leaves the previous
result file alone rather than overwriting it with a number measured under a heading that
would then be false. Configuration is read from `.env`, which is an assumption about a
process it did not start, and the docstring says so.

The load test needs Docker for k6 and a gateway that is actually recording metrics — which
is two settings, not one:

```sh
OTEL_ENABLED=True METRICS_ENDPOINT_ENABLED=True uv run uvicorn tollgate.main:app --port 8000
uv run python bench/load_test.py tg_...                          # all four scenarios
uv run python bench/load_test.py tg_... --scenarios steady burst # or a subset
```

`METRICS_ENDPOINT_ENABLED` serves `/metrics`; `OTEL_ENABLED` is what installs the meter
provider that puts the gateway's own series on it. With only the first, `/metrics` answers
200 with the process's default Python metrics and none of the gateway's, and a whole run
completes with every overhead column empty. `load_test.py` therefore checks for the
histogram rather than for the endpoint, and refuses to start without it.

The tenant used for load testing needs no rate limit, or the ceiling measured is the
limiter's rather than the gateway's, and `load_test.py` refuses to run rather than report
one:

```sh
uv run tollgate create-tenant loadtest
uv run tollgate set-limits loadtest --unlimited
uv run tollgate set-budget loadtest --unlimited
uv run tollgate create-key loadtest --name k6
```
