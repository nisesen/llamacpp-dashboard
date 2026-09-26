/* Getting data in: the WebSocket, and the fetches for history, requests and
   the event markers. */

import { setAnnotations } from './charts.js';
import { charts, occLegend, redrawAll, refreshLegends, tableBuilders } from './chart-setup.js';
import { renderLatency } from './sections/inference.js';
import { renderEvents } from './sections/machine.js';
import { LIVE_MAX, RANGE_SECONDS, clipWindow, domain, histKey, isProbe, liveCovers, mergeLiveRequests, state, useHistory, zoomRange } from './state.js';
import { dur, el, nf } from './util.js';
import { derive, flatten, render, setLink } from './view.js';

/* ---------------------------------------------------------------- socket */

let ws = null, backoff = 1000;

export function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => { backoff = 1000; state.connected = true; setLink(); };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'init') {
      state.meta = msg.meta || {};
      state.points = (msg.points || []).map(flatten);
      state.snap = msg.snap || {};
      // Stored points carry each card's memory share but not the total the
      // VRAM tile plots; rebuild it from the card sizes, so its spark is
      // whole from the first paint rather than growing in from the right.
      const size = new Map((state.snap.gpus || []).map((g) => [g.idx, g.mem_total]));
      for (const p of state.points) {
        if (p._vram != null || !p.gpus?.length) continue;
        const parts = p.gpus.map((g) => (g.mem_pct != null && size.get(g.idx) ? g.mem_pct / 100 * size.get(g.idx) : null));
        p._vram = parts.every((v) => v != null) ? parts.reduce((a, b) => a + b, 0) / 1024 : null;
      }
      // Legends that depend on configuration (the probe) wait for the snapshot.
      refreshLegends();
      occLegend();
      const last = state.points[state.points.length - 1];
      if (last) derive(state.snap, last);
      render(state.snap, last);
      redrawAll();
      renderEvents();
      loadBackfill();
    } else if (msg.type === 'tick') {
      const snap = mergeKept(msg.snap, msg.kept);
      const p = derive(snap, flatten(msg.point));
      state.points.push(p);
      const newReq = mergeLiveRequests(snap);
      watchAnnotations(snap);
      const cutoff = Date.now() / 1000 - 5400;
      while (state.points.length && state.points[0].ts < cutoff) state.points.shift();
      state.snap = snap;
      // A hidden page keeps its state current but draws nothing; it is drawn
      // once when shown again (see visibilitychange).
      if (!state.frozen && !document.hidden) {
        render(snap, p);
        // A zoom is a fixed window: new samples land outside it.
        if ((RANGE_SECONDS[state.range] <= LIVE_MAX && !state.zoom) || newReq) redrawAll();
        if (newReq) drawRequestCharts();
      }
      // A zoom read from the live buffer moves to stored history once the
      // buffer no longer reaches back to its start.
      if (state.zoom && useHistory() && state.histRange !== histKey()
          && state.histPending !== histKey()) loadHistory();
    }
    state.lastTick = Date.now();
    if (!state.frozen) document.body.classList.remove('stale');
  };

  ws.onclose = () => {
    if (state.parked) return;          // closed on purpose while hidden
    state.connected = false;
    setLink();
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 1.7, 15000);
  };
  ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
}

/* A tick leaves out the sections that have not changed since this page last
   received them and names them in `kept` ('key' or 'key.sub'); they carry
   over from the previous snapshot. */
function mergeKept(snap, kept) {
  if (!kept || !kept.length) return snap;
  const prev = state.snap || {};
  for (const path of kept) {
    const [top, sub] = path.split('.');
    if (sub) snap[top] = { ...(snap[top] || {}), [sub]: prev[top]?.[sub] };
    else snap[top] = prev[top];
  }
  return snap;
}

/* ------------------------------------------------------- event markers */

/* Chart annotations from the event log: a model change is an instant; a
   restart/switch and every alert are spans from raised to cleared. */
