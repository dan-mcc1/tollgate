// A degraded upstream: the same steady load, against a provider that is failing some of
// the time and slow for some of the rest.
//
// The runner restarts the mock with `MOCK_ERROR_RATE` before this runs, because random
// failure is what a degraded provider actually looks like and it is the only kind the
// gateway's retry can do anything about. A `[[mock:error=429]]` directive would fail
// every attempt identically - the retry re-sends the same body, directive included - so
// it models a request that is permanently rejected rather than a provider having a bad
// afternoon. A share of requests carry `[[mock:slow=...]]` on top of that, because the
// failure mode worth finding here is not the errors. It is what a long tail of slow
// requests does to a connection pool that is shared with the fast ones.
//
// Three things are being watched, none of them a pass or a fail:
//
//   * what the caller gets. Retries are supposed to absorb a share of the 429s; the rest
//     pass through with the provider's own status, which is the design.
//   * whether 503 `gateway_overloaded` appears. That is the gateway running out of pool,
//     not the provider, and it is the point where the degradation became the gateway's
//     problem rather than something it was passing along.
//   * whether overhead holds. Scraped from `/metrics` by the runner, not from here. A
//     gateway whose own cost climbs while it waits on a slow provider is buffering
//     something it should not be.

import { scenario } from 'k6/execution';
import { call, KEY, summaryHandler, TREND_STATS } from './lib/tollgate.js';

const RATE = Number(__ENV.RATE || 20);
const DURATION = __ENV.DURATION || '60s';
const SLOW_SHARE = Number(__ENV.SLOW_SHARE || 0.1);
const SLOW_MS = Number(__ENV.SLOW_MS || 3000);
const RUN = __ENV.RUN_ID || `${Date.now()}`;

// Roughly what a healthy request costs end to end against the mock: its 100 ms plus the
// gateway's own. Only used to size the worker pool, so it wants to be about right rather
// than exact.
const SERVICE_SECONDS = 0.15;

// Little's law. Requests in flight is the arrival rate times how long each one is held,
// and the slow tenth is held for SLOW_MS as well as its service time.
const CONCURRENCY = Math.max(
  10,
  RATE * ((1 - SLOW_SHARE) * SERVICE_SECONDS + SLOW_SHARE * (SLOW_MS / 1000 + SERVICE_SECONDS)),
);

// Everything the gateway may legitimately answer here. 429 and 503 are the provider's,
// relayed; 502 and 504 are the gateway saying it could not reach or could not wait for
// the provider. Anything outside this set is a bug and increments `tollgate_unexpected`.
const EXPECTED = [200, 429, 500, 502, 503, 504];

export const options = {
  summaryTrendStats: TREND_STATS,
  scenarios: {
    degraded: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      // Sized from the concurrency this mix actually implies, not from the rate. The
      // first version used a flat multiple of the rate, which at 40/s asked k6 to hold
      // 320 workers for about 18 requests' worth of real concurrency - and the surplus
      // workers opened enough connections through Docker's NAT that k6 started failing
      // to dial the gateway at all. Those showed up as responses with no status, which
      // reads exactly like the gateway dropping requests. It was the load generator.
      preAllocatedVUs: Math.ceil(CONCURRENCY * 3),
      maxVUs: Math.ceil(CONCURRENCY * 8),
    },
  },
  thresholds: Object.assign(
    {
      tollgate_unexpected: ['count == 0'],
    },
    // One per status, so k6 breaks the counter down and the report can print what the
    // caller actually got rather than a single "failed" percentage. None of these can
    // fail; they exist only to create the sub-metrics. 0 is k6's code for a request that
    // never received a status at all.
    Object.fromEntries(
      [0, 200, 400, 401, 402, 403, 429, 500, 502, 503, 504].map((status) => [
        `tollgate_status{status:${status}}`,
        ['count>=0'],
      ]),
    ),
  ),
};

export function setup() {
  if (!KEY) {
    throw new Error('TOLLGATE_KEY is not set: nothing would authenticate.');
  }
}

export default function () {
  const i = scenario.iterationInTest;
  // Deterministic rather than random, so two runs of the same shape offer the same mix.
  const slow = SLOW_SHARE > 0 && i % Math.round(1 / SLOW_SHARE) === 0;
  const directive = slow ? ` [[mock:slow=${SLOW_MS}]]` : '';
  call(`load degraded ${RUN} ${i}${directive}`, {
    expected: EXPECTED,
    tags: { scenario: 'degraded', slow: String(slow) },
    timeout: '120s',
  });
}

export const handleSummary = summaryHandler('degraded', {
  rate: RATE,
  duration: DURATION,
  slow_share: SLOW_SHARE,
  slow_ms: SLOW_MS,
});
