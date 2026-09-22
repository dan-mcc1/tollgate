// A burst: quiet, then a sudden multiple of the quiet rate, then quiet again.
//
// The question is not what the percentiles look like during the spike - `steady.js`
// answers that at any rate you like, and more precisely. It is what happens *after*.
// A gateway that queues a spike instead of shedding it stays slow long after the spike
// has gone, because the queue it built is still draining, and the caller sees a service
// that never recovered from something that lasted ten seconds. So the run is three
// phases and the number that matters is the third against the first.
//
// The phases are separate scenarios rather than stages of one ramp, because k6 only
// breaks a metric down by tag where a threshold names that tag. Thresholds below exist
// to create those sub-metrics; none of them is meant to fail a run.

import { scenario } from 'k6/execution';
import { call, KEY, summaryHandler, TREND_STATS } from './lib/tollgate.js';

const BASE = Number(__ENV.RATE || 10);
const MULTIPLE = Number(__ENV.SPIKE || 10);
const QUIET = Number(__ENV.QUIET_SECONDS || 20);
const SPIKE_SECONDS = Number(__ENV.SPIKE_SECONDS || 10);
const RUN = __ENV.RUN_ID || `${Date.now()}`;

// Roughly what a healthy request costs end to end against the mock: its 100 ms plus
// the gateway's own. Only used to size the worker pool, so about right is enough.
const SERVICE_SECONDS = 0.15;

function phase(name, rate, startSeconds, seconds) {
  return {
    executor: 'constant-arrival-rate',
    rate: rate,
    timeUnit: '1s',
    startTime: `${startSeconds}s`,
    duration: `${seconds}s`,
    // Sized from Little's law, with headroom for the spike phase, where latency climbs
    // by an order of magnitude and each worker is therefore held far longer.
    preAllocatedVUs: Math.max(20, Math.ceil(rate * SERVICE_SECONDS * 3)),
    maxVUs: Math.max(60, Math.ceil(rate * SERVICE_SECONDS * 20)),
    tags: { phase: name },
    exec: 'send',
  };
}

export const options = {
  summaryTrendStats: TREND_STATS,
  scenarios: {
    before: phase('before', BASE, 0, QUIET),
    spike: phase('spike', BASE * MULTIPLE, QUIET, SPIKE_SECONDS),
    after: phase('after', BASE, QUIET + SPIKE_SECONDS, QUIET),
  },
  thresholds: {
    // These three exist so that k6 reports the three phases separately. The bounds are
    // loose on purpose: the spike is allowed to be slow, and the run is not a pass/fail.
    'http_req_duration{phase:before}': ['p(99)<60000'],
    'http_req_duration{phase:spike}': ['p(99)<60000'],
    'http_req_duration{phase:after}': ['p(99)<60000'],
    'http_reqs{phase:before}': ['count>0'],
    'http_reqs{phase:spike}': ['count>0'],
    'http_reqs{phase:after}': ['count>0'],
  },
};

export function setup() {
  if (!KEY) {
    throw new Error('TOLLGATE_KEY is not set: nothing would authenticate.');
  }
}

export function send() {
  call(`load burst ${RUN} ${scenario.name} ${scenario.iterationInTest}`, {
    tags: { scenario: 'burst' },
  });
}

export const handleSummary = summaryHandler('burst', {
  base_rate: BASE,
  spike_rate: BASE * MULTIPLE,
  quiet_seconds: QUIET,
  spike_seconds: SPIKE_SECONDS,
});
