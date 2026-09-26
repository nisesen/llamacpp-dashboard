/* What the page holds, and which window the charts show.

   `state` is the one mutable object every module reads. The helpers here decide
   whether a window comes from the live buffer or stored history, and how
   requests split into workload and probe. */

export const RANGE_SECONDS = { '5m': 300, '15m': 900, '1h': 3600, '6h': 21600, '24h': 86400,
                        '7d': 604800, '14d': 1209600, '30d': 2592000, '90d': 7776000,
                        '1y': 31536000 };
// Running without the host agent. The server marks the page, so this is known
// before anything is built: charts that need the agent's per-request data read
// llama.cpp's own counters instead, and what needs the hardware is not drawn.
export const AGENTLESS = document.documentElement.dataset.agent === 'off';
export const LIVE_MAX = 3600;            // ranges at or below this come from the live buffer
// A prefill rate from a few hundred computed tokens is mostly overhead; the
// depth curve only means something for real prompt work.
export const DEPTH_PREFILL_MIN = 2048;

export const state = {
  points: [], histPoints: null, histRange: null, requests: [], snap: {}, meta: {},
  backfill: null, backfillKey: null,
  requestsRange: null, requestsLoading: false,
  range: '1h', gpuCount: -1, lastTick: 0, connected: false, frozen: false, parked: false,
  // The page (tab), and the end of the window the charts show: null = now.
  // Freezing pins `end`, and both go in the URL, so a link reopens that exact
  // moment. endFromUrl: the window came from a link, not from pressing freeze.
  tab: 'overview', end: null, endFromUrl: false,
  // A zoom: [from, to], set by dragging across any time chart. It overrides
  // the range buttons until zoomed out, and goes in the URL too.
  zoom: null,
};

/* A zoom is fetched as the smallest preset range that covers it, ending
   where it ends: the server buckets any range into ~900 points, so a
   three-hour zoom into a week reads 24-second buckets, not 11-minute ones. */
const zoomSpan = () => state.zoom[1] - state.zoom[0];
export const zoomRange = () =>
  Object.keys(RANGE_SECONDS).find((r) => RANGE_SECONDS[r] >= zoomSpan()) || '1y';

/* Which data a chart window comes from. Short live ranges read the 90-minute
   live buffer; long ranges, and a pinned window the live buffer no longer
   covers, read stored history. The key names what state.histPoints holds. */
export const histKey = () => (state.zoom ? `zoom:${zoomRange()}@${state.zoom[1]}`
  : `${state.range}@${state.end ?? ''}`);
export const liveCovers = (t0) => state.points.length > 0 && state.points[0].ts <= t0 + 5;
export const useHistory = () => (state.zoom
  ? zoomSpan() > LIVE_MAX || !liveCovers(state.zoom[0])
  : RANGE_SECONDS[state.range] > LIVE_MAX
    || (state.end != null && !liveCovers(state.end - RANGE_SECONDS[state.range])));

export function currentPoints() {
  const seconds = RANGE_SECONDS[state.range];
  // History draws its own points or nothing: standing the live buffer in
  // while it loads drew a 30-second axis under a "24h" button.
  if (useHistory()) {
    if (state.histRange !== histKey()) return [];
    return state.zoom ? clipWindow(state.histPoints || [], ...state.zoom) : state.histPoints || [];
  }
  if (state.zoom) return clipWindow(state.points, ...state.zoom);
  const end = state.end ?? Date.now() / 1000;
  const live = state.points.filter((p) => p.ts >= end - seconds && p.ts <= end);
  // Soon after the dashboard started, the buffer covers only part of the
  // window: stored history fills in before it (see loadBackfill).
  const back = state.backfillKey === state.range ? state.backfill : null;
  if (!back?.length) return live;
  const first = live.length ? live[0].ts : Infinity;
  return [...back.filter((p) => p.ts >= end - seconds && p.ts < first), ...live];
}

/* The points inside [t0, t1], plus the nearest one beyond each edge, so a
   zoomed line runs to the edges of the plot instead of stopping short. */
export function clipWindow(pts, t0, t1) {
  let a = 0, b = pts.length;
  while (a < pts.length && pts[a].ts < t0) a++;
  while (b > 0 && pts[b - 1].ts > t1) b--;
  return pts.slice(Math.max(0, a - 1), Math.min(pts.length, b + 1));
}

/* ------------------------------------------------ requests: probe vs work */

// A health-check probe (an external watchdog sending the same small request
// on a timer) can be most of the traffic and would set every headline number.
// When a probe fingerprint is configured it is drawn apart: as a clean
// same-prompt health signal, never as "the workload".
export const probeOn = () => !!state.snap.probe_signature?.enabled;
export function isProbe(r) {
  if (r.probe != null) return !!r.probe;
  const sig = state.snap.probe_signature;
  if (!sig?.enabled) return false;
  return r.gen_tokens === sig.gen_tokens && (r.n_tokens || 0) < sig.max_tokens;
}

