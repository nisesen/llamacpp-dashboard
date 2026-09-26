/* Every chart on the page: their construction, legends, table twins, and the
   redraw that feeds them the current window. */

import { BarChart, LineChart, ScatterChart, TimelineChart, fmtFull } from './charts.js';
import { AGENTLESS, DEPTH_PREFILL_MIN, LIVE_MAX, RANGE_SECONDS, currentPoints, domain, isProbe, latencyPoints, latencyRows, probeOn, requestPoints, state, workloadRows } from './state.js';
import { BINARY_INCRS, el, esc, fmtAxisSecs, fmtSecs, gpuColor, kfmt, latencySplits, nf, rateBytes, usageLabel } from './util.js';

/* -------------------------------------------------------------- charting */

export const charts = {};
export const tableBuilders = {};

/* A swatch follows the theme by naming the custom property, not its value. */
export const swatchBg = (color) => (color.startsWith('--') ? `var(${color})` : color);

/* A line chart's legend is a row of switches: click an entry to hide its
   line (the y axis rescales to what is left), hover one to bring its line
   forward. Other legends only name what the colours mean. */
export function legend(hostId, series, chartId) {
  const host = el(hostId);
  if (!host) return;
  const c = chartId ? charts[chartId]?.chart : null;
  const sw = (s) => `<span class="swatch" style="background:${swatchBg(s.color)}${
    s.opacity != null && s.opacity < 1 ? `;opacity:${s.opacity}` : ''}"></span>`;
  host.innerHTML = series.map((s) => (c?.toggle
    ? `<button type="button" class="item" data-chart="${esc(chartId)}" data-key="${esc(s.key)}"
        aria-pressed="${c.isShown(s.key)}" title="Show or hide ${esc(s.label)}">${sw(s)}${esc(s.label)}</button>`
    : `<span class="item">${sw(s)}${esc(s.label)}</span>`)).join('');
}

document.addEventListener('click', (ev) => {
  const b = ev.target.closest('.legend button.item[data-chart]');
  if (!b) return;
  const c = charts[b.dataset.chart]?.chart;
  if (!c) return;
  c.toggle(b.dataset.key);
  b.setAttribute('aria-pressed', String(c.isShown(b.dataset.key)));
  c.focus(null);
});
document.addEventListener('mouseover', (ev) => {
  const b = ev.target.closest('.legend button.item[data-chart]');
  if (b && b.getAttribute('aria-pressed') === 'true') charts[b.dataset.chart]?.chart.focus(b.dataset.key);
});
document.addEventListener('mouseout', (ev) => {
  const b = ev.target.closest('.legend button.item[data-chart]');
  if (b && !b.contains(ev.relatedTarget)) charts[b.dataset.chart]?.chart.focus(null);
});

function mkLine(id, opt) {
  const host = el(id);
  if (!host) return null;
  const c = new LineChart(host, opt);
  charts[id] = { chart: c, opt };
  if (opt.legendId && opt.series.length > 1) legend(opt.legendId, visibleSeries(opt.series), id);
  tableBuilders[id] = () => seriesTable(visibleSeries(opt.series), opt.format, opt.unit,
                                        charts[id].lastData);
  return c;
}

/* The occupancy legend names the probe only when a probe fingerprint is set,
   which is known once the first snapshot arrives. */
export function occLegend() {
  legend('lg-occ', [
    { label: 'warm (prompt reused)', color: '--series-3' },
    { label: 'cold prefill (≥8k computed)', color: '--series-2' },
    { label: 'probe', color: '--muted', probe: true },
  ].filter((s) => !s.probe || probeOn()));
}

/* Peak series only exist in stored history: the live buffer IS every sample. */
function visibleSeries(series) {
  const long = RANGE_SECONDS[state.range] > LIVE_MAX;
  return series.filter((s) => (long || !s.peak) && (!s.probe || probeOn()));
}

export function refreshLegends() {
  for (const id in charts) {
    const o = charts[id].opt;
    if (o.legendId && o.series?.length > 1) legend(o.legendId, visibleSeries(o.series), id);
  }
}

