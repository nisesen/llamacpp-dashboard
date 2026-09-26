/* -------------------------------------------------------------------------
   ScatterChart - one mark per request, numeric on both axes.

   Used for "throughput against context depth": the relationship the
   bench-depth sweep measures deliberately, read here from traffic that was
   going to happen anyway. Drag to zoom into a depth band; double-click to
   see all of it again.
   ------------------------------------------------------------------------- */

import { CSS, FONT, INCRS, PX, axisBase, esc, fmtFull, hideTip, placeTip, repaint, tipRow, valueAxis } from './core.js';
import { Plot } from './plot.js';

export class ScatterChart extends Plot {
  constructor(host, opt) {
    super(host, opt, {
      height: 210, format: (v) => String(Math.round(v)),
      xFormat: (v) => String(Math.round(v)), radius: 3.5,
      emptyText: 'No completed requests in this range',
    });
    this.rows = [];
    this.pts = [];
    this.hot = null;
    this.xZoom = null;
  }

  setData(rows) {
    this.rows = rows || [];
    const { xKey, series } = this.opt;
    this.pts = [];
    for (const row of this.rows) {
      const x = row[xKey];
      if (x == null || !isFinite(x)) continue;
      for (const s of series) {
        const y = row[s.key];
        if (y != null && isFinite(y)) this.pts.push({ x, y, s, row });
      }
    }
    this.pts.sort((a, b) => a.x - b.x);
    this._changed();
  }

  hasData() { return this.rows.length > 0; }

  _xRange() {
    if (this.xZoom) return this.xZoom;
    if (!this.pts.length) return [0, 1];
    let lo = this.pts[0].x, hi = this.pts[this.pts.length - 1].x;
    if (this.opt.xZero !== false) lo = 0;
    if (hi <= lo) hi = lo + 1;
    return [lo, hi + (hi - lo) * 0.06];
  }

  _yRange() {
    const [x0, x1] = this._xRange();
    let lo = Infinity, hi = -Infinity;
    for (const p of this.pts) {
      if (p.x < x0 || p.x > x1) continue;
      if (p.y < lo) lo = p.y;
      if (p.y > hi) hi = p.y;
    }
    if (lo === Infinity) return [0, 1];
    if (this.opt.yZero !== false) lo = 0;
    if (hi <= lo) hi = lo + 1;
    return [lo, hi + (hi - lo) * 0.10];
  }

  _data() { return [this.pts.map((p) => p.x), this.pts.map((p) => p.y)]; }

  _opts(w, h) {
    return {
      ...this._base(w, h),
      scales: {
        x: { time: false, range: () => this._xRange() },
        y: { range: () => this._yRange() },
      },
      axes: [
        {
          ...axisBase(), space: 96, size: 22, gap: 6, incrs: INCRS,
          border: { show: true, stroke: () => CSS('--axis'), width: 1 },
          values: (u, splits) => splits.map((v) => this.opt.xFormat(v)),
          label: this.opt.xLabel, labelFont: FONT(), labelSize: 14, labelGap: 0,
        },
        valueAxis(this.opt.format),
      ],
      series: [{}, { paths: () => null, points: { show: false } }],
      cursor: {
        x: false, y: false, points: { show: false },
        drag: { x: true, y: false, setScale: false, dist: 8 },
        bind: { dblclick: () => (e) => { if (e.button === 0 && this.xZoom) { this.xZoom = null; this._changed(); } return null; } },
      },
      hooks: {
        draw: [(u) => this._drawPts(u)],
        setCursor: [(u) => this._cursor(u)],
        setSelect: [(u) => {
          if (u.cursor.event?.type !== 'mouseup') return;
          const { left, width } = u.select;
          u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
          if (width < 8) return;
          this.xZoom = [u.posToVal(left, 'x'), u.posToVal(left + width, 'x')];
          this._changed();
        }],
      },
    };
  }

  /* Older marks recede, so the current regime reads first. */
  _drawPts(u) {
    const ctx = u.ctx, px = PX();
    const { left, top, width, height } = u.bbox;
    const [x0, x1] = [u.scales.x.min, u.scales.x.max];
    const now = Date.now() / 1000;
    let oldest = now;
    for (const r of this.rows) if (r.finished && r.finished < oldest) oldest = r.finished;
    const span = Math.max(1, now - oldest);
    const rad = this.opt.radius * px;
    const surface = CSS('--surface');
    ctx.save();
    ctx.beginPath(); ctx.rect(left - rad * 2, top - rad * 2, width + rad * 4, height + rad * 4); ctx.clip();
    for (const p of this.pts) {
      if (p.x < x0 || p.x > x1) continue;
      p.px = u.valToPos(p.x, 'x', true);
      p.py = u.valToPos(p.y, 'y', true);
      const age = (now - (p.row.finished || now)) / span;
      ctx.globalAlpha = 0.35 + 0.65 * (1 - age);
      ctx.fillStyle = surface;
      ctx.beginPath(); ctx.arc(p.px, p.py, rad + 1.5 * px, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = CSS(p.s.color);
      ctx.beginPath(); ctx.arc(p.px, p.py, rad, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (this.hot) {
      ctx.strokeStyle = CSS('--text'); ctx.lineWidth = 1.5 * px;
      ctx.beginPath(); ctx.arc(this.hot.px, this.hot.py, rad + 4 * px, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.restore();
  }

  _cursor(u) {
    const c = u.cursor, px = PX();
    let near = null;
    if (c.left != null && c.left >= 0) {
      const cx = c.left * px + u.bbox.left, cy = c.top * px + u.bbox.top;
      let best = (40 * px) ** 2;
      for (const p of this.pts) {
        if (p.px == null || p.x < u.scales.x.min || p.x > u.scales.x.max) continue;
        const d = (p.px - cx) ** 2 + (p.py - cy) ** 2;
        if (d < best) { best = d; near = p; }
      }
    }
    if (near !== this.hot) { this.hot = near; repaint(u); }
    if (!near) { hideTip(this.tip); return; }
    const r = near.row;
    const unit = this.opt.unit ? ` ${this.opt.unit}` : '';
    this.tip.innerHTML = `<div class="when">${fmtFull(r.finished)} · task ${esc(r.task ?? '—')}</div>`
      + tipRow(null, this.opt.xLabel || 'x', this.opt.xFormat(near.x))
      + tipRow(CSS(near.s.color), near.s.label, this.opt.format(near.y) + unit)
      + (r.wall_s != null ? tipRow(null, 'wall', `${r.wall_s.toFixed(1)}s`) : '');
    placeTip(this.tip, this.host, near.px / px, near.py / px);
  }
}
