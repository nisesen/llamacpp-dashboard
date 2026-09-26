/* -------------------------------------------------------------------------
   TimelineChart - which slot was busy when. Shares the page's window, its
   cursor, its zoom and its event markers with the time charts.
   ------------------------------------------------------------------------- */

import { ANNOT, annotationRows, annotationsNear, drawAnnotations } from './annotations.js';
import { CSS, PX, ZOOM, axisBase, esc, fmtFull, hideTip, placeTip, repaint, syncOpts, textWidth, timeAxis, tipRow, zoomCursor, zoomOnSelect } from './core.js';
import { Plot } from './plot.js';

export class TimelineChart extends Plot {
  constructor(host, opt) {
    super(host, opt, { height: 150, laneH: 22, emptyText: 'No completed requests in this range' });
    this.rows = [];
    this.lanes = [];
    this.domain = null;
    this.hot = null;
    ANNOT.charts.add(this);
    ZOOM.charts.add(this);
  }

  setData(rows, domain) {
    this.rows = rows || [];
    this.domain = domain || null;
    const lanes = [...new Set(this.rows.map((r) => r.slot))].filter((v) => v != null).sort((a, b) => a - b);
    // A new lane changes the axis: rebuild rather than patch.
    if (this.u && lanes.join() !== this.lanes.join()) { this.u.destroy(); this.u = null; }
    this.lanes = lanes;
    this._changed();
  }

  hasData() { return this.lanes.length > 0; }

  _data() {
    const [t0, t1] = this.domain || [Date.now() / 1000 - 3600, Date.now() / 1000];
    return [[t0, t1], [null, null]];
  }

  /* Lane i is centred on n - i - 0.5, so slot 0 is on top. */
  _laneY(u, i) { return u.valToPos(this.lanes.length - i - 0.5, 'y', true); }

  _opts(w, h) {
    const n = Math.max(1, this.lanes.length);
    const labelW = Math.max(0, ...this.lanes.map((s) => textWidth(`slot ${s}`)));
    return {
      ...this._base(w, h),
      padding: [6, 14, 0, 0],
      scales: {
        x: { time: true, range: () => this._data()[0] },
        y: { range: () => [0, n] },
      },
      axes: [
        timeAxis(),
        {
          ...axisBase(), side: 3, gap: 7, size: Math.max(30, Math.ceil(labelW) + 13),
          grid: { show: false },
          splits: () => this.lanes.map((_, i) => n - i - 0.5),
          values: (u, splits) => splits.map((v) => `slot ${this.lanes[n - 1 - Math.floor(v)]}`),
        },
      ],
      series: [{}, { paths: () => null, points: { show: false } }],
      cursor: {
        y: false, points: { show: false },
        sync: syncOpts(),
        ...zoomCursor(),
      },
      hooks: {
        drawAxes: [drawAnnotations],
        draw: [(u) => this._drawBars(u)],
        setCursor: [(u) => this._cursor(u)],
        setSelect: [zoomOnSelect],
      },
    };
  }

  _bars(u) {
    const px = PX();
    const { left, width, height } = u.bbox;
    const laneH = height / Math.max(1, this.lanes.length);
    const bh = Math.max(4 * px, Math.min(laneH - 8 * px, 14 * px));
    const t0 = u.scales.x.min, t1 = u.scales.x.max;
    const out = [];
    this.lanes.forEach((slot, li) => {
      const cy = this._laneY(u, li);
      for (const r of this.rows) {
        if (r.slot !== slot) continue;
        const a = r.started ?? r.finished, b = r.finished;
        if (a == null || b == null || b < t0 || a > t1) continue;
        const x1 = Math.max(left, u.valToPos(a, 'x', true));
        const x2 = Math.min(left + width, u.valToPos(b, 'x', true));
        out.push({ r, x: x1, y: cy - bh / 2, w: Math.max(2 * px, x2 - x1), h: bh });
      }
    });
    return out;
  }

  _drawBars(u) {
    const ctx = u.ctx, px = PX();
    ctx.save();
    for (const bar of this._bars(u)) {
      const { r } = bar;
      // Colour by what the request cost, not by rank: a cold prefill (a big
      // prompt computed from scratch) is the expensive kind, and a probe
      // request is background, so it recedes.
      const cold = (r.prompt_tokens || 0) >= (this.opt.coldTokens || 8192);
      const hot = this.hot && this.hot.r === r;
      ctx.fillStyle = CSS(r.probe ? '--muted' : cold ? '--series-2' : '--series-3');
      ctx.globalAlpha = hot ? 1 : r.probe ? 0.45 : 0.85;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(bar.x, bar.y, bar.w, bar.h, 3 * px);
      else ctx.rect(bar.x, bar.y, bar.w, bar.h);
      ctx.fill();
      if (hot) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = CSS('--text'); ctx.lineWidth = 1.5 * px;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  _cursor(u) {
    const c = u.cursor, px = PX();
    const owned = c.event != null;
    let near = null;
    if (owned && c.left != null && c.left >= 0) {
      const cx = c.left * px + u.bbox.left, cy = c.top * px + u.bbox.top;
      for (const bar of this._bars(u)) {
        if (cx >= bar.x - 2 * px && cx <= bar.x + bar.w + 2 * px
            && cy >= bar.y - 3 * px && cy <= bar.y + bar.h + 3 * px) near = bar;
      }
    }
    if ((near?.r || null) !== (this.hot?.r || null)) { this.hot = near; repaint(u); }
    if (near) {
      const r = near.r;
      const n = (v) => (v != null ? Math.round(v).toLocaleString() : '—');
      const prefillS = r.prompt_tokens && r.prefill_tps ? r.prompt_tokens / r.prefill_tps : null;
      this.tip.innerHTML = `<div class="when">task ${esc(r.task ?? '—')} · slot ${esc(r.slot)}${r.probe ? ' · probe' : ''}</div>`
        + tipRow(null, 'finished', fmtFull(r.finished))
        + tipRow(null, 'wall', r.wall_s != null ? `${r.wall_s.toFixed(1)}s` : '—')
        + tipRow(null, 'context', `${n(r.n_tokens)} tok`)
        + tipRow(null, 'prompt computed', `${n(r.prompt_tokens)} tok${
          prefillS != null && prefillS >= 1 ? ` · ${prefillS.toFixed(0)}s` : ''}`)
        + tipRow(null, 'generated', `${n(r.gen_tokens)} tok`)
        + tipRow(null, 'decode', r.decode_tps != null ? `${r.decode_tps.toFixed(1)} tok/s` : '—');
      placeTip(this.tip, this.host, (near.x + near.w / 2) / px, (near.y + near.h / 2) / px);
      return;
    }
    const annots = owned ? annotationsNear(u, c.left) : [];
    if (annots.length) {
      this.tip.innerHTML = annotationRows(annots);
      placeTip(this.tip, this.host, u.bbox.left / px + c.left, u.bbox.top / px + c.top);
      return;
    }
    hideTip(this.tip);
  }
}
