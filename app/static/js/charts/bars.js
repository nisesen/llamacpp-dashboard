/* -------------------------------------------------------------------------
   BarChart - categories along x, one series or a stack. 4px rounded
   data-ends anchored to the baseline, a 2px surface gap between neighbours.
   ------------------------------------------------------------------------- */

import { CSS, PX, alpha, axisBase, esc, hideTip, niceStep, placeTip, repaint, textWidth, tipRow, valueAxis } from './core.js';
import { Plot } from './plot.js';

export class BarChart extends Plot {
  constructor(host, opt) {
    super(host, opt, { height: 170, color: '--series-1', format: (v) => String(Math.round(v)) });
    this.data = [];
    this.hot = null;
  }

  setData(data) { this.data = data || []; this._changed(); }

  _parts(d) {
    return this.opt.stack
      ? this.opt.stack.map((s) => [s, d[s.key] || 0])
      : [[{ color: this.opt.color, label: this.opt.seriesLabel || 'value' }, d.value || 0]];
  }

  _total(d) { return this._parts(d).reduce((a, [, v]) => a + v, 0); }

  hasData() { return this.data.some((d) => this._total(d) > 0); }

  /* Headroom to the next round number, so the tallest bar never touches the
     top edge and the top gridline has a label worth reading. */
  _top() {
    const hi = Math.max(this.opt.minMax || 1, ...this.data.map((d) => this._total(d)));
    const step = niceStep(hi / 4);
    return Math.ceil(hi / step - 1e-9) * step;
  }

  _data() {
    const n = this.data.length;
    return [Array.from({ length: n }, (_, i) => i), this.data.map((d) => this._total(d))];
  }

  /* Label every Nth bar, N chosen so the widest label never collides. */
  _xSplits(u) {
    const n = this.data.length;
    if (!n) return [];
    const step = u.bbox.width / PX() / n;
    const lw = Math.max(...this.data.map((d) => textWidth(d.label)));
    const every = Math.max(1, Math.ceil((lw + 8) / Math.max(1, step)));
    const out = [];
    for (let i = 0; i < n; i += every) out.push(i);
    return out;
  }

  _opts(w, h) {
    return {
      ...this._base(w, h),
      scales: {
        x: { time: false, range: () => [-0.5, Math.max(1, this.data.length) - 0.5] },
        y: { range: () => [0, this._top()] },
      },
      axes: [
        {
          ...axisBase(), size: 24, gap: 6, grid: { show: false },
          border: { show: true, stroke: () => CSS('--axis'), width: 1 },
          splits: (u) => this._xSplits(u),
          values: (u, splits) => splits.map((i) => this.data[i]?.label ?? ''),
        },
        valueAxis(this.opt.format, { space: 30 }),
      ],
      series: [{}, { paths: () => null, points: { show: false } }],
      cursor: { x: false, y: false, points: { show: false }, drag: { x: false, y: false } },
      hooks: {
        draw: [(u) => this._drawBars(u)],
        setCursor: [(u) => this._cursor(u)],
      },
    };
  }

  _drawBars(u) {
    const n = this.data.length;
    if (!n) return;
    const ctx = u.ctx, px = PX();
    const { left, width } = u.bbox;
    const step = width / n;
    const bw = Math.max(3 * px, step - 2 * px);         // the 2px surface gap
    ctx.save();
    this.data.forEach((d, i) => {
      const x = left + i * step + (step - bw) / 2;
      const dim = this.hot != null && this.hot !== i;
      const parts = this._parts(d).filter(([, v]) => v > 0);
      let base = 0;
      parts.forEach(([s, v], j) => {
        const y0 = u.valToPos(base, 'y', true), y1 = u.valToPos(base + v, 'y', true);
        const hgt = Math.max(px, y0 - y1);
        ctx.fillStyle = alpha(CSS(s.color), dim ? 0.5 : 1);
        ctx.beginPath();
        // Only the top of the whole bar is rounded, never a joint.
        const r = j === parts.length - 1 ? [4 * px, 4 * px, 0, 0] : 0;
        if (ctx.roundRect) ctx.roundRect(x, y0 - hgt, bw, hgt, r);
        else ctx.rect(x, y0 - hgt, bw, hgt);
        ctx.fill();
        base += v;
      });
    });
    ctx.restore();
  }

  _cursor(u) {
    const c = u.cursor;
    const i = c.left != null && c.left >= 0 ? c.idx : null;
    if (i !== this.hot) { this.hot = i; repaint(u); }
    const d = i != null ? this.data[i] : null;
    if (!d) { hideTip(this.tip); return; }
    const parts = this._parts(d);
    this.tip.innerHTML = `<div class="when">${esc(this.opt.xLabel ? `${this.opt.xLabel} ` : '')}${esc(d.tipLabel || d.label)}</div>`
      + parts.map(([s, v]) => tipRow(CSS(s.color), s.label,
        !this.opt.stack && this.opt.tipFormat ? this.opt.tipFormat(d) : this.opt.format(v))).join('');
    const px = PX();
    const x = u.bbox.left / px + u.valToPos(i, 'x');
    const y = u.bbox.top / px + u.valToPos(this._total(d), 'y');
    placeTip(this.tip, this.host, x, Math.max(y, u.bbox.top / px + 20));
  }
}
