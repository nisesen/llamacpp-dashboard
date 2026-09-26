/* -------------------------------------------------------------------------
   LineChart - time series. Lines for sampled data; dots for one mark per
   completed request, where connecting them would imply a continuous rate
   that was never measured. One chart can carry both.
   ------------------------------------------------------------------------- */

import { ANNOT, annotationRows, annotationsNear, drawAnnotations } from './annotations.js';
import { CSS, PX, ZOOM, alpha, fmtFull, hideTip, observeSize, placeTip, syncOpts, timeAxis, tipRow, uPlot, valueAxis, zoomCursor, zoomOnSelect } from './core.js';
import { Plot } from './plot.js';

/* Series the viewer switched off, by chart, so a chart rebuilt when a GPU
   appears or goes keeps them off. */
const HIDDEN = new Map();

export class LineChart extends Plot {
  /**
   * @param {HTMLElement} host  .plot container (uPlot, tooltip and empty state live inside)
   * @param {object} opt
   *   series     [{key, label, color, area, width, opacity, mode}]
   *   height     px of the whole box INCLUDING the x-axis band
   *   yMin/yMax  force a bound (default: auto, zero-anchored when all >= 0)
   *   format     (v) => string, for axis + tooltip; tipFormat for the tooltip only,
   *              axisFormat for the axis only
   *   incrs      the tick steps to choose from, when 1/2/5 x 10^n is wrong (bytes)
   *   unit       string appended in the tooltip
   *   thresholds [{value, label, color}]  drawn dashed: a real threshold, not grid
   *   stepped    draw as a step line
   *   mode       'dots' for event-sampled series (per series: s.mode)
   *   splits     (min, max) => tick values, when 1/2/5 steps are wrong (a log axis)
   */
  constructor(host, opt) {
    super(host, opt, { height: 190, format: (v) => String(Math.round(v)), thresholds: [] });
    this.series = this.opt.series;
    this.points = [];
    this.domain = null;
    this.hidden = HIDDEN.get(host.id) || new Set();
    HIDDEN.set(host.id, this.hidden);
    this.focusIdx = null;                 // the mark the arrow keys are on
    if (this.opt.annotations !== false) ANNOT.charts.add(this);
    if (this.opt.sync !== false) ZOOM.charts.add(this);
  }

  setData(points, domain) {
    this.points = points || [];
    this.domain = domain || null;
    this._changed();
  }

  hasData() {
    const [t0, t1] = this.domain || [-Infinity, Infinity];
    const keys = this.series.map((s) => s.key);
    return this.points.some((p) => p.ts >= t0 && p.ts <= t1
      && keys.some((k) => p[k] != null && isFinite(p[k])));
  }

  isDots(s) { return s.mode ? s.mode === 'dots' : this.opt.mode === 'dots'; }

  /* Legend toggles. */
  isShown(key) { return !this.hidden.has(key); }

  toggle(key) {
    const i = this.series.findIndex((s) => s.key === key);
    if (i < 0) return;
    if (this.hidden.has(key)) this.hidden.delete(key); else this.hidden.add(key);
    this.u?.setSeries(i + 1, { show: !this.hidden.has(key) });
  }

  focus(key) {
    if (!this.u) return;
    const i = this.series.findIndex((s) => s.key === key);
    this.u.setSeries(i < 0 ? null : i + 1, { focus: i >= 0 });
  }

  /* Aligned columns for uPlot. A point that does not carry a series at all
     (a request mark merged into a sampled line's data) is `undefined`, which
     uPlot steps over; only an explicit null breaks the line. */
  _data() {
    const pts = this.points, n = pts.length, S = this.series;
    const xs = new Array(n);
    const ys = S.map(() => new Array(n));
    for (let i = 0; i < n; i++) {
      const p = pts[i];
      xs[i] = p.ts;
      for (let j = 0; j < S.length; j++) {
        const k = S[j].key;
        if (!(k in p)) { ys[j][i] = undefined; continue; }
        const v = p[k];
        ys[j][i] = v == null || !isFinite(v) ? null : v;
      }
    }
    // A series no point carries at all (a peak line, which only stored
    // history has) says nothing in the tooltip, not "—".
    this.present = ys.map((col) => col.some((v) => v !== undefined));
    return [xs, ...ys];
  }