function buildAnnotations(events, since, activeKeys) {
  const out = [], open = new Map();
  for (const e of events) {
    if (e.key === 'model_change') {
      out.push({ kind: 'model', ts: e.ts, level: e.level, title: 'Model changed', detail: e.detail });
      continue;
    }
    const kind = e.key === 'transition' ? 'transition' : 'alert';
    if (e.state === 'raised') {
      // Raised again with no clear between: the dashboard restarted while it
      // held. Still the same condition, so still the same span.
      if (open.has(e.key)) continue;
      const a = { kind, ts: e.ts, end: null, level: e.level, title: e.title, detail: e.detail || '' };
      out.push(a);
      open.set(e.key, a);
    } else if (e.state === 'cleared') {
      const a = open.get(e.key);
      if (a) {
        a.end = e.ts;
        if (e.detail) a.detail = a.detail ? `${a.detail} · ${e.detail}` : e.detail;
        open.delete(e.key);
      } else {                                // raised before the range began
        out.push({ kind, ts: since, end: e.ts, level: e.level, title: e.title, detail: e.detail || '' });
      }
    }
  }
  // Still open but no longer true: orphaned by a dashboard restart that never
  // logged the clear. Shown where it began rather than stretched to now.
  for (const [key, a] of open) {
    const live = key === 'transition' ? !!state.snap.transition : activeKeys.has(key);
    if (!live) a.end = a.ts;
  }
  return out;
}

export async function loadAnnotations() {
  const since = Math.floor(domain()[0] - 60);
  try {
    const events = await (await fetch(`/api/events?since=${since}&limit=5000`)).json();
    setAnnotations(buildAnnotations(events, since, new Set((state.snap.alerts || []).map((a) => a.key))));
  } catch { /* transient: keep the last set */ }
}

/* Reload the markers as soon as something they show changes, rather than on
   the next 30 s refresh. */
let annotSig = null;
function watchAnnotations(snap) {
  const sig = [snap.model?.path, !!snap.transition,
    (snap.alerts || []).map((a) => a.key).sort().join()].join('|');
  if (annotSig !== null && sig !== annotSig) loadAnnotations();
  annotSig = sig;
}

/* The page holds the range's requests. After the first fetch it asks only
   for what finished since the newest one it has - overlapping a little, for
   requests stored a moment after they finished - and merges. A range change
   starts over. A 7-day refresh used to re-send 604 KB every 15 s. */
/* The request window: the range's (at least 6 h, for the per-request tiles
   and sparks), or a zoom's own when zoomed. */
const reqRangeKey = () => (state.zoom ? zoomRange()
  : RANGE_SECONDS[state.range] > LIVE_MAX ? state.range : '6h');
const reqEnd = () => (state.zoom ? state.zoom[1] : state.end);
const reqKey = (r) => `${r.task}|${r.finished}`;

export async function loadRequests() {
  const range = reqRangeKey(), end = reqEnd(), key = `${range}@${end ?? ''}`;
  const fresh = state.requestsRange !== key;
  const have = fresh ? [] : state.requests || [];
  const newest = have.reduce((m, r) => Math.max(m, r.finished || 0), 0);
  const url = `/api/requests?range=${range}` + (newest ? `&since=${newest - 30}` : '')
    + (end != null ? `&end=${end}` : '');
  if (fresh) { state.requestsLoading = true; drawRequestCharts(); }
  let rows;
  try {
    rows = await (await fetch(url)).json();
  } catch {
    state.requestsLoading = false;
    return;
  }
  if (`${reqRangeKey()}@${reqEnd() ?? ''}` !== key) return;   // the window changed meanwhile
  const cutoff = (end ?? Date.now() / 1000) - RANGE_SECONDS[range];
  const byKey = new Map(have.map((r) => [reqKey(r), r]));
  for (const r of rows) byKey.set(reqKey(r), r);
  state.requests = [...byKey.values()]
    .filter((r) => (r.finished || 0) >= cutoff)
    .sort((a, b) => (b.finished || 0) - (a.finished || 0));
  state.requestsRange = key;
  state.requestsLoading = false;
  drawRequestCharts();
}

export function drawRequestCharts() {
  const rows = state.requests || [];
  renderLatency();
  for (const id in charts) {
    if (!charts[id].requests) continue;
    const data = charts[id].rows ? charts[id].rows() : rows;
    if (charts[id].timeline) {
      // The timeline shares the page's time range; the scatters do not,
      // because a depth curve wants every sample it can get.
      charts[id].chart.setData(data, domain());
    } else {
      charts[id].chart.setData(data);
    }
    const twin = document.getElementById(id + '-twin');
    if (twin) twin.innerHTML = tableBuilders[id]();
  }
  // Counted over the requests the timeline is actually showing.
  const [t0, t1] = domain();
  const inView = rows.filter((r) => (r.finished || 0) >= t0 && (r.finished || 0) <= t1);
  const probes = inView.filter(isProbe).length;
  const cold = inView.filter((r) => !isProbe(r) && (r.prompt_tokens || 0) >= 8192);
  const coldS = cold.reduce((a, r) => a + (r.prefill_tps ? r.prompt_tokens / r.prefill_tps : 0), 0);
  el('occ-note').textContent = state.requestsLoading ? 'Loading requests for this range…'
    : inView.length
    ? (probes ? `${nf(inView.length - probes)} workload requests and ${nf(probes)} probe requests in this range. `
      : `${nf(inView.length)} requests in this range. `)
      + (cold.length
        ? `${nf(cold.length)} paid for a cold prefill — ${dur(coldS)} spent computing prompts from scratch.`
        : 'None paid for a cold prefill.')
    : 'No completed requests in this range.';
}

