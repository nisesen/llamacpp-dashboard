/* The headline tiles along the top of the page, each with its spark. */

import { Sparkline } from './charts.js';
import { swatchBg } from './chart-setup.js';
import { AGENTLESS, minPrefill, state, workloadRows } from './state.js';
import { ago, dur, el, esc, nf } from './util.js';

/* ---------------------------------------------------------------- tiles */

/* Request tiles read the LAST WORKLOAD REQUEST (a configured probe is shown
   beside it, never instead of it), and their sparks are the last 40 workload
   requests rather than a time window that is mostly empty between requests.
   `value(snap, point)` returns the reading; `stale` marks a reading that is
   a past request rather than something happening now. */
const lastWl = (s) => s.last?.workload || null;
const pctOf = (v) => (v == null ? null : v * 100);

export const TILES = [
  { id: 'decode', label: 'Decode', unit: 'tok/s', color: '--series-1',
    fmt: (v) => v.toFixed(1), stale: true,
    value: (s) => lastWl(s)?.decode_tps,
    sub: (s) => {
      const w = lastWl(s), pr = s.last?.probe;
      return [w?.n_tokens ? `at ${nf(w.n_tokens)} ctx` : null,
              pr?.decode_tps != null ? `probe ${nf(pr.decode_tps, 1)}` : null].filter(Boolean).join(' · ');
    },
    spark: (rows) => rows.filter((r) => (r.gen_tokens || 0) >= 24).map((r) => r.decode_tps),
    counter: 'decode_tps' },
  { id: 'prefill', label: 'Prefill', unit: 'tok/s', color: '--series-2',
    fmt: (v) => nf(v), stale: true,
    value: (s) => {
      // The last request that computed a real prompt, not a 4-token tail.
      const w = lastWl(s);
      return w?.prefill_tps ?? state.lastPrefill?.prefill_tps;
    },
    sub: (s) => {
      const w = lastWl(s)?.prefill_tps != null ? lastWl(s) : state.lastPrefill;
      return w ? `${nf(w.prompt_tokens)} tokens computed` : 'no prompt computed yet';
    },
    when: (s) => (lastWl(s)?.prefill_tps != null ? lastWl(s) : state.lastPrefill)?.finished,
    spark: (rows) => rows.filter((r) => (r.prompt_tokens || 0) >= minPrefill()).map((r) => r.prefill_tps),
    counter: 'prefill_tps' },
  { id: 'requests', label: 'Active requests', unit: '', color: '--series-5', key: 'req_proc',
    fmt: (v) => nf(v), live: true,
    sub: (s, p) => `${nf(p?.req_def)} deferred · ${nf(s.model?.total_slots)} slots` },
  { id: 'ctx', label: 'Context', unit: 'tok', color: '--series-7',
    fmt: (v) => nf(v),
    // In flight: the deepest busy slot. Idle: the last request's depth - an
    // idle slot's own counter reads 0 on this build even with a cache parked.
    value: (s) => {
      const busy = (s.slots || []).filter((x) => x.processing);
      return busy.length ? Math.max(...busy.map((x) => x.prompt_tokens || 0)) : lastWl(s)?.n_tokens;
    },
    stale: (s) => !(s.slots || []).some((x) => x.processing),
    sub: (s) => {
      const n = s.model?.n_ctx;
      const busy = (s.slots || []).some((x) => x.processing);
      const v = TILES[3].value(s);
      const share = n && v != null ? `${(v / n * 100).toFixed(1)}% of ${nf(n)}` : '';
      return busy ? `in flight · ${share}` : `last request · ${share}`;
    },
    spark: (rows) => rows.map((r) => r.n_tokens) },
  { id: 'accept', label: 'Draft acceptance', unit: '%', color: '--series-3',
    fmt: (v) => v.toFixed(0), stale: true,
    value: (s) => pctOf(lastWl(s)?.accept_rate),
    // No 24h workload says nothing about whether a drafter exists - a freshly
    // switched model has neither traffic nor counters yet.
    sub: (s) => (s.stats?.workload_accept_med != null
      ? `24h median ${(100 * s.stats.workload_accept_med).toFixed(0)}%`
      : s.stats?.workload_n === 0 ? 'no workload in 24h' : ''),
    spark: (rows) => rows.map((r) => (r.accept_rate == null ? null : r.accept_rate * 100)),
    counter: 'accept_pct' },
  { id: 'reuse', label: 'Prompt reused', unit: '%', color: '--series-4',
    fmt: (v) => v.toFixed(0), stale: true,
    value: (s) => pctOf(lastWl(s)?.reuse),
    sub: (s) => {
      const st = s.stats || {};
      return st.cold_n == null ? '' : st.cold_n
        ? `${nf(st.cold_n)} cold prefills in 24h · ${dur(st.cold_seconds)}`
        : 'no cold prefills in 24h';
    },
    spark: (rows) => rows.map((r) => {
      const total = (r.n_tokens || 0) - (r.gen_tokens || 0);
      return total > 0 && r.prompt_tokens != null ? 100 * Math.max(0, 1 - r.prompt_tokens / total) : null;
    }),
    counter: 'cache_pct', counterLabel: 'Prompt from cache' },
  { id: 'gpupwr', label: 'GPU power', unit: 'W', color: '--series-6', key: '_gpupwr', agent: true,
    fmt: (v) => v.toFixed(0), live: true,
    sub: (s) => {
      const cap = (s.gpus || []).reduce((a, g) => a + (g.pwr_limit || 0), 0);
      const day = s.stats?.gpu_kwh_24h;
      return (cap ? `of ${nf(cap)} W capped` : '—') + (day != null ? ` · ${day.toFixed(1)} kWh/24h` : '');
    } },
  { id: 'vram', label: 'VRAM in use', unit: 'GiB', color: '--series-1', key: '_vram', agent: true,
    fmt: (v) => v.toFixed(1), live: true,
    sub: (s) => {
      const t = (s.gpus || []).reduce((a, g) => a + (g.mem_total || 0), 0);
      return t ? `of ${(t / 1024).toFixed(0)} GiB across ${s.gpus.length} card${s.gpus.length === 1 ? '' : 's'}` : '—';
    } },
];

