// Shared pieces for the four load scenarios. What every scenario needs: where the
// gateway is, a tenant key, a request body the gateway will treat as ordinary traffic,
// and one place that decides what counts as a failure.
//
// Three choices are made here rather than per scenario, because getting any of them
// wrong would make every number on the page wrong in the same direction.
//
// **Load is offered at an arrival rate, never by a fixed pool of virtual users.** A
// closed model - N workers, each sending the next request when the last one comes back -
// cannot overload anything. When the service slows, the generator slows with it, and the
// throughput it reports is a measure of its own patience rather than of the service's
// capacity. `constant-arrival-rate` keeps sending at the rate asked for whether or not
// replies come back, which is the only way a ceiling shows up at all, and the queue
// growing in front of a saturated gateway is the thing being measured.
//
// **k6's timings are not the gateway's overhead.** `http_req_duration` includes the mock
// holding the request for 100 ms, so it is dominated by the upstream. Subtracting two
// distributions to remove it is wrong at the tail, which is the end anyone cares about.
// The gateway already records total-minus-upstream per request, so overhead comes from
// scraping `/metrics` either side of the run and diffing the histogram, and
// k6 is left to measure what only the client can see: throughput, errors, and the
// latency a caller actually experienced.
//
// **A non-200 is not automatically a failure.** A degraded upstream is supposed to
// produce 502s, and a rate-limited tenant is supposed to produce 429s. Each scenario
// says which statuses it expects; anything else is counted as unexpected, and that
// counter is what the thresholds watch.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

export const GATEWAY = __ENV.TOLLGATE_URL || 'http://host.docker.internal:8000';
export const UPSTREAM = __ENV.UPSTREAM_URL || 'http://host.docker.internal:8001';
export const KEY = __ENV.TOLLGATE_KEY || '';
export const MODEL = __ENV.TOLLGATE_MODEL || 'gemini-3.7-flash';

// Requests naming no temperature are served at the provider's default of 1.0, which the
// gateway declines to cache on purpose. A load test that forgot this would measure a
// 0% hit rate and conclude the cache was broken.
export const TEMPERATURE = 0.0;

export const CACHE_HEADER = 'x-tollgate-cache';

// k6's default trend statistics stop at p(95), and the number this project is judged on
// is a p99. Set on every scenario, because a percentile missing from the summary is
// indistinguishable, to the runner reading the JSON, from a percentile of zero.
export const TREND_STATS = ['min', 'med', 'avg', 'p(90)', 'p(95)', 'p(99)', 'max'];

// Counted rather than checked, so a scenario can expect some of them.
export const unexpected = new Counter('tollgate_unexpected');
// Every status the caller saw, by status. k6 reports 0 for a request that never got one
// - a timeout, a reset, a connection it could not open - and lumping that in with the
// gateway's own error codes would hide the difference between "refused" and "no answer".
export const statuses = new Counter('tollgate_status');
export const refused = new Counter('tollgate_refused');
export const upstreamErrors = new Counter('tollgate_upstream_errors');
export const cacheHits = new Rate('tollgate_cache_hit');
export const hitLatency = new Trend('tollgate_hit_latency', true);
export const missLatency = new Trend('tollgate_miss_latency', true);

export function generatePath(stream) {
  const method = stream ? 'streamGenerateContent' : 'generateContent';
  const suffix = stream ? '?alt=sse' : '';
  return `/v1beta/models/${MODEL}:${method}${suffix}`;
}

// A body the gateway sees as ordinary traffic. `maxOutputTokens` is set because the
// budget reservation is taken from it, and leaving it out would have every request
// reserve the model's ceiling.
export function body(prompt, extra) {
  return Object.assign(
    {
      contents: [{ role: 'user', parts: [{ text: prompt }] }],
      generationConfig: { temperature: TEMPERATURE, maxOutputTokens: 256 },
    },
    extra || {},
  );
}

export function headers() {
  return { 'x-goog-api-key': KEY, 'Content-Type': 'application/json' };
}

// One request, classified. `expected` is the set of statuses this scenario considers a
// correct answer; everything else increments `tollgate_unexpected`, which is what the
// thresholds are written against.
export function call(prompt, options) {
  const opts = options || {};
  const expected = opts.expected || [200];
  const stream = opts.stream || false;
  const response = http.post(
    `${GATEWAY}${generatePath(stream)}`,
    JSON.stringify(body(prompt, opts.extra)),
    { headers: headers(), tags: opts.tags || {}, timeout: opts.timeout || '90s' },
  );

  const ok = expected.includes(response.status);
  check(response, { 'status was expected': () => ok });
  statuses.add(1, { status: String(response.status) });
  if (!ok) {
    unexpected.add(1, { status: String(response.status) });
  }
  if (response.status === 429 || response.status === 402 || response.status === 403) {
    refused.add(1, { status: String(response.status) });
  }
  if (response.status >= 500) {
    upstreamErrors.add(1, { status: String(response.status) });
  }

  if (response.status === 200) {
    const outcome = response.headers[cacheHeaderName(response)] || 'miss';
    const hit = outcome === 'exact' || outcome === 'semantic';
    cacheHits.add(hit, opts.tags || {});
    (hit ? hitLatency : missLatency).add(response.timings.duration, opts.tags || {});
  }
  return response;
}

// k6 normalises header names to title case, and the gateway sends a lower-case one.
// Looking it up case-insensitively rather than guessing which form arrives.
function cacheHeaderName(response) {
  const wanted = CACHE_HEADER.toLowerCase();
  return Object.keys(response.headers).find((name) => name.toLowerCase() === wanted) || '';
}

// Written next to the summary so a result file records the shape of the run that made
// it, rather than the defaults someone reading it later would assume.
export function describe(extra) {
  return Object.assign({ gateway: GATEWAY, model: MODEL, temperature: TEMPERATURE }, extra || {});
}

// Every scenario writes the same two things: the machine-readable summary the Python
// runner parses, and the human one on stdout so a bare `k6 run` is still useful.
export function summaryHandler(name, meta) {
  return function handleSummary(data) {
    const out = {};
    out[`/results/${name}.json`] = JSON.stringify(
      { scenario: name, meta: describe(meta), metrics: data.metrics },
      null,
      2,
    );
    out.stdout = textSummary(data, name);
    return out;
  };
}

function textSummary(data, name) {
  const lines = [`\n${name}:`];
  for (const metric of ['http_reqs', 'http_req_duration', 'http_req_failed', 'tollgate_unexpected']) {
    const values = (data.metrics[metric] || {}).values;
    if (!values) continue;
    const parts = Object.keys(values).map((k) => `${k}=${round(values[k])}`);
    lines.push(`  ${metric.padEnd(22)} ${parts.join(' ')}`);
  }
  return lines.join('\n') + '\n';
}

function round(value) {
  return typeof value === 'number' ? Math.round(value * 100) / 100 : value;
}