  _yRange(dmin, dmax) {
    const o = this.opt;
    let lo = dmin == null || !isFinite(dmin) ? Infinity : dmin;
    let hi = dmax == null || !isFinite(dmax) ? -Infinity : dmax;
    // Thresholds bound the view in BOTH directions - a lower limit below the
    // data has to stay on screen too. Opt out when a sensor's own limits are
    // so wide that honouring them would flatten the signal (a 12 V rail whose
    // critical band is 9.9-14.5 V); then they draw only if they fall in view.
    if (o.thresholdsExpandDomain !== false) {
      for (const th of o.thresholds) {
        if (th.value > hi) hi = th.value;
        if (th.value < lo) lo = th.value;
      }
    }
    if (lo === Infinity) { lo = 0; hi = 1; }
    // Zero-anchoring is right for rates and counts, where zero means "none".
    // It is wrong for quantities with a meaningful non-zero baseline - a 12 V
    // rail or a die temperature - where it crushes the signal into a sliver.
    const zeroAnchor = o.zeroAnchor !== false;
    if (o.yMin != null) lo = o.yMin;
    else if (zeroAnchor && lo >= 0) lo = 0;
    if (o.yMax != null) hi = o.yMax;
    if (hi <= lo) hi = lo + Math.abs(lo || 1) * 0.01;
    const padY = (hi - lo) * 0.08;
    if (o.yMax == null) hi += padY;
    if (o.yMin == null && !(zeroAnchor && lo === 0)) lo -= padY;
    // A floor on the visible span, as a fraction of the value itself. Without
    // it a rail sitting at 11.98 +/- 0.02 would fill the plot with noise and
    // make a steady supply look unstable. Derived, so it suits a 5 V rail too.
    if (o.minSpanFrac && o.yMin == null && o.yMax == null) {
      const centre = (hi + lo) / 2;
      const want = Math.abs(centre) * o.minSpanFrac;
      if (hi - lo < want) { hi = centre + want / 2; lo = centre - want / 2; }
    }
    return [lo, hi];
  }

  _series(s) {
    const color = () => CSS(s.color);
    if (this.isDots(s)) {
      return {
        label: s.label, show: !this.hidden.has(s.key),
        paths: () => null,
        points: {
          show: true, space: 0, size: 9.5, width: 1.5,
          stroke: () => CSS('--surface'), fill: color,
        },
      };
    }
    const op = s.opacity ?? 1;
    return {
      label: s.label, show: !this.hidden.has(s.key),
      stroke: () => alpha(color(), op),
      width: s.width || 2,
      cap: 'round',
      paths: this.opt.stepped ? uPlot.paths.stepped({ align: 1 }) : uPlot.paths.linear(),
      fill: s.area ? (u) => {
        const { top, height } = u.bbox;
        const g = u.ctx.createLinearGradient(0, top, 0, top + height);
        g.addColorStop(0, alpha(color(), 0.22));
        g.addColorStop(1, alpha(color(), 0));
        return g;
      } : undefined,
      // A lone sample between gaps still shows, as a small dot.
      points: { show: false, size: 4, width: 0, fill: color, filter: isolatedPoints },
    };
  }

  _opts(w, h) {
    const o = this.opt;
    const synced = o.sync !== false;
    return {
      ...this._base(w, h),
      focus: { alpha: 0.28 },
      scales: {
        x: {
          time: true,
          range: (u, min, max) => (this.domain ? [this.domain[0], this.domain[1]]
            : min == null ? [Date.now() / 1000 - 3600, Date.now() / 1000]
            : min === max ? [min - 30, max + 30] : [min, max]),
        },
        y: { range: (u, min, max) => this._yRange(min, max) },
      },
      axes: [
        timeAxis(),
        valueAxis(o.axisFormat || o.format, {
          ...(o.incrs ? { incrs: o.incrs } : {}),
          ...(o.splits ? { splits: (u, ai, min, max) => o.splits(min, max) } : {}),
        }),
      ],
      series: [{}, ...this.series.map((s) => this._series(s))],
      cursor: {
        y: false,
        ...(synced ? { sync: syncOpts() } : {}),
        // Sparse marks are grabbed within ~24px; a sampled line reads the
        // nearest sample, or nothing across a gap.
        hover: {
          skip: [null, undefined],
          prox: (u, si) => (si > 0 && this.isDots(this.series[si - 1]) ? 24 : 40),
        },
        points: {
          size: 11, width: 1.5,
          stroke: () => CSS('--surface'),
          fill: (u, si) => {
            const s = this.series[si - 1];
            return alpha(CSS(s.color), s.opacity ?? 1);
          },
        },
        ...(synced ? zoomCursor() : { drag: { x: false, y: false } }),
      },
      hooks: {
        drawAxes: [(u) => { if (o.annotations !== false) drawAnnotations(u); this._drawThresholds(u); }],
        draw: [(u) => { this._drawEndpoints(u); this._drawThresholds(u, true); }],
        setCursor: [(u) => this._cursor(u)],
        setSelect: [zoomOnSelect],
      },
    };
  }

