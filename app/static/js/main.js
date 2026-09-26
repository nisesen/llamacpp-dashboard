/* InferenceInquire - the page.

   One WebSocket carries a compact numeric "point" plus a snapshot every poll.
   Points accumulate into a rolling buffer that backs the short ranges; long
   ranges are fetched pre-bucketed from SQLite. One time range scopes every
   chart on the page.

   Nothing about the machine is assumed here either: GPU count, VRAM size, core
   count, interface and rail names all come from the snapshot.

   This module boots the page and owns its controls: the range, freeze, zoom,
   theme, tabs and the URL that carries them. */

import { CSS, fmtFull, onZoom, showAnnotations } from './charts.js';
import { buildBaseCharts, charts, redrawAll, refreshLegends } from './chart-setup.js';
import { connect, drawRequestCharts, loadAnnotations, loadHistory, loadRequests, parkSocket, resumeSocket } from './data.js';
import { loadUsage } from './sections/inference.js';
import { renderEvents } from './sections/machine.js';
import { LIVE_MAX, RANGE_SECONDS, domain, state } from './state.js';
import { TILES, sparks } from './tiles.js';
import { dur, el, esc } from './util.js';
import { render, setLink } from './view.js';

function setAnnotationsOn(on) {
  showAnnotations(on);
  el('annot-toggle').setAttribute('aria-pressed', String(on));
  try { localStorage.setItem('annotations', on ? '1' : '0'); } catch { /* private mode */ }
}
el('annot-toggle').addEventListener('click', () =>
  setAnnotationsOn(el('annot-toggle').getAttribute('aria-pressed') !== 'true'));

/* ------------------------------------------------------------------ boot */

function setRange(r) {
  if (!RANGE_SECONDS[r]) return;
  state.range = r;
  state.zoom = null;                 // a range button zooms out
  [...el('range').children].forEach((b) =>
    b.setAttribute('aria-pressed', String(b.dataset.range === r)));
  try { localStorage.setItem('range', r); } catch { /* private mode */ }
  writeUrl();
  renderTimeBanner();
  refreshLegends();
  loadHistory();
  loadRequests();
  loadAnnotations();
  loadUsage();
}

el('range').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-range]');
  if (btn) setRange(btn.dataset.range);
});

/* The browser chrome (and an installed app's status bar) follows the page. */
function syncThemeColor() {
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', CSS('--plane'));
}

function setTheme(next) {
  document.documentElement.dataset.theme = next;
  syncThemeColor();
  try { localStorage.setItem('theme', next); } catch { /* private mode */ }
  for (const id in charts) charts[id].chart.draw();
  for (const t of TILES) sparks[t.id]?.draw();
  if (state.snap.gpus) render(state.snap, state.points[state.points.length - 1]);
}

el('theme-toggle').addEventListener('click', () =>
  setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));

/* Freezing pins the window's end, and the URL carries it: send the link and
   it opens on the same charts. Resuming clears it. */
function setEnd(end, fromUrl = false) {
  state.end = end == null ? null : +end;
  state.frozen = state.end != null;
  state.endFromUrl = fromUrl && state.frozen;
  setLink();
  writeUrl();
  renderTimeBanner();
  if (!state.frozen && state.snap.ts) render(state.snap, state.points[state.points.length - 1]);
  redrawAll();
  loadHistory();
  loadRequests();
  loadAnnotations();
  loadUsage();
}

function toggleFreeze() {
  setEnd(state.frozen ? null : Math.round(state.snap.ts || Date.now() / 1000));
}

/* Zoom: every time chart shows [from, to] until zoomed out. At least a
   minute, never past now. Tiles and cards stay live - only the charts hold
   still - and the window goes in the URL like a frozen one. */
function setZoom(z) {
  if (z) {
    let [a, b] = z.map(Number);
    b = Math.min(b, Date.now() / 1000);
    if (b - a < 60) { const c = Math.min((a + b) / 2, Date.now() / 1000 - 30); a = c - 30; b = c + 30; }
    state.zoom = [Math.floor(a), Math.ceil(b)];
  } else {
    if (!state.zoom) return;
    state.zoom = null;
  }
  writeUrl();
  renderTimeBanner();
  redrawAll();
  drawRequestCharts();
  loadHistory();
  loadRequests();
  loadAnnotations();
}
onZoom(setZoom);

