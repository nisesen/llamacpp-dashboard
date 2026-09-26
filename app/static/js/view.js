/* Drawing one snapshot: points are flattened for the charts, then every
   section renders. */

import { buildGpuCharts, charts } from './chart-setup.js';
import { renderAlerts, renderModel, renderRequestHealth, renderSlots, renderTopbar } from './sections/inference.js';
import { renderCores, renderFaults, renderFlow, renderGpuCards, renderIpmi, renderMemory, renderPlatform, renderServices } from './sections/machine.js';
import { AGENTLESS, state } from './state.js';
import { renderTiles } from './tiles.js';
import { el, nf } from './util.js';

/* --------------------------------------------------------------- plumbing */

export function flatten(p) {
  if (p && p.gpus) {
    for (const g of p.gpus) {
      p[`g${g.idx}_util`] = g.util;
      p[`g${g.idx}_pwr`] = g.pwr;
      p[`g${g.idx}_hbm`] = g.hbm;
      p[`g${g.idx}_temp`] = g.temp;
      p[`g${g.idx}_mem`] = g.mem_pct;
      p[`g${g.idx}_sm`] = g.sm_clk;
    }
  }
  if (p && p._gpupwr == null) {
    // Total draw across cards, for history rows too (live points get it in derive()).
    let sum = null;
    for (const k in p) if (/^g\d+_pwr$/.test(k) && p[k] != null) sum = (sum || 0) + p[k];
    p._gpupwr = sum;
  }
  if (p) {
    p.accept_pct = p.accept_rate == null ? null : p.accept_rate * 100;
    p.cache_pct = p.cache_hit == null ? null : p.cache_hit * 100;
  }
  return p;
}

export function derive(snap, point) {
  const gpus = snap.gpus || [];
  point._gpupwr = gpus.reduce((a, g) => a + (g.pwr || 0), 0) || null;
  point._vram = gpus.reduce((a, g) => a + (g.mem_used || 0), 0) / 1024 || null;
  return point;
}

export function render(snap, point) {
  renderAlerts(snap);
  renderTopbar(snap);
  renderTiles(snap, point);
  renderModel(snap);
  renderRequestHealth(snap);
  renderSlots(snap);
  buildGpuCharts(snap);
  renderGpuCards(snap);
  renderCores(snap);
  renderMemory(snap);
  renderIpmi(snap);
  renderServices(snap);
  renderPlatform(snap);
  renderFaults(snap);
  renderFlow(snap);

  // Positions beyond the drafter's depth carry vestigial counts; showing all
  // of them buries the ones that matter under a flat tail.
  const allPos = snap.spec_positions || [];
  const peak = Math.max(1, ...allPos.map((p) => p.accepted || 0));
  let cut = 0;
  allPos.forEach((p, i) => { if ((p.accepted || 0) >= peak * 0.01) cut = i; });
  const shown = allPos.slice(0, Math.max(4, Math.min(allPos.length, cut + 3)));
  charts['c-spec'].chart.setData(shown.map((p) => ({ label: String(p.position), value: p.accepted })));
  const tail = allPos.length - shown.length;

  const t = snap.totals || {};
  el('spec-note').textContent = t.draft_tokens
    ? `${nf(t.accepted_tokens)} of ${nf(t.draft_tokens)} accepted overall (${(100 * t.accept_rate_avg).toFixed(1)}%).`
      + (tail ? ` ${tail} deeper positions omitted — under 1% of the peak.` : '')
    : 'Speculative decoding is not active.';

  el('inference-meta').textContent = [snap.model?.file, snap.model?.build].filter(Boolean).join(' · ');
  const sig = snap.probe_signature || {};
  el('probe-note').textContent = sig.enabled
    ? ` Grey marks are probe requests (exactly ${sig.gen_tokens} tokens generated, under ${sig.max_tokens} `
      + 'in the slot): the same request every time, so they are the cleanest trend line for server '
      + 'health, while workload marks move with context depth.'
    : '';
  el('health-meta').textContent = snap.agent_ok || AGENTLESS ? '' : 'host agent unreachable';
  el('net-sub').textContent = snap.net?.primary || '';
  el('footer-text').textContent =
    `Polling ${snap.llama?.url || '—'}${AGENTLESS ? '' : ' and the host agent'} every ${snap.poll_interval}s · ` +
    `${state.points.length} live samples buffered · history retained ${state.meta.retain_days || '?'} days`;
}

export function setLink() {
  const dot = el('link-dot');
  const frozen = state.frozen;
  dot.className = 'dot ' + (frozen ? 'warning' : state.connected ? 'good live' : 'critical');
  el('link-text').textContent = frozen ? 'frozen' : state.connected ? 'live' : 'reconnecting…';
  el('freeze').setAttribute('aria-pressed', String(frozen));
  document.body.classList.toggle('stale', !state.connected && !frozen);
}