  _mounted() {
    const over = this.u.over;
    over.tabIndex = 0;
    over.setAttribute('role', 'img');
    over.setAttribute('aria-label', this.opt.ariaLabel || this.opt.title || 'chart');
    // Keyboard parity: arrows walk the marks the crosshair would show.
    over.addEventListener('keydown', (ev) => {
      if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
      const idxs = this._marks();
      if (!idxs.length) return;
      ev.preventDefault();
      let k = this.focusIdx == null ? idxs.length - 1 : idxs.indexOf(this.focusIdx);
      if (k < 0) k = idxs.length - 1;
      else if (this.focusIdx != null) k = Math.max(0, Math.min(idxs.length - 1, k + (ev.key === 'ArrowRight' ? 1 : -1)));
      this.focusIdx = idxs[k];
      const u = this.u, i = this.focusIdx;
      let top = u.bbox.height / PX() / 2;
      for (let j = 1; j < u.series.length; j++) {
        const v = u.data[j][i];
        if (u.series[j].show && v != null) { top = u.valToPos(v, 'y'); break; }
      }
      u.setCursor({ left: u.valToPos(u.data[0][i], 'x'), top });
    });
    over.addEventListener('blur', () => {
      if (this.focusIdx == null) return;
      this.focusIdx = null;
      this.u?.setCursor({ left: -10, top: -10 });
    });
  }

  /* Indices inside the window that carry a shown value. */
  _marks() {
    const u = this.u, xs = u.data[0], out = [];
    const t0 = u.scales.x.min, t1 = u.scales.x.max;
    for (let i = 0; i < xs.length; i++) {
      if (xs[i] < t0 || xs[i] > t1) continue;
      for (let j = 1; j < u.series.length; j++) {
        if (u.series[j].show && u.data[j][i] != null) { out.push(i); break; }
      }
    }
    return out;
  }

  /* Dashed BECAUSE they are thresholds, never grid. The line runs under
     the data; its label is drawn over it (labels = true), on a backing of
     the surface, so marks crowding the line never bury it. */
  _drawThresholds(u, labels = false) {
    if (!this.opt.thresholds.length) return;
    const ctx = u.ctx, px = PX();
    const { left, top, width, height } = u.bbox;
    ctx.save();
    ctx.font = `${10 * px}px ${CSS('--font')}`;
    for (const th of this.opt.thresholds) {
      const y = Math.round(u.valToPos(th.value, 'y', true)) + 0.5;
      if (y < top || y > top + height) continue;
      const c = CSS(th.color || '--muted');
      if (!labels) {
        ctx.setLineDash([4 * px, 4 * px]);
        ctx.strokeStyle = c;
        ctx.lineWidth = px;
        ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + width, y); ctx.stroke();
        continue;
      }
      if (!th.label) continue;
      // Above the line, or below it when the line runs along the top.
      const tw = ctx.measureText(th.label).width;
      const below = y - 15 * px < top;
      const bx = left + 3 * px, by = below ? y + 2 * px : y - 15 * px;
      ctx.fillStyle = alpha(CSS('--surface'), 0.85);
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(bx, by, tw + 6 * px, 13 * px, 3 * px);
      else ctx.rect(bx, by, tw + 6 * px, 13 * px);
      ctx.fill();
      ctx.fillStyle = c;
      ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
      ctx.fillText(th.label, bx + 3 * px, by + 12 * px);
    }
    ctx.restore();
  }

  /* Selective direct label: the endpoint of each line only. */
  _drawEndpoints(u) {
    if (this.opt.directLabel === false) return;
    const ctx = u.ctx, px = PX();
    const { left, width } = u.bbox;
    this.series.forEach((s, j) => {
      if (this.isDots(s) || (s.opacity ?? 1) < 1 || !u.series[j + 1].show) return;
      const ys = u.data[j + 1], xs = u.data[0];
      for (let i = ys.length - 1; i >= 0; i--) {
        if (ys[i] == null) continue;
        if (xs[i] < u.scales.x.min || xs[i] > u.scales.x.max) return;
        const x = Math.min(u.valToPos(xs[i], 'x', true), left + width - 2 * px);
        ctx.fillStyle = CSS(s.color);
        ctx.beginPath(); ctx.arc(x, u.valToPos(ys[i], 'y', true), 2.5 * px, 0, Math.PI * 2); ctx.fill();
        return;
      }
    });
  }

  _cursor(u) {
    const c = u.cursor;
    // The mouse is here, or the arrow keys are: this chart shows the tooltip.
    if (c.event != null) this.focusIdx = null;
    const owned = c.event != null || this.focusIdx != null;
    if (!owned || c.left == null || c.left < 0) { hideTip(this.tip); return; }
    const rows = [];
    let best = null;
    const anyDots = this.series.some((s) => this.isDots(s));
    for (let i = 1; i < u.series.length; i++) {
      if (!u.series[i].show) continue;
      const s = this.series[i - 1];
      const idx = c.idxs[i];
      if (idx == null) {
        if (!anyDots && this.present?.[i - 1]) rows.push([s, null]);
        continue;
      }
      const d = Math.abs(u.valToPos(u.data[0][idx], 'x') - c.left);
      if (!best || d < best.d) best = { d, ts: u.data[0][idx] };
      rows.push([s, u.data[i][idx]]);
    }
    const near = this.opt.annotations === false ? [] : annotationsNear(u, c.left);
    if (!rows.length && !near.length) { hideTip(this.tip); return; }
    const unit = this.opt.unit ? ` ${this.opt.unit}` : '';
    const tfmt = this.opt.tipFormat || this.opt.format;
    const when = best ? best.ts : u.posToVal(c.left, 'x');
    this.tip.innerHTML = (rows.length ? `<div class="when">${fmtFull(when)}</div>` : '')
      + rows.map(([s, v]) => tipRow(alpha(CSS(s.color), s.opacity ?? 1), s.label,
        v == null ? '—' : tfmt(v) + unit)).join('')
      + annotationRows(near);
    const px = PX();
    placeTip(this.tip, this.host, u.bbox.left / px + c.left, u.bbox.top / px + c.top);
  }
}