function renderTimeBanner() {
  const b = el('timebanner');
  if (state.end == null && !state.zoom) { b.hidden = true; b.innerHTML = ''; return; }
  const [t0, t1] = domain();
  b.hidden = false;
  b.classList.toggle('zoom', !!state.zoom);
  const what = state.zoom
    ? `<strong>Zoomed</strong> · charts show ${esc(fmtFull(t0))} – ${esc(fmtFull(t1))} (${esc(dur(t1 - t0))})${
      state.end != null ? ' · frozen' : ' · tiles and cards stay live'}`
    : `<strong>${state.endFromUrl ? 'A saved moment' : 'Frozen'}</strong> · charts show ${
      esc(fmtFull(t0))} – ${esc(fmtFull(t1))}${state.endFromUrl
      ? ' · tiles and cards show the state when the page opened' : ' · nothing updates until you resume'}`;
  b.innerHTML = `<span class="dot ${state.zoom ? 'zoom' : 'warning'}"></span><span class="tb-text">${what}</span>
    <button class="chip" data-act="copy">Copy link</button>
    ${state.zoom ? '<button class="chip" data-act="unzoom">Zoom out</button>' : ''}
    ${state.end != null ? '<button class="chip" data-act="live">Back to live</button>' : ''}`;
}

el('timebanner').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button');
  if (!btn) return;
  if (btn.dataset.act === 'live') { state.zoom = null; setEnd(null); return; }
  if (btn.dataset.act === 'unzoom') { setZoom(null); return; }
  try {
    await navigator.clipboard.writeText(location.href);
  } catch {
    // No clipboard API on plain HTTP: fall back to a selected text field.
    const t = document.createElement('textarea');
    t.value = location.href;
    document.body.appendChild(t);
    t.select();
    try { document.execCommand('copy'); } catch { /* nothing more to try */ }
    t.remove();
  }
  btn.textContent = 'Copied';
  setTimeout(() => { btn.textContent = 'Copy link'; }, 1500);
});
el('freeze').addEventListener('click', toggleFreeze);

document.addEventListener('keydown', (ev) => {
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const tag = (ev.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return;
  // `code` as well as `key`: layouts and synthetic events do not always agree
  // on what the space bar produces.
  if (ev.key === ' ' || ev.code === 'Space' || ev.key === 'Spacebar') {
    ev.preventDefault();
    toggleFreeze();
  } else if (ev.key === 'Escape' && state.zoom) {
    setZoom(null);
  } else if (ev.key === 'e' || ev.key === 'E') {
    setAnnotationsOn(el('annot-toggle').getAttribute('aria-pressed') !== 'true');
  } else if (ev.key === 't' || ev.key === 'T') {
    setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  } else if (/^[0-9]$/.test(ev.key)) {
    setRange(Object.keys(RANGE_SECONDS)[ev.key === '0' ? 9 : +ev.key - 1]);
  }
});

try {
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  syncThemeColor();
  if (localStorage.getItem('annotations') === '0') {
    showAnnotations(false);
    el('annot-toggle').setAttribute('aria-pressed', 'false');
  }
  const savedRange = localStorage.getItem('range');
  if (savedRange && RANGE_SECONDS[savedRange]) {
    state.range = savedRange;
    [...el('range').children].forEach((b) =>
      b.setAttribute('aria-pressed', String(b.dataset.range === savedRange)));
  }
} catch { /* private mode */ }

// Hover readout for the core heatmap.
const tiptext = el('tiptext');
const explainer = el('explainer');

document.addEventListener('mouseover', (ev) => {
  const t = ev.target.closest('[data-tip]');
  if (t) {
    tiptext.textContent = t.dataset.tip;
    const r = t.getBoundingClientRect();
    tiptext.style.left = (r.left + r.width / 2) + 'px';
    tiptext.style.top = r.top + 'px';
    tiptext.style.opacity = '1';
  } else {
    tiptext.style.opacity = '0';
  }

  // The teaching layer: anything with an explainer says what it means and
  // why it matters, not just what it is.
  const e = ev.target.closest('[data-explain-title]');
  if (!e) { explainer.style.opacity = '0'; return; }
  explainer.innerHTML = `<div class="et">${esc(e.dataset.explainTitle)}</div>
    <div class="eb">${esc(e.dataset.explainBody)}</div>`;
  const r = e.getBoundingClientRect();
  const w = explainer.offsetWidth || 320;
  const x = Math.max(w / 2 + 8, Math.min(window.innerWidth - w / 2 - 8, r.left + r.width / 2));
  const above = r.top > explainer.offsetHeight + 16;
  explainer.style.left = x + 'px';
  explainer.style.top = (above ? r.top - 8 : r.bottom + explainer.offsetHeight + 8) + 'px';
  explainer.style.opacity = '1';
});

// Tooltips are placed in viewport coordinates when they open, so once the
// page scrolls they point at nothing - and on a phone a tap leaves one up.
window.addEventListener('scroll', () => {
  tiptext.style.opacity = '0';
  explainer.style.opacity = '0';
}, { passive: true });

/* A background tab draws nothing (see the tick handler) and fetches nothing.
   After a few minutes hidden the socket closes too - a phone left on this
   page would otherwise keep receiving - and reopens on return; its init
   message carries the whole live buffer, so nothing is lost. */
const PARK_AFTER_MS = 5 * 60 * 1000;
let parkTimer = null;
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    parkTimer = setTimeout(() => {
      state.parked = true;
      parkSocket();
    }, PARK_AFTER_MS);
    return;
  }
  clearTimeout(parkTimer);
  if (state.parked) {
    state.parked = false;
    state.lastTick = Date.now();        // not "stale": the reconnect is under way
    resumeSocket();
  } else if (!state.frozen && state.snap.ts) {
    render(state.snap, state.points[state.points.length - 1]);
    redrawAll();
  }
  if (!state.frozen) {
    loadRequests();
    if (RANGE_SECONDS[state.range] > LIVE_MAX) loadHistory();
    renderEvents();
    loadAnnotations();
    loadUsage();
  }
});

