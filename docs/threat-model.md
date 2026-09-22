# Threat model

What Tollgate is defending, from whom, and what it does not defend. The architecture diagram is in
the [README](../README.md); the reasoning behind individual decisions is in
[design.md](design.md).

Each entry below is **asset → attack → mitigation → residual risk**. The residual risk sections are
the useful part: a control that is described without its limits is a control nobody can reason
about. Where a mitigation is partial, it says so, with the measured number where one exists.

## Trust boundaries

```
  untrusted        semi-trusted             trusted                    trusted
  ──────────       ────────────             ───────                    ───────
  end users   →    tenant applications  →   Tollgate + its data   →    model provider
  (prompts)        (hold a tenant key)      (holds the provider key)   (Gemini)
```

Four boundaries, and the gateway sits on the third:

1. **End user → tenant application.** Outside Tollgate's control. Prompt content arriving from a
   tenant's own users is treated as hostile input, which is what input detection is for.
2. **Tenant → Tollgate.** A tenant is authenticated but not trusted. It may be hostile to other
   tenants, and it may be a compromised account.
3. **Tollgate → its datastores.** Postgres and Redis are trusted to store what they are given and
   are assumed reachable only from the gateway's security group.
4. **Tollgate → provider.** The provider is trusted to be who it says it is (TLS) but not trusted
   to produce safe content, which is what output scanning is for.

### Assumed, not defended

- **The operator is trusted.** Anyone with the AWS role, a shell in the task, or database
  credentials defeats everything below. No insider threat modelling.
- **The AWS control plane, Neon and Upstash are trusted** to enforce their own access controls.
- **Volumetric DDoS is the load balancer's problem**, not the gateway's.
- **Availability of the provider is not a security property.** A provider outage is an outage.

## Summary

| # | Threat | Strongest control | Residual risk |
|---|---|---|---|
| 1 | Provider credential stolen | Secrets Manager + `SecretStr` + header allowlist | No rotation automation; one key for all tenants |
| 2 | Tenant key stolen | 256-bit random, stored hashed, revocable | No expiry, no anomaly detection; valid until revoked |
| 3 | Cross-tenant cache access | `tenant_id` in the filter *and* in the hash | Defeated by direct database access |
| 4 | Prompt injection | Regex baseline + pinned ONNX classifier | **Recall 0.666**; indirect injection unmeasured; monitor by default |
| 5 | Sensitive data in a response | 15 rules + stream hold-back in block mode | Monitor mode detects after delivery; regex-bound |
| 6 | Malicious or oversized request | Auth, rate limits, layered timeouts | **No request body size cap**; queues rather than sheds |
| 7 | Token-consumption abuse | Buckets in Redis + budget reserved pre-call | No default budget; limits fail open |
| 8 | Telemetry leaking prompts | Hand-written spans + canary test | Tenant names and spend do leave as labels |
| 9 | Compromised or spoofed upstream | TLS verified, redirects not followed | No pinning; content is trusted to be correct |
| 10 | Redis compromise | Nothing in it is a source of truth | Write access grants free spend or denial of service |
| 11 | Database compromise | No prompt text stored; keys only hashed | Responses stored whole; **embeddings are lossy prompts** |
| 12 | Replayed request | Metered, priced and recorded every time | **No nonce or idempotency key** — bounded, not prevented |

---

## 1. The provider credential is stolen

**Asset.** `GEMINI_API_KEY` — unmetered spend on the account behind it, and the reason the gateway
exists at all.

**Attack.** Read it out of a task definition, an environment dump, a log line, an exception
traceback or an exported trace. Or be handed it: a tenant that was ever given the upstream key
directly makes every other control moot.

**Mitigation.** The key lives in Secrets Manager and is injected at task start by the execution
role, so it is never in the task definition body that `describe-task-definition` returns. It is
held as a Pydantic `SecretStr`, so interpolating it into a log line or an exception yields
`**********`. Tenants authenticate with their own keys and never see it. On the way upstream the
tenant's key is removed and only an allowlist of four headers is forwarded
(`content-type`, `accept`, `user-agent`, `x-goog-api-client`); a `?key=` query parameter on the
inbound request is stripped. Spans record `url.path`, never `url.full`.

**Residual risk.** Anyone with the AWS role or a shell in the task reads it directly. There is one
provider key for every tenant, so a single leak is a full compromise of upstream spend, and
rotation is manual: update the secret and redeploy. Nothing detects the key being used from
somewhere other than the gateway.