/* Requests arrive two ways: /api/requests for the range, and the journal's
   last few completions on every tick. Merge the latter in so the charts move
   between fetches. */
export function mergeLiveRequests(snap) {
  const live = (snap.journal || {}).requests || [];
  if (!live.length) return false;
  const seen = new Set((state.requests || []).slice(0, 200).map((r) => `${r.task}|${r.finished}`));
  let added = false;
  for (const r of live) {
    if (r.finished == null || seen.has(`${r.task}|${r.finished}`)) continue;
    const row = { ...r, cache_similarity: r.pick?.similarity, pick: r.pick?.how };
    row.probe = isProbe(row) ? 1 : 0;
    state.requests.unshift(row);
    added = true;
  }
  if (added) state.requests.sort((a, b) => (b.finished || 0) - (a.finished || 0));
  return added;
}

/* One chart point per completed request, keyed so that a LineChart can carry
   workload and probe as separate series. Keys are only set when they apply,
   so a request mark never breaks a sampled line merged into the same data. */
/* Past the raw request log (RETAIN_DAYS), the per-request charts plot
   hourly trends from the rollups instead: one mark per bucket, the mean of
   the requests in it. */
export const beyondRequestLog = () => {
  const retain = (state.meta.retain_days || 14) * 86400 * 1.01;
  // A zoom is judged by where it starts: into the last few days of a
  // year's view, every request is still on record.
  return state.zoom ? state.zoom[0] < Date.now() / 1000 - retain : RANGE_SECONDS[state.range] > retain;
};

export function requestPoints() {
  if (beyondRequestLog() && state.histRange === histKey() && state.histReq) {
    return state.histReq;
  }
  const minP = minPrefill();
  const out = [];
  for (const r of state.requests || []) {
    if (r.finished == null) continue;
    const p = { ts: r.finished };
    if (isProbe(r)) {
      if (r.decode_tps != null) p.probe_decode = r.decode_tps;
    } else {
      if (r.decode_tps != null && (r.gen_tokens || 0) >= 24) p.wl_decode = r.decode_tps;
      if (r.prefill_tps != null && (r.prompt_tokens || 0) >= minP) p.wl_prefill = r.prefill_tps;
      if (r.accept_rate != null) p.accept_pct = r.accept_rate * 100;
      const total = (r.n_tokens || 0) - (r.gen_tokens || 0);
      if (total > 0 && r.prompt_tokens != null) {
        p.reuse_pct = Math.max(0, Math.min(100, 100 * (1 - r.prompt_tokens / total)));
      }
    }
    if (Object.keys(p).length > 1) out.push(p);
  }
  return out.reverse();                       // oldest first, like samples
}

export const minPrefill = () => state.snap.probe_signature?.min_prefill_tokens ?? 1024;

export function workloadRows() {
  return (state.requests || []).filter((r) => !isProbe(r));
}

export function domain() {
  if (state.zoom) return [state.zoom[0], state.zoom[1]];
  const seconds = RANGE_SECONDS[state.range];
  if (state.end != null) return [state.end - seconds, state.end];
  const now = Date.now() / 1000;
  const pts = currentPoints();
  if (seconds > LIVE_MAX && pts.length) return [pts[0].ts, Math.max(now, pts[pts.length - 1].ts)];
  return [now - seconds, now];
}


/* Workload requests in view, with the two latencies the journal gives.
   First token = prompt eval time (computed prompt / prefill rate); end to end
   = the slot's launch to release. Neither includes time spent queued before a
   slot was free: llama.cpp does not log a request's arrival. */
export function latencyRows() {
  const [t0, t1] = domain();
  const cold = state.snap.probe_signature?.cold_prefill_tokens ?? 8192;
  const out = [];
  for (const r of state.requests || []) {
    if (isProbe(r) || (r.finished || 0) < t0 || (r.finished || 0) > t1) continue;
    if (!(r.prefill_tps > 0) || r.prompt_tokens == null) continue;
    out.push({ ...r, ttft: r.prompt_tokens / r.prefill_tps, e2e: r.wall_s,
               cold: (r.prompt_tokens || 0) >= cold });
  }
  return out;
}

export function latencyPoints() {
  const lg = (v) => Math.log10(Math.max(v, 0.001));
  return latencyRows().map((r) => {
    const p = { ts: r.finished };
    p[r.cold ? 'lat_cold' : 'lat_warm'] = lg(r.ttft);
    if (r.e2e != null) p.lat_e2e = lg(r.e2e);
    return p;
  }).sort((a, b) => a.ts - b.ts);
}
