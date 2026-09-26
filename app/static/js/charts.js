/* ==========================================================================
   Charts for the dashboard, drawn with uPlot.

   uPlot (static/vendor/uplot, MIT, loaded as a classic script before the
   app) does the plumbing: scales, axes, the grid, the cursor and its sync
   across charts, drag selection, resizing, and a year of history without
   breaking a sweat. It is vendored, not pulled from a CDN, so the container
   needs no outbound network at render time.

   What is drawn follows one spec on every chart: 2px lines, a hairline solid
   grid, 4px rounded data-ends, >=8px hover markers, a tooltip beside the
   cursor on every plot and keyboard parity with hover. Marks uPlot has no
   notion of - event annotations, thresholds, rounded bars, the slot timeline,
   the depth scatter - are drawn in its hooks, on its canvas and its scales.

   Every chart keeps the same small API: setData(), draw(), destroy(),
   hasData(). A chart on a hidden page keeps its data and draws when shown.
   ========================================================================== */

export { setAnnotations, showAnnotations } from './charts/annotations.js';
export { BarChart } from './charts/bars.js';
export { CSS, fmtFull, fmtTime, onZoom } from './charts/core.js';
export { LineChart, Sparkline } from './charts/line.js';
export { ScatterChart } from './charts/scatter.js';
export { TimelineChart } from './charts/timeline.js';