## 2. A tenant key is stolen

**Asset.** One tenant's quota, monthly budget and cache.

**Attack.** The key leaks from client code, a CI log, a screenshot or a shared `.env`, and is used
directly against the public endpoint.

**Mitigation.** Keys are `tg_` plus `secrets.token_urlsafe(32)` — 256 bits of entropy, not
guessable. Only a SHA-256 of the key is stored, so a database dump yields nothing usable, and the
plaintext is displayed once at creation. A short prefix identifies a key in logs and in
`tollgate list-keys` without revealing it. A tenant may hold several live keys, so rotation needs
no downtime, and `tollgate revoke-key` takes effect on the next request. The damage a stolen key
can do is bounded by that tenant's rate limit and monthly budget, and every request records its
`api_key_id`, so the blast radius is reconstructable from the ledger.

**Residual risk.** Keys do not expire, there is no IP allowlist and nothing looks for anomalous
use. A stolen key works until a human notices and revokes it, spending up to the budget in the
meantime. The hash is unsalted SHA-256 — appropriate for a 256-bit random secret, where brute
force is not the threat, but it would be the wrong choice for anything user-chosen.

## 3. One tenant reads another's cached answers

**Asset.** Another tenant's generated responses, and — by timing — the questions they asked.

**Attack.** Two routes. Send a competitor's likely prompt and measure the reply: a hit returns in
14.6 ms and a miss in 122.5 ms, which is a clean oracle for "has anyone recently asked this?".
Or rely on a lookup somewhere forgetting its tenant filter and returning a colliding key.

**Mitigation.** `tenant_id` is both a column the lookup filters on *and* part of the hashed key
material, so two tenants asking an identical question produce different keys and cannot collide
even if a future query drops the `WHERE`. The semantic tier's vector search is filtered the same
way. There is no configuration option to share a cache between tenants, because a setting is one
mistake away from being switched on. `test_one_tenant_cannot_be_served_another_tenants_answer`
sends the same prompt as two tenants and asserts the second still reaches the provider;
`test_a_tenant_cannot_see_another_tenants_spend` does the equivalent for usage.

**Residual risk.** All of this is enforced inside the gateway. An attacker with direct database
access reads every tenant's entries regardless — see threat 11.

## 4. Prompt injection

**Asset.** The tenant application's behaviour: its system instruction, its tool calls, and the
assumption that user content is data rather than instructions.

**Attack.** Content that instructs the model to ignore its instructions, disclose its system
prompt, or take an action on the attacker's behalf.

**Mitigation.** Two tiers on the way in: a regex baseline that reads every byte, and a pinned ONNX
classifier whose inference budget is spent window by window so a long prompt is inspected rather
than truncated — "put the payload at the end" is the cheapest evasion there is. Policy is per
tenant: `off`, `monitor` or `block`. Verdicts, rule ids and scores go to the ledger and the
dashboard. A refusal names nothing it found, because naming the rule hands an attacker an oracle:
send a prompt, read the rule, reword until nothing fires.

**Residual risk.** This is the weakest control in the system and the numbers say so. Measured
recall is **0.666 at 0.938 precision** against the committed corpus — a third of the injections in
it get through. The corpus is direct jailbreaks from two public datasets; it contains **no indirect
injection** (hostile content arriving through a retrieved document) and **no exfiltration
payloads**, so performance against those is not measured at all and should not be assumed.
Detection fails open by design: a bug in a rule must not become an outage. The default mode is
`monitor`, so a tenant that changes nothing gets recording and no blocking.

## 5. Sensitive information leaves in a response

**Asset.** Credentials and personal data in model output, whether regurgitated from context or
produced under an injected instruction.

**Attack.** Get the model to emit a key or a customer record, and read it from the response.

**Mitigation.** Fifteen rules over response text: eleven credential formats — including Tollgate's
own tenant key format and its provider key's, the two that are certainly credentials and should
never appear at all — and four kinds of personal data, with Luhn on card numbers and issuance rules
on social security numbers, because cheap arithmetic removes whole classes of false positive. In
`block` mode the relay runs `DETECTION_STREAM_HOLDBACK_BYTES` behind the upstream, so a credential
found inside the unreleased window never leaves and the stream is cut with an error event. A
response with findings is never cached, so one bad answer does not become a permanent one. Findings
are recorded by rule id and the matched text is never quoted anywhere.