/* For a line with points hidden: the samples with no neighbour to join. */
function isolatedPoints(u, si, show) {
  if (show) return null;
  const ys = u.data[si], n = ys.length;
  if (!n) return null;
  const [i0, i1] = u.series[0].idxs || [0, n - 1];
  const out = [];
  let prev = null;                                // last defined value before i
  for (let i = Math.max(0, i0 - 1); i <= Math.min(n - 1, i1 + 1); i++) {
    const v = ys[i];
    if (v === undefined) continue;
    if (v != null && prev == null) {
      let j = i + 1;
      while (j < n && ys[j] === undefined) j++;
      if (j >= n || ys[j] == null) out.push(i);
    }
    prev = v;
  }
  return out.length ? out : null;
}

/* -------------------------------------------------------------------------
   Sparkline - a stat tile's context strip. No axes, no tooltip: the tile's
   value is the reading, the spark is only the shape.
   ------------------------------------------------------------------------- */

export class Sparkline {
  constructor(host, opt) {
    this.host = host;
    this.opt = Object.assign({ color: '--series-1' }, opt);
    this.values = [];
    this.u = null;
    this.dirty = true;
    this.ro = observeSize(host, () => this.draw());
  }

  setData(values) {
    const next = (values || []).map((v) => (v == null || !isFinite(v) ? null : v));
    if (next.length === this.values.length && next.every((v, i) => v === this.values[i])) return;
    this.values = next;
    this.dirty = true;
    this.draw();
  }

  _data() { return [this.values.map((_, i) => i), this.values]; }

  draw() {
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (w < 8 || h < 6) return;
    if (!this.u) {
      const color = () => CSS(this.opt.color);
      this.u = new uPlot({
        width: w, height: h,
        padding: [2, 1, 2, 1],
        legend: { show: false },
        cursor: { show: false },
        select: { show: false },
        axes: [{ show: false }, { show: false }],
        scales: {
          x: { time: false, range: (u, min, max) => [0, Math.max(1, this.values.length - 1)] },
          y: {
            range: (u, min, max) => {
              if (min == null) return [0, 1];
              const lo = this.opt.zero ? Math.min(0, min) : min;
              return [lo, max > lo ? max : lo + 1];
            },
          },
        },
        series: [{}, {
          stroke: color, width: 1.5, cap: 'round',
          fill: (u) => {
            const g = u.ctx.createLinearGradient(0, 0, 0, u.height * PX());
            g.addColorStop(0, alpha(color(), 0.25));
            g.addColorStop(1, alpha(color(), 0));
            return g;
          },
          points: { show: false },
        }],
      }, this._data(), this.host);
      this.u.root.setAttribute('aria-hidden', 'true');
      this.dirty = false;
      return;
    }
    if (this.u.width !== w || this.u.height !== h) this.u.setSize({ width: w, height: h });
    if (this.dirty) { this.dirty = false; this.u.setData(this._data()); } else this.u.redraw(false);
  }
}