/* Without the agent there are no per-request timings and no GPUs: the request
   tiles read each poll's counter rates instead, and the GPU tiles go. */
const counterTile = (t) => ({
  ...t, label: t.counterLabel || t.label, key: t.counter, value: null, stale: false,
  sub: () => 'from llama.cpp’s counters',
});
const SHOWN = AGENTLESS
  ? TILES.filter((t) => !t.agent).map((t) => (t.counter ? counterTile(t) : t)) : TILES;

export const sparks = {};
const lastGood = {};

export function renderTiles(snap, point) {
  const host = el('tiles');
  if (!host.children.length) {
    host.innerHTML = SHOWN.map((t) => `
      <div class="tile" id="tile-${t.id}">
        <div class="lbl"><span class="sw" style="background:${swatchBg(t.color)}"></span>${esc(t.label)}</div>
        <div class="val" id="tv-${t.id}">—</div>
        <div class="sub" id="ts-${t.id}"></div>
        <div class="spark" id="tk-${t.id}"></div>
      </div>`).join('');
    for (const t of SHOWN) sparks[t.id] = new Sparkline(el('tk-' + t.id), { color: t.color, zero: true });
  }

  const pts = state.points.slice(-180);
  // Chronological workload requests, for the per-request sparks.
  const wlDesc = workloadRows();
  const wl = wlDesc.slice(0, 40).reverse();
  state.lastPrefill = wlDesc.find((r) => (r.prompt_tokens || 0) >= minPrefill() && r.prefill_tps) || null;
  for (const t of SHOWN) {
    if (t.value) {
      let v = null;
      try { v = t.value(snap, point); } catch { v = null; }
      const has = v != null && isFinite(v);
      const stale = typeof t.stale === 'function' ? t.stale(snap) : t.stale;
      const valEl = el('tv-' + t.id);
      valEl.className = 'val' + (has && stale ? ' idle' : '');
      valEl.innerHTML = !has ? '—'
        : `${esc(t.fmt(v))}${t.unit ? `<span class="u">${esc(t.unit)}</span>` : ''}`;
      let sub = '';
      try { sub = t.sub(snap, point) || ''; } catch { sub = ''; }
      const when = stale && has ? ago(t.when ? t.when(snap) : lastWl(snap)?.finished) : '';
      el('ts-' + t.id).textContent = [sub, when].filter(Boolean).join(' · ');
      sparks[t.id].setData(t.spark ? t.spark(wl) : []);
      continue;
    }
    if (lastGood[t.id] == null) {                    // first paint: reach back
      // Newest first: the live buffer, then stored history behind it.
      const seed = [...(state.histPoints || state.backfill || []), ...state.points];
      for (let i = seed.length - 1; i >= 0; i--) {
        const hv = seed[i][t.key];
        if (hv != null && isFinite(hv)) { lastGood[t.id] = hv; break; }
      }
    }
    const v = point ? point[t.key] : null;
    const idle = v == null || !isFinite(v);
    if (!idle) lastGood[t.id] = v;
    const shown = idle ? lastGood[t.id] : v;
    const valEl = el('tv-' + t.id);
    valEl.className = 'val' + (idle && !t.live ? ' idle' : '');
    valEl.innerHTML = shown == null ? '—'
      : `${esc(t.fmt(shown))}${t.unit ? `<span class="u">${esc(t.unit)}</span>` : ''}`;
    let sub = '';
    try { sub = t.sub(snap, point) || ''; } catch { sub = ''; }
    el('ts-' + t.id).textContent = idle && !t.live ? (sub ? sub + ' · idle' : 'idle') : sub;
    sparks[t.id].setData(pts.map((p) => p[t.key]));
  }
}