**Residual risk.** In `monitor` mode — the default — scanning happens after delivery: the finding
is a record, not a containment. Even in `block` mode, a credential that appears earlier than the
hold-back window has already gone; the window is the length of leak that can still be caught, and
a kilobyte is chosen because a private key announces itself in its first forty bytes. The rules are
pattern-based, so a novel credential format or paraphrased personal data passes. Nothing is ever
redacted — a finding stops a response, it never edits one.

## 6. Malicious or oversized requests

**Asset.** Gateway availability, on a 0.25 vCPU / 512 MB task.

**Attack.** A very large body, deeply nested JSON, or many slow concurrent requests.

**Mitigation.** The load balancer terminates TLS and applies its own idle timeout. A request is
authenticated before anything expensive happens, and an unauthenticated one costs a key hash and a
database lookup. Per-tenant token buckets cap request rate. Outbound calls carry separate connect,
read, write and pool timeouts, and a single deadline covers the whole exchange including retries,
so a slow upstream cannot pin a worker indefinitely.

**Residual risk.** Two real gaps. `await request.body()` reads the entire request body into memory
with **no configured size cap and no 413**, so a large enough authenticated body is memory pressure
on a small task. And the load test showed the gateway **queues rather than sheds**: above its
throughput ceiling overhead climbs and latency degrades for every caller, with nothing refused on
the way. A concurrency limit that returned 503 early would convert a capacity problem back into a
bounded one.

## 7. Abuse through token consumption

**Asset.** Money — the tenant's budget, and the account behind the provider key.

**Attack.** An authenticated tenant loops, retries aggressively, or asks for enormous
`maxOutputTokens` on every call.

**Mitigation.** A token bucket per tenant, held in Redis with an atomic Lua script so several tasks
share one bucket rather than each refilling its own — the difference is measured: in-process
counters drift to **+388%** of the permitted rate at five tasks, Redis stays at **0%** error at any
count. A monthly budget in integer micro-cents is checked *before* the upstream call. Because a
streamed response's cost is unknown when it starts, an estimate is reserved from the request's own
ceiling and reconciled at settlement, and reservations are leases with an expiry so a gateway
killed mid-stream does not strand a tenant's budget. Out of budget returns 402, not 429, because
retrying will not help until the month turns over. The ledger is append-only and priced at the
version in force when the row was written.

**Residual risk.** A budget has no default: a tenant created without `set-budget` is unlimited, and
that is deliberate — there is no honest number to invent for what a customer agreed to pay — but it
means the safe configuration is opt-in. The reservation overshoots by **14x at the median**, so a
tenant can be refused while genuinely under its spend. Rate limits fail open if Redis is
unreachable, which is the right trade for availability and the wrong one for an attacker who can
take Redis down.

## 8. Telemetry leaks prompts

**Asset.** Prompt and response text, which leaves the building on every request once traces are
exported.

**Attack.** Content reaches a span attribute, a metric label or a log line, and is then shipped to
a third-party backend where it is outside the gateway's controls entirely.

**Mitigation.** Spans are written out by hand rather than installed.
`opentelemetry-instrumentation-fastapi` would produce a server span for free and record `url.full`
with it — which carries the query string, which is exactly where the Gemini SDK is happy to put an
API key. The spans here record `url.path`. `tests/test_telemetry.py` sends a canary string through
the gateway three ways — with the key in the query string, with a response full of fake secrets,
and once as a stream — then searches every span, every metric label and every log line for it. The
same test pins `httpx` to WARNING, because httpx logs every request URL at INFO and the gateway
uses httpx to reach the provider. `/metrics` is off in production, where the load balancer is
public.

**Residual risk.** The canary test proves the paths it exercises, not every path a future code
change could add. And telemetry is not empty of sensitive data by a broader definition: tenant
names and their per-tenant spend are metric labels, and they do go to Grafana Cloud. That is the
reason `/metrics` is disabled in production — but it is on by default in local development.

## 9. The upstream is compromised or impersonated

**Asset.** Everything on the return path, and the provider key that is attached to every outbound
call.

**Attack.** Intercept the connection to the provider, or point the gateway at a host the attacker
controls and collect the key on the first request.

