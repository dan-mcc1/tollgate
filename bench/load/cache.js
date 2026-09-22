// Cache-warm against cache-cold: the same work twice, over an identical pool of prompts.
//
// The runner clears the tenant's entries, runs this once (every prompt new, so every
// lookup misses and the cache fills), then runs it again with the same `RUN_ID` (every
// prompt already stored, so every lookup hits). Nothing about the gateway changes
// between the two, and neither does the work: the same prompts in the same order.
//
// This one scenario is closed rather than open - a fixed number of iterations shared
// between a fixed number of workers - and that is deliberate. The other three are
// looking for a ceiling, which only an arrival rate can find. This one is comparing two
// runs of identical work, where the honest measure is how long the work took and what
// each request cost, and an arrival rate would answer a different question: a warm cache
// simply sustains a higher one. Both runs are given the same number of workers, so the
// difference in wall clock is the difference the cache made.
//
// `RUN_ID` is fixed by the runner rather than taken from the clock, because a prompt that
// differed between the two runs would miss in both and the comparison would be of a cold
// cache against a cold cache.

import { scenario } from 'k6/execution';
import { call, KEY, summaryHandler, TREND_STATS } from './lib/tollgate.js';

const POOL = Number(__ENV.POOL || 400);
const VUS = Number(__ENV.VUS || 10);
const PHASE = __ENV.PHASE || 'cold';
const RUN = __ENV.RUN_ID || 'fixed';

// Dull, varied in length, and stable for a given RUN_ID. What is being measured is a
// hit against a miss, so the only thing the text has to do is be the same both times.
const TOPICS = [
  'what is a reverse proxy',
  'explain connection pooling to a new engineer',
  'how does a token bucket differ from a sliding window',
  'why store a hash of an API key rather than the key',
  'summarise the tradeoffs of server-sent events against websockets',
  'what does backpressure mean in a streaming pipeline',
  'when should a service fail open rather than closed',
  'describe an append-only ledger and why it is not updated in place',
];

export const options = {
  summaryTrendStats: TREND_STATS,
  scenarios: {
    replay: {
      executor: 'shared-iterations',
      vus: VUS,
      iterations: POOL,
      maxDuration: '10m',
    },
  },
  thresholds: {
    tollgate_unexpected: ['count == 0'],
    // The run is only meaningful if the cache did what the phase says it did. A cold run
    // that hits, or a warm run that misses, means the clear or the pool went wrong, and
    // the comparison that follows would be nonsense.
    tollgate_cache_hit: [PHASE === 'warm' ? 'rate>0.95' : 'rate<0.05'],
  },
};

export function setup() {
  if (!KEY) {
    throw new Error('TOLLGATE_KEY is not set: nothing would authenticate.');
  }
}

export default function () {
  const i = scenario.iterationInTest;
  const prompt = `${TOPICS[i % TOPICS.length]}? (cache pool ${RUN}, entry ${i})`;
  call(prompt, { tags: { scenario: 'cache', phase: PHASE } });
}

export const handleSummary = summaryHandler(`cache_${PHASE}`, {
  phase: PHASE,
  pool: POOL,
  vus: VUS,
  run_id: RUN,
});
