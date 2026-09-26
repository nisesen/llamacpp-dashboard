/* -------------------------------------------------------------------------
   Event annotations, shared by every time chart. The page sets them once
   from the event log; each chart draws the part inside its own window, so a
   model switch lines up across throughput, power and concurrency at once.
     {kind: 'model'|'transition'|'alert', ts, end, level, title, detail}
   A transition or an alert is a span (`end` null while still open); a model
   change is an instant.
   ------------------------------------------------------------------------- */

import { CSS, PX, alpha, esc, fmtFull } from './core.js';

export const ANNOT = { items: [], on: true, charts: new Set() };
const LEVEL_COLOR = { critical: '--critical', serious: '--serious', warning: '--warning' };
const annotColor = (a) => (a.kind === 'model' ? '--series-7'
  : a.kind === 'transition' ? '--muted' : LEVEL_COLOR[a.level] || '--muted');

export function setAnnotations(items) {
  ANNOT.items = items || [];
  for (const c of ANNOT.charts) c.draw();
}

export function showAnnotations(on) {
  ANNOT.on = !!on;
  for (const c of ANNOT.charts) c.draw();
}

/* Spans sit under the data. A restart/switch is a grey wash; an alert is a
   strip of its level's colour along the top, plus a faint wash when it is
   serious or critical - warnings stay a strip, or a week of them would bury
   the data. Spans of one kind are merged in pixel space first, so a burst of
   overlapping alerts draws once instead of stacking into a solid bar. A
   model change is a solid line with a flag. */
export function drawAnnotations(u) {
  if (!ANNOT.on || !ANNOT.items.length) return;
  const ctx = u.ctx, px = PX();
  const { left: l, top: t, width: w, height: h } = u.bbox;
  const t0 = u.scales.x.min, t1 = u.scales.x.max;
  const X = (ts) => u.valToPos(ts, 'x', true);
  const now = Date.now() / 1000;
  const bands = new Map();                          // style -> [[x1, x2], ...]
  for (const a of ANNOT.items) {
    if (a.kind === 'model') continue;
    const e = a.end ?? now;
    if (e < t0 || a.ts > t1) continue;
    const x1 = Math.max(l, X(a.ts));
    const x2 = Math.min(l + w, Math.max(X(e), x1 + 2 * px));
    const style = a.kind === 'transition' ? 'transition' : a.level;
    if (!bands.has(style)) bands.set(style, []);
    bands.get(style).push([x1, x2]);
  }
  ctx.save();
  ctx.beginPath(); ctx.rect(l, t, w, h); ctx.clip();
  for (const style of ['transition', 'warning', 'serious', 'critical']) {
    const list = (bands.get(style) || []).sort((p, q) => p[0] - q[0]);
    const merged = [];
    for (const [x1, x2] of list) {
      const last = merged[merged.length - 1];
      if (last && x1 <= last[1] + px) last[1] = Math.max(last[1], x2);
      else merged.push([x1, x2]);
    }
    const c = CSS(style === 'transition' ? '--muted' : LEVEL_COLOR[style] || '--muted');
    for (const [x1, x2] of merged) {
      if (style !== 'warning') {
        ctx.fillStyle = alpha(c, style === 'transition' ? 0.16 : 0.07);
        ctx.fillRect(x1, t, x2 - x1, h);
      }
      if (style !== 'transition') {
        ctx.fillStyle = alpha(c, 0.9);
        ctx.fillRect(x1, t, x2 - x1, 3 * px);
      }
    }
  }
  for (const a of ANNOT.items) {
    if (a.kind !== 'model' || a.ts < t0 || a.ts > t1) continue;
    const x = Math.round(X(a.ts)) + 0.5;
    const c = CSS(annotColor(a));
    ctx.strokeStyle = alpha(c, 0.9);
    ctx.lineWidth = 1.5 * px;
    ctx.beginPath(); ctx.moveTo(x, t); ctx.lineTo(x, t + h); ctx.stroke();
    ctx.fillStyle = c;
    ctx.beginPath(); ctx.moveTo(x, t); ctx.lineTo(x + 7 * px, t + 4 * px); ctx.lineTo(x, t + 8 * px); ctx.fill();
  }
  ctx.restore();
}

/* The annotations under a cursor at `cx` (CSS px from the plot's left). */
export function annotationsNear(u, cx) {
  if (!ANNOT.on || !ANNOT.items.length || cx == null || cx < 0) return [];
  const t0 = u.scales.x.min, t1 = u.scales.x.max, w = u.bbox.width / PX();
  const X = (ts) => u.valToPos(ts, 'x');
  const now = Date.now() / 1000;
  const near = [];
  for (const a of ANNOT.items) {
    if (a.kind === 'model') {
      if (a.ts >= t0 && a.ts <= t1 && Math.abs(X(a.ts) - cx) <= 6) near.push(a);
      continue;
    }
    const e = a.end ?? now;
    if (e < t0 || a.ts > t1) continue;
    const x1 = Math.max(0, X(a.ts)), x2 = Math.min(w, Math.max(X(e), x1 + 2));
    if (cx >= x1 - 4 && cx <= x2 + 4) near.push(a);
  }
  return near;
}

/* A zoomed-out week can put a dozen events under one pixel. The tooltip
   names the few that matter most - model changes, then by severity - and
   counts the rest; zooming in pulls them apart. */
const ANNOT_RANK = { model: 0, critical: 1, serious: 2, warning: 3, transition: 4 };
const ANNOT_MAX = 4;

export function annotationRows(all) {
  const rank = (a) => ANNOT_RANK[a.kind === 'alert' ? a.level : a.kind] ?? 5;
  const list = all.length <= ANNOT_MAX ? all
    : [...all].sort((p, q) => rank(p) - rank(q) || p.ts - q.ts).slice(0, ANNOT_MAX).sort((p, q) => p.ts - q.ts);
  const more = all.length - list.length;
  return list.map((a) => {
    const when = a.kind === 'model' ? fmtFull(a.ts)
      : `${fmtFull(a.ts)} – ${a.end != null ? fmtFull(a.end) : 'now'}`;
    return `<div class="row annot"><span class="swatch" style="background:${CSS(annotColor(a))}"></span>`
      + `<span class="k">${esc(a.title)}</span></div>`
      + `<div class="annot-d">${esc(when)}${a.detail ? ` · ${esc(a.detail)}` : ''}</div>`;
  }).join('') + (more > 0
    ? `<div class="annot-more">+ ${more} more event${more === 1 ? '' : 's'} here · drag across the chart to pull them apart</div>` : '');
}