/* ------------------------------------------------------------------ pages */

/* One page at a time, Overview first; "All" is the whole dashboard in one
   scroll. The tab, the range and a pinned end live in the URL hash
   (#tab=gpus&range=24h&end=1790000000), so a link opens the same view.
   Old section anchors (#sec-gpus) still land on their page. */
const PAGES = ['overview', 'inference', 'flow', 'gpus', 'host', 'health', 'all'];
const OLD_ANCHORS = { 'sec-inference': 'inference', 'sec-flow': 'flow', 'sec-gpus': 'gpus',
                      'sec-host': 'host', 'sec-health': 'health' };

function readUrl() {
  const raw = decodeURIComponent(location.hash.slice(1));
  if (OLD_ANCHORS[raw]) return { tab: OLD_ANCHORS[raw] };
  const p = new URLSearchParams(raw);
  const end = p.get('end');
  const from = +p.get('from'), to = +p.get('to');
  return { tab: p.get('tab'), range: p.get('range'),
           end: end != null && isFinite(+end) && +end > 0 ? +end : null,
           zoom: from > 0 && to > from ? [from, to] : null };
}

function writeUrl() {
  const p = new URLSearchParams({ tab: state.tab, range: state.range });
  if (state.end != null) p.set('end', String(state.end));
  if (state.zoom) { p.set('from', String(state.zoom[0])); p.set('to', String(state.zoom[1])); }
  const hash = '#' + p.toString();
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

function setTab(tab, scroll = true) {
  state.tab = PAGES.includes(tab) ? tab : 'overview';
  for (const page of document.querySelectorAll('.page')) {
    page.hidden = state.tab !== 'all' && page.dataset.page !== state.tab;
  }
  for (const a of document.querySelectorAll('#navlinks a[data-tab]')) {
    const on = a.dataset.tab === state.tab;
    a.classList.toggle('active', on);
    a.setAttribute('aria-selected', String(on));
  }
  writeUrl();
  if (scroll) window.scrollTo({ top: 0 });
}

el('navlinks').addEventListener('click', (ev) => {
  const a = ev.target.closest('a[data-tab]');
  if (!a) return;
  ev.preventDefault();
  setTab(a.dataset.tab);
});

// A pasted or edited link in an open tab.
window.addEventListener('hashchange', () => {
  const u = readUrl();
  if (u.tab) setTab(u.tab, false);
  if (u.range && RANGE_SECONDS[u.range] && u.range !== state.range) setRange(u.range);
  if ((u.end ?? null) !== state.end) setEnd(u.end, true);
  if (String(u.zoom) !== String(state.zoom)) setZoom(u.zoom);
});

{
  const u = readUrl();
  if (u.range && RANGE_SECONDS[u.range]) {
    state.range = u.range;
    [...el('range').children].forEach((b) =>
      b.setAttribute('aria-pressed', String(b.dataset.range === u.range)));
  }
  if (u.end != null) {
    state.end = u.end;
    state.frozen = true;
    state.endFromUrl = true;
  }
  if (u.zoom) state.zoom = u.zoom;
  setTab(u.tab || 'overview', false);
}
buildBaseCharts();
connect();
setLink();
renderTimeBanner();
loadHistory();
loadRequests();
loadAnnotations();
loadUsage();
setInterval(() => { if (!document.hidden) { renderEvents(); loadAnnotations(); } }, 30000);
setInterval(() => { if (!document.hidden && !state.frozen) loadUsage(); }, 60000);
setInterval(() => { if (!state.frozen && !document.hidden) loadRequests(); }, 15000);
setInterval(() => {
  if (RANGE_SECONDS[state.range] > LIVE_MAX && !state.zoom && !state.frozen && !document.hidden) loadHistory();
}, 20000);
setInterval(() => {
  if (state.connected && !state.frozen && !document.hidden && Date.now() - state.lastTick > 12000) {
    document.body.classList.add('stale');
  }
}, 3000);