/* /api/series as chart points: one per bucket, GPUs flattened in. */
function seriesPoints(data) {
  const s = data.samples;
  const pts = (s.ts || []).map((t, i) => {
    const p = { ts: t };
    for (const k in s) if (k !== 'ts') p[k] = s[k][i];
    return p;
  });
  const byTs = new Map(pts.map((p) => [p.ts, p]));
  for (const idx in data.gpus) {
    const g = data.gpus[idx];
    g.ts.forEach((t, i) => {
      const p = byTs.get(t);
      if (!p) return;
      p[`g${idx}_util`] = g.util[i];
      p[`g${idx}_pwr`] = g.pwr[i];
      p[`g${idx}_hbm`] = g.hbm[i];
      if (g.pwr_max) p[`g${idx}_pwr_max`] = g.pwr_max[i];
      if (g.hbm_max) p[`g${idx}_hbm_max`] = g.hbm_max[i];
      p[`g${idx}_temp`] = g.temp[i];
      p[`g${idx}_mem`] = g.mem_pct[i];
      p[`g${idx}_sm`] = g.sm_clk[i];
    });
  }
  pts.forEach(flatten);
  return pts;
}

/* A live range the buffer does not reach back across yet - the first hour
   after the dashboard started - is filled in from stored history, once per
   range. Without it a restart left the short ranges nearly empty. */
export async function loadBackfill() {
  const key = state.range;
  if (useHistory() || state.zoom || state.backfillKey === key || !state.points.length
      || liveCovers(Date.now() / 1000 - RANGE_SECONDS[key])) return;
  state.backfillKey = key;
  state.backfill = null;
  try {
    const pts = seriesPoints(await (await fetch(`/api/series?range=${key}`)).json());
    if (state.backfillKey !== key) return;
    state.backfill = pts;
  } catch {
    return;
  }
  redrawAll();
}

export async function loadHistory() {
  if (!useHistory()) {
    state.histPoints = null;
    el('range-note').textContent = `live · ${state.meta.poll_interval || 2}s`;
    redrawAll();
    loadBackfill();
    return;
  }
  el('range-note').textContent = 'loading…';
  const key = histKey();
  const range = state.zoom ? zoomRange() : state.range;
  const end = state.zoom ? state.zoom[1] : state.end;
  state.histPending = key;
  try {
    const data = await (await fetch(`/api/series?range=${range}`
      + (end != null ? `&end=${end}` : ''))).json();
    if (histKey() !== key) return;               // superseded by a newer window
    const pts = seriesPoints(data);
    state.histPoints = pts;
    state.histRange = key;
    state.histReq = (data.requests || []).map((r) => {
      const p = { ts: r.ts };
      if (r.wl_decode != null) p.wl_decode = r.wl_decode;
      if (r.probe_decode != null) p.probe_decode = r.probe_decode;
      if (r.wl_prefill != null) p.wl_prefill = r.wl_prefill;
      if (r.accept != null) p.accept_pct = r.accept * 100;
      return p;
    }).filter((p) => Object.keys(p).length > 1);
    const b = data.bucket;
    const shown = state.zoom ? clipWindow(pts, ...state.zoom).length : pts.length;
    el('range-note').textContent = `${shown} pts · ${b >= 3600 ? `${(b / 3600).toFixed(1)}h`
      : `${Math.round(b)}s`} buckets` + (data.source && data.source !== 'raw' ? ` · ${data.source} rollups` : '');
  } catch {
    if (histKey() !== key) return;
    el('range-note').textContent = 'history unavailable';
    state.histPoints = [];
    state.histRange = key;
  } finally {
    if (state.histPending === key) state.histPending = null;
  }
  redrawAll();
}


/* Close the socket while the page is hidden, and reopen it on return. */
export function parkSocket() {
  try { ws.close(); } catch { /* already closed */ }
}

export function resumeSocket() {
  backoff = 1000;
  connect();
}
