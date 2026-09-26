/* -------------------------------------------------------------------------
   Plot - what every chart shares: the host box, the tooltip and the empty
   state, lazy creation, resizing, and drawing only what is on screen.
   ------------------------------------------------------------------------- */

import { ANNOT } from './annotations.js';
import { ZOOM, observeSize, uPlot } from './core.js';

export class Plot {
  constructor(host, opt, defaults) {
    this.host = host;
    this.opt = Object.assign({}, defaults, opt);
    this.u = null;
    this.dirty = true;
    // min-height, not height: .plot is flex:1 inside the card, so the plot
    // absorbs whatever extra height the grid row gives the card.
    host.style.minHeight = `${this.opt.height}px`;
    this.tip = document.createElement('div');
    this.tip.className = 'tip';
    host.appendChild(this.tip);
    this.empty = document.createElement('div');
    this.empty.className = 'plot-empty';
    this.empty.textContent = this.opt.emptyText || 'No data in this range';
    host.appendChild(this.empty);
    this.ro = observeSize(host, () => this.draw());
  }

  /* Draw now if the plot is on screen, otherwise the next time it is: a
     chart on a hidden page keeps its data and costs nothing. Also the
     redraw after a theme change or new annotations - colours are read at
     draw time. */
  draw() {
    const empty = !this.hasData();
    this.host.classList.toggle('is-empty', empty);
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (w < 40 || h < 30) return;
    if (!this.u) {
      this.u = new uPlot(this._opts(w, h), this._data(), this.host);
      this.dirty = false;
      this._mounted?.();
      return;
    }
    if (this.u.width !== w || this.u.height !== h) this.u.setSize({ width: w, height: h });
    if (this.dirty) { this.dirty = false; this.u.setData(this._data()); } else this.u.redraw(false);
  }

  _changed() { this.dirty = true; this.draw(); }

  destroy() {
    this.ro?.disconnect();
    this.u?.destroy();
    this.u = null;
    ANNOT.charts.delete(this);
    ZOOM.charts.delete(this);
  }

  _base(w, h) {
    return {
      width: w, height: h,
      legend: { show: false },
      padding: [10, 14, 0, 0],
    };
  }
}