**Mitigation.** httpx verifies TLS certificates by default and nothing in this codebase disables
it. Redirects are not followed, so a `302` cannot silently move an authenticated, key-bearing call
to another host. Output scanning runs on every response regardless of where it came from, and a
response with findings is never cached.

**Residual risk.** No certificate pinning, so a trusted-root compromise is not covered. The
provider is trusted for *content*: the scanner detects credential and PII shapes, not a subtly
wrong or manipulated answer. `UPSTREAM_BASE_URL` is ordinary configuration, so anyone who can
change the task's environment can redirect every request and every provider-key-bearing call with
it — which collapses this threat into threat 1.

## 10. Redis is compromised

**Asset.** Rate-limit buckets, month-to-date spend counters and in-flight budget reservations —
`tollgate:rl:*`, `tollgate:spend:*`, `tollgate:reserved:*`. No prompt or response content is ever
written to Redis.

**Attack.** Read or write the keyspace directly.

**Mitigation.** Nothing in Redis is a source of truth. Every key is a cache of the ledger or
in-flight bookkeeping, each with a TTL, which is what makes it safe to run Redis with eviction
enabled at all. Postgres holds the authoritative ledger, so losing Redis costs precision in budget
enforcement rather than enforcement itself, and counters reconcile against the ledger.

**Residual risk.** Write access is a real capability: raising a tenant's spend counter denies them
service, and lowering it grants free spend until the next reconciliation; draining a bucket denies
service outright. Read access discloses tenant identifiers, request rates and spend magnitudes —
traffic metadata, not content.

## 11. The database is compromised

**Asset.** The append-only ledger, cache entries, API key hashes, tenants and prices.

**Attack.** Obtain the connection string or exploit the database directly, then dump it.

**Mitigation.** API keys are stored only as SHA-256 of a 256-bit random value, so a dump yields no
usable credential. **No prompt text is stored anywhere in the schema** — a request appears only as
`cache_key`, a SHA-256 over its normalised form, which cannot be read back, so a dump of
`cache_entries` does not reveal what anybody asked. The connection string lives in Secrets Manager
and never in a task definition. Neon enforces TLS.

**Residual risk.** Two things a dump *does* disclose, and they deserve stating plainly. First,
`cache_entries.response` holds the provider's response object whole — that is the point of a cache
— so every cached answer is readable. Second, when the semantic tier is enabled,
`cache_entries.embedding` holds the prompt as the embedding model saw it. "No prompt text is
stored" is true of text and misleading about meaning: an embedding is a lossy, partially invertible
representation of the prompt, and that column should be treated as prompt-derived data rather than
as an opaque index. The ledger additionally discloses per-tenant spend, volumes and usage patterns
over time.

## 12. Replayed requests

**Asset.** Budget, and the integrity of the usage ledger as a billing record.

**Attack.** Capture an authenticated request and send it again, repeatedly.

**Mitigation.** TLS end to end means capture requires an already-compromised client or a leaked
key, at which point threat 2 is the live problem. Every replay is authenticated, metered, priced
and written to the ledger, so it costs the tenant and is visible afterwards rather than being
invisible. A replay of a cacheable request is served from the cache, billed at zero, and recorded
as a hit.

**Residual risk.** There is **no nonce, no timestamp validation and no idempotency key**. A
replayed request is indistinguishable from the same request legitimately sent twice, which clients
do. Replay is therefore bounded by the rate limit and the budget rather than prevented. This is a
direct consequence of byte-compatibility with the provider's API: requiring an extra signed header
would break the property that adoption is a base URL change and the official SDK keeps working.
Accepting replay is the price of that, and it is a trade rather than an oversight.

---

## Known gaps, worst first

An honest reading of the twelve above. These are properties of the system as it stands, stated so
that a reader does not have to infer them:

1. **Injection recall is 0.666, and indirect injection is unmeasured.** The measured number is
   published next to the baseline precisely so it cannot be mistaken for a solved problem.
2. **No request body size cap.** `await request.body()` will read whatever it is given.
3. **Monitor is the default in both directions**, so an unconfigured tenant is observed, not
   protected.
4. **No replay protection**, by deliberate trade against SDK compatibility.
5. **One provider key for all tenants, rotated by hand.**
6. **Tenant keys never expire** and nothing watches for anomalous use.
7. **Embeddings are prompt-derived data** and are not currently treated as more sensitive than the
   rest of the cache row.
