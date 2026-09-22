// Steady load: the gateway's cost and capacity when nothing is going wrong.
//
// This is also the script the runner sweeps to find the throughput ceiling, by invoking
// it once per arrival rate rather than ramping inside a single run. A ramp inside one run
// gives one set of percentiles smeared across every rate it passed through, and the
// interesting thing about a ceiling is exactly where the percentiles stop being flat.
// One run per rate keeps each row honest at the cost of a longer sweep.
//
// Every prompt is unique. A repeated one would be served from the cache, and this
// scenario would then be measuring how fast the gateway can answer without doing any
// work - which is a real number, and the one `cache.js` is for.

import { scenario } from 'k6/execution';
import { call, KEY, summaryHandler, TREND_STATS } from './lib/tollgate.js';

const RATE = Number(__ENV.RATE || 20);
const DURATION = __ENV.DURATION || '60s';
const STREAM = (__ENV.STREAM || 'false') === 'true';
const RUN = __ENV.RUN_ID || `${Date.now()}`;

// Roughly what a healthy request costs end to end against the mock: its 100 ms plus
// the gateway's own. Only used to size the worker pool, so about right is enough.
const SERVICE_SECONDS = 0.15;

export const options = {
  summaryTrendStats: TREND_STATS,
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      // Enough workers that the arrival rate is never limited by having none free. k6
      // warns if it runs out, and that warning invalidates the run: it means the offered
      // rate was lower than the one printed at the top of the result file.
      //
      // Sized from Little's law rather than from a flat multiple of the rate. Requests in
      // flight is the arrival rate times how long each is held, which against a 100 ms
      // mock is a small number even at high rates; the ceiling is allowed a lot of
      // headroom because latency triples once the gateway saturates, and that is exactly
      // the row that must not be starved of workers. Surplus workers are not free - they
      // open connections, and enough of them through Docker's NAT makes the generator
      // fail to dial before the gateway fails to answer.
      preAllocatedVUs: Math.max(20, Math.ceil(RATE * SERVICE_SECONDS * 3)),
      maxVUs: Math.max(60, Math.ceil(RATE * SERVICE_SECONDS * 15)),
    },
  },
  thresholds: {
    // Deliberately not a latency threshold. At rates above the ceiling this scenario is
    // *supposed* to produce terrible percentiles, and a threshold that failed the run
    // there would make the sweep unable to find the thing it is looking for.
    tollgate_unexpected: ['count == 0'],
  },
  discardResponseBodies: false,
};

export function setup() {
  if (!KEY) {
    throw new Error('TOLLGATE_KEY is not set: nothing would authenticate.');
  }
}

export default function () {
  call(`load steady ${RUN} ${scenario.iterationInTest}`, {
    stream: STREAM,
    tags: { scenario: 'steady', rate: String(RATE) },
  });
}

export const handleSummary = summaryHandler('steady', {
  rate: RATE,
  duration: DURATION,
  streamed: STREAM,
});
