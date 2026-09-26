/* What every chart shares: theme colours, time and number formatting, axes,
   the tooltip, and the cursor sync and drag-zoom that tie the time charts together. */

export const uPlot = window.uPlot;

/* Custom properties, resolved once per theme: the charts ask for colours on
   every frame, and getComputedStyle is not free. */
const cssCache = new Map();
let cssTheme = null;
export function CSS(name, el) {
  if (el) return getComputedStyle(el).getPropertyValue(name).trim() || name;
  const theme = document.documentElement.dataset.theme;
  if (theme !== cssTheme) { cssCache.clear(); cssTheme = theme; }
  let v = cssCache.get(name);
  if (v === undefined) {
    v = getComputedStyle(document.documentElement).getPropertyValue(name).trim() || name;
    cssCache.set(name, v);
  }
  return v;
}

export const PX = () => window.devicePixelRatio || 1;

export const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

export function alpha(hex, a) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

const p2 = (n) => String(n).padStart(2, '0');

export function fmtTime(ts, span) {
  const d = new Date(ts * 1000);
  if (span <= 900) return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  if (span <= 172800) return `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${p2(d.getHours())}:00`;
}

export function fmtFull(ts) {
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

/* A time-axis label for a tick `incr` seconds from its neighbours. Ticks
   land on round local times, and the one at midnight names the day, so a
   24-hour axis reads 20:00 22:00 9/24 02:00. */
function timeTick(v, incr) {
  const d = new Date(v * 1000);
  const hm = `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  if (incr < 60) return `${hm}:${p2(d.getSeconds())}`;
  const day = `${d.getMonth() + 1}/${d.getDate()}`;
  if (incr < 86400) return d.getHours() === 0 && d.getMinutes() === 0 ? day : hm;
  if (incr < 28 * 86400) return day;
  return d.getMonth() === 0 ? String(d.getFullYear()) : d.toLocaleDateString(undefined, { month: 'short' });
}

export const FONT = () => `10px ${CSS('--font')}`;

export const textWidth = (() => {
  const ctx = document.createElement('canvas').getContext('2d');
  return (text) => { ctx.font = FONT(); return ctx.measureText(String(text)).width; };
})();

/* Tick steps restricted to 1/2/5 x 10^n so labels stay readable. */
export const INCRS = [];
for (let e = -4; e <= 12; e++) for (const m of [1, 2, 5]) INCRS.push(+(m * 10 ** e).toPrecision(1));

export function niceStep(raw) {
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
}

/* A ResizeObserver whose callback can change layout is a loop waiting to
   happen. Only react when the box actually changed size, and coalesce into
   one animation frame. */
export function observeSize(host, onChange) {
  let lastW = -1, lastH = -1, queued = false;
  const ro = new ResizeObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const w = host.clientWidth, h = host.clientHeight;
      if (w === lastW && h === lastH) return;
      lastW = w; lastH = h;
      onChange();
    });
  });
  ro.observe(host);
  return ro;
}

/* ------------------------------------------------------------ the tooltip */

export function tipRow(color, label, value) {
  return `<div class="row">${color ? `<span class="swatch" style="background:${color}"></span>` : ''}`
    + `<span class="k">${esc(label)}</span><span class="v">${esc(value)}</span></div>`;
}

/* Beside the point, never on it: right of the cursor, flipping left near the
   edge, vertically centred on it and kept inside the plot. */
export function placeTip(tip, host, x, y) {
  tip.style.opacity = '1';
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  const W = host.clientWidth, H = host.clientHeight;
  let left = x + 14;
  if (left + tw > W) left = x - 14 - tw;
  left = Math.max(0, Math.min(W - tw, left));
  const top = Math.max(-6, Math.min(H - th + 6, y - th / 2));
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

export const hideTip = (tip) => { tip.style.opacity = '0'; };

/* Repaint for a hover highlight. Deferred: a cursor hook can fire inside
   uPlot's own commit (setData under a resting mouse), and a redraw asked for
   there is dropped - the highlight would stay painted after the mouse left. */
export const repaint = (u) => queueMicrotask(() => u.redraw(false));

/* -------------------------------------------------------- shared settings */

export function axisBase() {
  return {
    font: FONT(),
    stroke: () => CSS('--muted'),
    grid: { stroke: () => CSS('--grid'), width: 1 },
    ticks: { show: false },
  };
}

export function timeAxis() {
  return {
    ...axisBase(), space: 78, size: 24, gap: 6,
    border: { show: true, stroke: () => CSS('--axis'), width: 1 },
    values: (u, splits, ax, space, incr) => splits.map((v) => timeTick(v, incr)),
  };
}

/* The left gutter is sized to the widest label actually drawn, so
   "0.5 KiB/s" is not clipped and "40" does not get a 46px moat. */
export function valueAxis(format, extra) {
  return {
    // A short plot (the phone's overview) still gets two labels at least.
    ...axisBase(), side: 3, gap: 7, incrs: INCRS,
    space: (u, ai, min, max, dim) => Math.max(12, Math.min(34, dim / 2.6)),
    size: (u, values) => Math.max(30, Math.ceil(Math.max(0, ...(values || []).map(textWidth))) + 13),
    values: (u, splits) => splits.map((v) => format(v)),
    ...extra,
  };
}

/* One cursor across every time chart. Hovering any plot puts the crosshair
   at the same instant on all of them, which is how you actually read a
   dashboard: "what were the GPUs doing when that request was slow?" Only the
   hovered chart shows a tooltip; the rest show the line and their markers. */
const SYNC_KEY = 'inferenceinquire-time';
// Time only: every chart's y axis means something different.
export const syncOpts = () => ({ key: SYNC_KEY, setSeries: false, scales: ['x', null] });

/* Dragging across any time chart zooms every one of them to that window.
   The page owns what a zoom means - it fetches finer history, puts the
   window in the URL - so the charts only report the selection. */
export const ZOOM = { handler: null, charts: new Set() };

export function onZoom(fn) { ZOOM.handler = fn; }

function clearSelections() {
  for (const c of ZOOM.charts) c.u?.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
}

export function zoomOnSelect(u) {
  // Only the chart the mouse went up on reports; synced charts only mirror
  // the rectangle while it is drawn.
  if (u.cursor.event?.type !== 'mouseup') return;
  const { left, width } = u.select;
  const a = u.posToVal(left, 'x'), b = u.posToVal(left + width, 'x');
  clearSelections();
  if (width >= 8) ZOOM.handler?.([a, b]);
}

export const zoomCursor = () => ({
  drag: { x: true, y: false, setScale: false, dist: 8 },
  bind: {
    // Double-click is "zoom out", the page's own reset, not uPlot's.
    dblclick: () => (e) => { if (e.button === 0) ZOOM.handler?.(null); return null; },
  },
});