/* Every chart has a table twin: the same numbers, WCAG-clean, no colour needed. */
function seriesTable(series, format, unit, data) {
  const [t0, t1] = domain();
  const pts = (data || currentPoints()).filter((p) => p.ts >= t0 && p.ts <= t1
    && series.some((s) => p[s.key] != null && isFinite(p[s.key])));
  const step = Math.max(1, Math.ceil(pts.length / 180));
  const rows = [];
  for (let i = pts.length - 1; i >= 0; i -= step) rows.push(pts[i]);
  return `<div class="tablewrap"><table class="data">
    <thead><tr><th>Time</th>${series.map((s) =>
      `<th class="num"><span class="swatch" style="background:${swatchBg(s.color)}"></span>${esc(s.label)}${unit ? ' (' + esc(unit) + ')' : ''}</th>`).join('')}</tr></thead>
    <tbody>${rows.map((p) => `<tr><td>${fmtFull(p.ts)}</td>${series.map((s) => {
      const v = p[s.key];
      return `<td class="num">${v == null || !isFinite(v) ? '—' : esc(format ? format(v) : v)}</td>`;
    }).join('')}</tr>`).join('')}</tbody></table></div>`;
}

document.addEventListener('click', (ev) => {
  const copy = ev.target.closest('[data-copy]');
  if (copy) {
    navigator.clipboard?.writeText(copy.dataset.copy).then(() => {
      const old = copy.textContent;
      copy.textContent = 'copied';
      setTimeout(() => { copy.textContent = old; }, 1200);
    }).catch(() => {});
    return;
  }

  const btn = ev.target.closest('[data-table]');
  if (!btn) return;
  const id = btn.dataset.table;
  const pressed = btn.getAttribute('aria-pressed') === 'true';

  if (charts[id]) {
    const host = el(id);
    if (pressed) {
      document.getElementById(id + '-twin')?.remove();
      host.hidden = false;
      btn.setAttribute('aria-pressed', 'false');
      btn.textContent = 'table';
      charts[id].chart.draw();
    } else {
      const twin = document.createElement('div');
      twin.id = id + '-twin';
      twin.innerHTML = tableBuilders[id]();
      host.after(twin);
      host.hidden = true;
      btn.setAttribute('aria-pressed', 'true');
      btn.textContent = 'chart';
    }
    return;
  }

  const target = el(id);
  if (target) {
    target.hidden = pressed;
    if (!pressed && tableBuilders[id]) target.innerHTML = tableBuilders[id]();
    btn.setAttribute('aria-pressed', String(!pressed));
    btn.textContent = pressed ? 'table' : 'hide';
  }
});

export function redrawAll() {
  const pts = currentPoints();
  const d = domain();
  let reqPts = null;
  for (const id in charts) {
    if (charts[id].bars) continue;            // categorical, not a time series
    if (charts[id].requests) continue;        // fed from /api/requests instead
    if (charts[id].opt.perRequest) {
      // One mark per completed request, from the request log - every request
      // in the range, not the one-in-five a stored sample happened to catch.
      reqPts = reqPts || requestPoints();
      const merged = charts[id].opt.withSamples
        ? [...reqPts, ...pts].sort((a, b) => a.ts - b.ts) : reqPts;
      charts[id].lastData = merged;
      charts[id].chart.setData(merged, d);
      continue;
    }
    charts[id].lastData = null;
    charts[id].chart.setData(pts, d);
  }
  for (const id in charts) {
    const twin = document.getElementById(id + '-twin');
    if (twin) twin.innerHTML = tableBuilders[id]();
  }
}

/* ------------------------------------------------------------ chart setup */

export function buildBaseCharts() {
  if (AGENTLESS) {
    // Without the agent there are no per-request timings: plot what each poll
    // read from llama.cpp's own counters instead.
    mkLine('c-decode', {
      series: [{ key: 'decode_tps', label: 'Decode', color: '--series-1' }],
      mode: 'dots', unit: 'tok/s', height: 200,
      format: (v) => nf(v, 0), tipFormat: (v) => v.toFixed(1),
      ariaLabel: 'Decode throughput from llama.cpp counters over time',
    });
    mkLine('c-prefill', {
      series: [{ key: 'prefill_tps', label: 'Prefill', color: '--series-2' }],
      mode: 'dots', unit: 'tok/s', height: 200, format: (v) => nf(v),
      ariaLabel: 'Prefill throughput from llama.cpp counters over time',
    });
    mkLine('c-ratios', {
      series: [
        { key: 'accept_pct', label: 'Draft acceptance', color: '--series-3' },
        { key: 'cache_pct', label: 'Prompt from cache', color: '--series-7' },
      ],
      legendId: 'lg-ratios', mode: 'dots', unit: '%', height: 190, yMin: 0, yMax: 100,
      format: (v) => v.toFixed(0),
      ariaLabel: 'Draft acceptance and prompt cache hits from llama.cpp counters over time',
    });
  } else {
    mkLine('c-decode', {
      series: [
        { key: 'wl_decode', label: 'Workload', color: '--series-1' },
        { key: 'probe_decode', label: 'Probe', color: '--muted', probe: true },
        { key: 'tg_live', label: 'Live (3s window)', color: '--series-3', mode: 'line' },
      ],
      legendId: 'lg-decode', mode: 'dots', unit: 'tok/s', height: 200,
      perRequest: true, withSamples: true,
      format: (v) => nf(v, 0), tipFormat: (v) => v.toFixed(1),
      ariaLabel: 'Decode throughput per completed request over time',
    });
    mkLine('c-prefill', {
      series: [{ key: 'wl_prefill', label: 'Prefill', color: '--series-2' }],
      mode: 'dots', unit: 'tok/s', height: 200, format: (v) => nf(v), perRequest: true,
      ariaLabel: 'Prefill throughput per completed request over time',
    });
    mkLine('c-ratios', {
      series: [
        { key: 'accept_pct', label: 'Draft acceptance', color: '--series-3' },
        { key: 'reuse_pct', label: 'Prompt reused', color: '--series-7' },
      ],
      legendId: 'lg-ratios', mode: 'dots', unit: '%', height: 190, yMin: 0, yMax: 100,
      perRequest: true, format: (v) => v.toFixed(0),
      ariaLabel: 'Draft acceptance and prompt reuse per workload request over time',
    });
  }
  mkLine('c-conc', {
    series: [
      { key: 'req_proc', label: 'Processing', color: '--series-1' },
      { key: 'req_def', label: 'Deferred', color: '--series-8' },
      { key: 'busy_slots', label: 'Busy slots/decode', color: '--series-4' },
    ],
    legendId: 'lg-conc', height: 190, yMin: 0, stepped: true,
    format: (v) => v.toFixed(v % 1 ? 1 : 0),
    ariaLabel: 'Requests processing, deferred, and busy slots over time',
  });
  mkLine('c-cpu', {
    series: [{ key: 'cpu_pct', label: 'Host CPU', color: '--series-1', area: true }],
    unit: '%', height: 190, yMin: 0, yMax: 100, format: (v) => v.toFixed(0),
    ariaLabel: 'Host CPU utilisation over time',
  });
  mkLine('c-temps', {
    series: [
      { key: 'cpu_temp', label: 'CPU', color: '--series-5' },
      { key: 'sys_temp', label: 'System', color: '--series-3' },
    ],
    legendId: 'lg-temps', unit: '°C', height: 190, zeroAnchor: false,
    format: (v) => v.toFixed(0),
    ariaLabel: 'Chassis CPU and system temperature over time',
  });
  mkLine('c-net', {
    series: [
      { key: 'net_rx', label: 'Receive', color: '--series-1' },
      { key: 'net_tx', label: 'Transmit', color: '--series-2' },
    ],
    // Binary steps, so the axis reads 8, 16, 24 KiB/s rather than 9.8, 19.5.
    legendId: 'lg-net', height: 200, yMin: 0, format: (v) => rateBytes(v),
    axisFormat: (v) => rateBytes(v).replace('.0 ', ' '), incrs: BINARY_INCRS,
    ariaLabel: 'Network receive and transmit rate over time',
  });
  mkLine('c-rail', {
    series: [{ key: 'v12', label: 'Rail', color: '--series-4' }],
    unit: 'V', height: 200, zeroAnchor: false, minSpanFrac: 0.08,
    thresholdsExpandDomain: false, format: (v) => v.toFixed(2),
    ariaLabel: 'Voltage rail over time',
  });

  charts['c-spec'] = {
    chart: new BarChart(el('c-spec'), {
      height: 190, color: '--series-1', xLabel: 'Draft position',
      seriesLabel: 'Accepted tokens', format: (v) => nf(v),
      ariaLabel: 'Accepted draft tokens by speculative draft position',
    }),
    opt: {}, bars: true,
  };
  tableBuilders['c-spec'] = () => {
    const d = state.snap.spec_positions || [];
    const total = d.reduce((a, b) => a + (b.accepted || 0), 0) || 1;
    return `<div class="tablewrap"><table class="data">
      <thead><tr><th>Position</th><th class="num">Accepted</th><th class="num">Share</th></tr></thead>
      <tbody>${d.map((p) => `<tr><td>${p.position}</td><td class="num">${nf(p.accepted)}</td>
        <td class="num">${(100 * p.accepted / total).toFixed(1)}%</td></tr>`).join('')}</tbody></table></div>`;
  };

  // Overview: one timeline in two lanes - requests over GPU power - sharing
  // the cursor and the event markers, so "busy" and "what happened" read at
  // the same instant.
  mkLine('c-act-req', {
    series: [
      { key: 'req_proc', label: 'Running', color: '--series-5', area: true },
      { key: 'req_def', label: 'Waiting for a slot', color: '--series-8' },
    ],
    legendId: 'lg-act-req', height: 96, stepped: true, yMin: 0,
    format: (v) => nf(v), ariaLabel: 'Requests running and waiting over time',
  });
  mkLine('c-act-pwr', {
    series: [{ key: '_gpupwr', label: 'GPU power', color: '--series-6', area: true }],
    height: 116, unit: 'W', format: (v) => nf(v),
    ariaLabel: 'Total GPU power over time',
  });

  // Latency: one mark per workload request, log10 seconds on the y axis -
  // warm requests take tens of milliseconds and cold ones minutes, and a
  // linear axis would flatten every warm one onto zero.
  mkLine('c-latency', {
    series: [
      { key: 'lat_warm', label: 'First token · warm', color: '--series-3' },
      { key: 'lat_cold', label: 'First token · cold prefill', color: '--series-2' },
      { key: 'lat_e2e', label: 'End to end', color: '--series-7' },
    ],
    legendId: 'lg-latency', mode: 'dots', height: 210, zeroAnchor: false,
    splits: latencySplits,
    format: (v) => fmtAxisSecs(10 ** v), tipFormat: (v) => fmtSecs(10 ** v),
    ariaLabel: 'Time to first token and end-to-end time per request',
  });
  Object.assign(charts['c-latency'], { requests: true, timeline: true, rows: latencyPoints });
  tableBuilders['c-latency'] = () => `<div class="tablewrap"><table class="data">
    <thead><tr><th>Finished</th><th class="num">First token</th><th class="num">End to end</th>
    <th class="num">Prompt computed</th><th class="num opt">Generated</th><th>Kind</th></tr></thead>
    <tbody>${latencyRows().slice(0, 200).map((r) => `<tr><td>${fmtFull(r.finished)}</td>
      <td class="num">${fmtSecs(r.ttft)}</td><td class="num">${r.e2e != null ? fmtSecs(r.e2e) : '—'}</td>
      <td class="num">${nf(r.prompt_tokens)}</td><td class="num opt">${nf(r.gen_tokens)}</td>
      <td>${r.cold ? 'cold prefill' : 'warm'}</td></tr>`).join('')}</tbody></table></div>`;

  // Energy & usage: per-hour or per-day totals from /api/usage, refreshed on
  // their own cadence like the bar chart above.
  charts['c-usage-tok'] = {
    chart: new BarChart(el('c-usage-tok'), {
      height: 170, format: kfmt, ariaLabel: 'Tokens generated per hour or per day',
      stack: [{ key: 'gen_wl', label: 'Workload', color: '--series-1' },
              { key: 'gen_probe', label: 'Probe', color: '--muted' }],
    }),
    opt: {}, bars: true,
  };
  charts['c-usage-wh'] = {
    chart: new BarChart(el('c-usage-wh'), {
      height: 170, color: '--series-6', seriesLabel: 'GPU energy',
      format: (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)} kWh` : `${Math.round(v)} Wh`),
      ariaLabel: 'GPU energy per hour or per day',
    }),
    opt: {}, bars: true,
  };
  tableBuilders['c-usage-tok'] = () => {
    const u = state.usage;
    if (!u) return '';
    return `<div class="tablewrap"><table class="data">
      <thead><tr><th>Period</th><th class="num">Generated</th><th class="num opt">Probe</th>
      <th class="num">Prompt computed</th><th class="num">Requests</th><th class="num">GPU energy</th></tr></thead>
      <tbody>${u.buckets.slice().reverse().map((b) => `<tr><td>${esc(usageLabel(b.ts, u.bucket, true))}</td>
        <td class="num">${nf(b.gen_wl)}</td><td class="num opt">${nf(b.gen_probe)}</td>
        <td class="num">${nf(b.prompt_wl)}</td><td class="num">${nf(b.req_wl)}</td>
        <td class="num">${nf(b.gpu_wh, 0)} Wh</td></tr>`).join('')}</tbody></table></div>`;
  };

  // Request-backed charts. These read /api/requests rather than the sample
  // buffer, so they are refreshed on their own cadence.
  charts['c-depth-prefill'] = {
    chart: new ScatterChart(el('c-depth-prefill'), {
      xKey: 'n_tokens', xLabel: 'context depth (tokens)', unit: 'tok/s',
      series: [{ key: 'prefill_tps', label: 'Prefill', color: '--series-2' }],
      height: 210, format: (v) => nf(v), xFormat: (v) => nf(v),
      ariaLabel: 'Prefill throughput against context depth, one mark per request',
    }),
    opt: {}, requests: true,
    // Real prompt work only: a 4-token tail after a cache hit is not a rate.
    rows: () => workloadRows().filter((r) => (r.prompt_tokens || 0) >= DEPTH_PREFILL_MIN),
  };
  charts['c-depth-decode'] = {
    chart: new ScatterChart(el('c-depth-decode'), {
      xKey: 'n_tokens', xLabel: 'context depth (tokens)', unit: 'tok/s',
      series: [{ key: 'decode_tps', label: 'Decode', color: '--series-1' }],
      height: 210, format: (v) => nf(v, 1), xFormat: (v) => nf(v),
      ariaLabel: 'Decode throughput against context depth, one mark per request',
    }),
    opt: {}, requests: true,
    rows: () => workloadRows().filter((r) => (r.gen_tokens || 0) >= 24),
  };
  charts['c-occupancy'] = {
    chart: new TimelineChart(el('c-occupancy'), {
      height: 150, ariaLabel: 'Slot occupancy over time',
      coldTokens: 8192,
    }),
    opt: {}, requests: true, timeline: true,
    rows: () => state.requests || [],
  };

  const reqTable = (cols, id) => () => {
    const rows = charts[id]?.rows ? charts[id].rows() : (state.requests || []);
    if (!rows.length) return '<div class="empty">No completed requests in this range.</div>';
    return `<div class="tablewrap"><table class="data"><thead><tr>${
      cols.map(([h]) => `<th class="num">${esc(h)}</th>`).join('')}</tr></thead><tbody>${
      rows.slice(0, 200).map((r) => `<tr>${
        cols.map(([, f]) => `<td class="num">${esc(f(r))}</td>`).join('')}</tr>`).join('')
    }</tbody></table></div>`;
  };
  tableBuilders['c-depth-prefill'] = reqTable([
    ['When', (r) => fmtFull(r.finished)], ['Depth', (r) => nf(r.n_tokens)],
    ['Prompt computed', (r) => nf(r.prompt_tokens)], ['Prefill tok/s', (r) => nf(r.prefill_tps)]],
  'c-depth-prefill');
  tableBuilders['c-depth-decode'] = reqTable([
    ['When', (r) => fmtFull(r.finished)], ['Depth', (r) => nf(r.n_tokens)],
    ['Generated', (r) => nf(r.gen_tokens)], ['Decode tok/s', (r) => nf(r.decode_tps, 1)]],
  'c-depth-decode');
  tableBuilders['c-occupancy'] = reqTable([
    ['Slot', (r) => r.slot], ['Task', (r) => r.task],
    ['Start', (r) => (r.started ? fmtFull(r.started) : '—')],
    ['Wall s', (r) => (r.wall_s != null ? r.wall_s.toFixed(1) : '—')],
    ['Computed', (r) => nf(r.prompt_tokens)],
    ['Kind', (r) => (isProbe(r) ? 'probe' : (r.prompt_tokens || 0) >= 8192 ? 'cold prefill' : 'warm')]],
  'c-occupancy');

  occLegend();

  tableBuilders['cores-table'] = () => {
    const per = (state.snap.cpu || {}).per_core || [];
    return `<div class="tablewrap"><table class="data">
      <thead><tr><th>Core</th><th class="num">Busy %</th><th>Core</th><th class="num">Busy %</th></tr></thead>
      <tbody>${per.map((v, i) => (i % 2 === 0
        ? `<tr><td>cpu${i}</td><td class="num">${v == null ? '—' : v.toFixed(0)}</td>` +
          `<td>cpu${i + 1}</td><td class="num">${per[i + 1] == null ? '—' : per[i + 1].toFixed(0)}</td></tr>`
        : '')).join('')}</tbody></table></div>`;
  };
}

/* GPU charts are rebuilt whenever the number of cards changes, so adding or
   losing one needs no edit here. */
export function buildGpuCharts(snap) {
  const gpus = snap.gpus || [];
  if (state.gpuCount === gpus.length) return;
  state.gpuCount = gpus.length;
  const idxs = gpus.map((g) => g.idx);

  const capacities = [...new Set(gpus.map((g) => Math.round((g.mem_total || 0) / 1024)))];
  const caps = [...new Set(gpus.map((g) => g.pwr_limit).filter(Boolean))];
  const memWarn = gpus.map((g) => (g.limits || {}).temp_mem_warn).filter(Boolean);

  for (const [id, key, label, opt] of [
    ['c-power', 'pwr', 'Power', {
      unit: 'W', yMin: 0, format: (v) => v.toFixed(0), peaks: true,
      thresholds: caps.length === 1 ? [{ value: caps[0], label: `${caps[0]} W cap`, color: '--warning' }] : [],
    }],
    ['c-hbm', 'hbm', 'Memory temperature', {
      unit: '°C', zeroAnchor: false, format: (v) => v.toFixed(0), peaks: true,
      thresholds: memWarn.length ? [{ value: Math.min(...memWarn), label: 'warn', color: '--warning' }] : [],
    }],
    ['c-util', 'util', 'Utilisation', { unit: '%', yMin: 0, yMax: 100, format: (v) => v.toFixed(0) }],
    ['c-vram', 'mem', 'VRAM', { unit: '%', yMin: 0, yMax: 100, format: (v) => v.toFixed(0) }],
  ]) {
    const host = el(id);
    if (!host) continue;
    if (charts[id]) charts[id].chart.destroy?.();
    host.innerHTML = '';
    document.getElementById(id + '-twin')?.remove();
    const series = idxs.map((i) => ({
      key: `g${i}_${key}`, label: `GPU${i}`, color: gpuColor(i), area: gpus.length === 1,
    }));
    // Over hours, a bucket's mean hides the spike that matters - a multi-day
    // power chart can top out at a fraction of the real peak draw. The peak
    // rides along as a faint line; live ranges plot every sample anyway.
    if (opt.peaks) {
      for (const i of idxs) {
        series.push({ key: `g${i}_${key}_max`, label: `GPU${i} peak`, color: gpuColor(i),
                      width: 1, opacity: 0.45, peak: true });
      }
    }
    mkLine(id, Object.assign({
      series, height: 190, legendId: 'lg-' + id.slice(2),
      ariaLabel: `${label} per GPU over time`,
    }, opt));
  }

  el('vram-sub').textContent = capacities.length === 1
    ? `percent of ${capacities[0]} GiB per card` : 'percent per card';
  el('power-sub').textContent = caps.length === 1
    ? `watts per card, against a ${caps[0]} W cap` : 'watts per card';
  const shut = gpus.map((g) => (g.limits || {}).temp_shutdown).filter(Boolean);
  el('hbm-sub').textContent = shut.length
    ? `memory junction, where reported · shutdown ${Math.min(...shut)} °C` : 'memory junction, where the card reports it';
}
