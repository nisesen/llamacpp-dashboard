/* The inference sections: alerts, the top bar, the model card, latency,
   usage, request health and slots. */

import { fmtFull } from '../charts.js';
import { charts, legend, tableBuilders } from '../chart-setup.js';
import { AGENTLESS, beyondRequestLog, currentPoints, latencyRows, probeOn, state } from '../state.js';
import { ICONS, ago, dur, el, esc, fmtSecs, kfmt, nf, quantile, targetName, usageLabel } from '../util.js';

export function renderAlerts(snap) {
  const host = el('alerts');
  const list = snap.alerts || [];
  if (!list.length) {
    // During a restart or switch the banner above says what is happening;
    // "inference healthy" would be false.
    const gpus = AGENTLESS ? '' : `${nf((snap.gpus || []).length)} GPU present, `;
    host.innerHTML = snap.transition && !snap.llama?.healthy
      ? `<div class="allclear">${ICONS.good}
      No alerts — ${gpus}no faults logged; inference is coming back.</div>`
      : `<div class="allclear">${ICONS.good}
      All checks passing — ${gpus}inference healthy, no faults logged.</div>`;
    return;
  }
  const order = { critical: 0, serious: 1, warning: 2 };
  host.innerHTML = [...list].sort((a, b) => (order[a.level] ?? 3) - (order[b.level] ?? 3))
    .map((a) => `<div class="alert ${esc(a.level)}" role="alert">
      <span style="color:var(--${esc(a.level)})">${ICONS[a.level] || ICONS.warning}</span>
      <span class="txt"><span class="t">${esc(a.title)}</span>
      ${a.detail ? `<span class="d"> — ${esc(a.detail)}</span>` : ''}</span>
      <span class="lvl">${esc(a.level)}</span></div>`).join('');
}

export function renderTopbar(snap) {
  const m = snap.model || {}, l = snap.llama || {};
  // Mid-restart or mid-switch the server is neither healthy nor down.
  const switching = !!snap.transition && !snap.transition.stalled && !l.healthy;
  el('model-alias').textContent = m.alias || (switching ? 'loading…' : 'unknown');
  el('model-unit').textContent = m.unit ? `· ${m.unit}` : '';
  el('chip-model').title = m.path ? `model_path: ${m.path}` : 'no model reported';
  el('chip-model').querySelector('.dot').className =
    'dot ' + (l.healthy ? 'good' : switching ? 'warning' : 'critical');

  const worst = snap.worst || 'good';
  const n = (snap.alerts || []).length;
  el('chip-health').querySelector('.dot').className = 'dot ' + (switching && worst === 'good' ? 'warning' : worst);
  el('health-text').textContent = switching ? 'restarting or switching'
    : !l.reachable ? 'server unreachable'
    : worst === 'good' && l.sleeping ? 'asleep · wakes on the next request'
    : worst === 'good' ? `healthy · ${nf(l.latency_ms, 0)} ms`
    : `${n} alert${n === 1 ? '' : 's'}`;

  const ct = snap.llm_ct || {};
  // The unit's own start, not CT uptime: a restarted unit in a long-running
  // container used to read "up 5d" minutes after the restart.
  el('uptime-text').textContent = m.unit && m.unit_since
    ? `${m.unit} up ${dur(Date.now() / 1000 - m.unit_since)}`
    : m.unit ? `${m.unit} running` : `host up ${dur(snap.host_uptime)}`;
  el('chip-uptime').title = ct.vmid && (ct.runtime || 'proxmox-lxc') === 'proxmox-lxc'
    ? `${targetName(ct)} up ${dur(ct.uptime)} · host up ${dur(snap.host_uptime)}` : '';
  el('hostline').textContent = [
    snap.static?.hostname, targetName(ct),
    AGENTLESS ? null : `${(snap.gpus || []).length} GPU`, snap.demo ? 'demo: recorded data' : null,
  ].filter(Boolean).join(' · ');

  document.title = worst === 'good' ? 'InferenceInquire'
    : `${worst === 'critical' ? '🔴' : worst === 'serious' ? '🟠' : '🟡'} ${n} · InferenceInquire`;
}

export function renderModel(snap) {
  const m = snap.model || {}, t = snap.totals || {}, st = snap.stats || {};
  const l = snap.llama || {};
  el('modelinfo').innerHTML = [
    ['Alias', esc(m.alias)],
    ['Unit', m.unit ? `<code>${esc(m.unit)}</code>` : '—'],
    ['File', esc(m.file)],
    ['Path', `<span style="color:var(--text-2);font-size:11px">${esc(m.path)}</span>
       <button class="copybtn" data-copy="${esc(m.path)}">copy</button>`],
    ['llama.cpp build', esc(m.build)],
    ['Context window', `${nf(m.n_ctx)} tokens · ${nf(m.total_slots)} slots`],
    ...(l.sleep_idle_s > 0 ? [['Sleep on idle', `after ${dur(l.sleep_idle_s)} · ${l.sleeping
      ? '<strong>asleep now</strong> — memory freed until the next request' : 'awake'}`
      + '<br><span style="color:var(--muted)">Slots are read only while requests run, '
      + 'so this dashboard never keeps the model awake.</span>'
      + (l.sleep_blocked ? '<br><span style="color:var(--warning)">This llama.cpp build predates '
        + 'b10519: it counts /metrics scrapes as activity, so the model cannot sleep while '
        + 'anything polls it.</span>' : '')]] : []),
    ['Workload decode', st.workload_decode_med != null
      ? `${nf(st.workload_decode_med, 1)} tok/s <span style="color:var(--muted)">24h median of ${nf(st.workload_n)} requests at a median ${nf(st.workload_depth_med)} ctx</span>`
      : '<span style="color:var(--muted)">no workload in 24h</span>'],
    ...(probeOn() ? [['Probe', st.probe_decode_med != null
      ? `${nf(st.probe_decode_med, 1)} tok/s <span style="color:var(--muted)">24h median of ${nf(st.probe_n)} probe requests · the same request each time</span>`
      : '<span style="color:var(--muted)">no probe seen in 24h</span>']] : []),
    ['Alert baseline', m.baseline_tps
      ? `${nf(m.baseline_tps, 1)} tok/s <span style="color:var(--muted)">median of the last ${nf(m.baseline_samples)} observations, all traffic · alerts under ${nf(m.baseline_tps * 0.25, 1)}</span>`
      : `<span style="color:var(--muted)">learning — ${nf(m.baseline_samples)} of 12 observations</span>`],
    ['Counters below', `<span style="color:var(--muted)">llama.cpp's own, since ${esc(m.unit || 'the server')} started${m.unit_since ? ` ${dur(Date.now() / 1000 - m.unit_since)} ago` : ''}</span>`],
    ['Largest seen', `${nf(t.tokens_max)} tokens`],
    ['Generated', `${nf(t.tokens_predicted)} tokens in ${nf(t.decode_calls)} decode calls`],
    ['Prompt processed', `${nf(t.prompt_tokens)} tokens · ${nf(t.prompt_cached)} reused`],
    ['Time generating', `${dur(t.generating_seconds)} · prefilling ${dur(t.prefill_seconds)}`],
    ['Speculative', t.draft_tokens
      ? `${nf(t.accepted_tokens)} of ${nf(t.draft_tokens)} draft tokens accepted over ${nf(t.drafts)} passes`
      : 'not in use'],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('');
}

export function renderLatency() {
  const rows = latencyRows();
  const ttft = rows.map((r) => r.ttft).sort((a, b) => a - b);
  const e2e = rows.map((r) => r.e2e).filter((v) => v != null).sort((a, b) => a - b);
  const cold = rows.filter((r) => r.cold);
  const coldWait = cold.reduce((a, r) => a + r.ttft, 0);
  const deferred = Math.max(0, ...currentPoints().map((p) => p.req_def || 0));
  const cell = (k, v, small) => `<div class="s"><div class="k">${esc(k)}</div>`
    + `<div class="v">${v}${small ? ` <small>${small}</small>` : ''}</div></div>`;
  // The cells stay when the window is empty (dashes, not nothing), so the
  // card keeps its height as the range changes.
  el('latency-stats').innerHTML = [
    cell('First token · p50', fmtSecs(quantile(ttft, 0.5))),
    cell('First token · p95', fmtSecs(quantile(ttft, 0.95)), ttft.length ? `max ${fmtSecs(ttft[ttft.length - 1])}` : ''),
    cell('End to end · p50', fmtSecs(quantile(e2e, 0.5))),
    cell('End to end · p95', fmtSecs(quantile(e2e, 0.95))),
    cell('Cold prefills', rows.length ? nf(cold.length) : '—',
         cold.length ? `${fmtSecs(coldWait)} waiting in total` : rows.length ? 'none' : ''),
    cell('Queued', deferred ? `${nf(deferred)} <small>at most</small>` : '0',
         deferred ? 'requests waited for a slot' : 'no request waited for a slot'),
  ].join('');
  // p50/p95 of first-token time as reference lines, in the chart's log space.
  const c = charts['c-latency'].chart;
  c.opt.thresholds = rows.length ? [
    { value: Math.log10(Math.max(quantile(ttft, 0.95), 0.001)), label: 'p95 first token', color: '--series-2' },
    { value: Math.log10(Math.max(quantile(ttft, 0.5), 0.001)), label: 'p50', color: '--muted' },
  ] : [];
  el('latency-note').textContent = (beyondRequestLog()
    ? `Request-level detail is kept ${state.meta.retain_days || 14} days, so this covers only those. ` : '')
    // The explanation stays when the window is empty, so the card does not
    // change height with the range.
    + (rows.length ? `${nf(rows.length)} workload requests in this range. `
      : 'No workload requests with prefill timings in this range. ')
    + 'First token is llama.cpp\'s prompt-eval time; end to end runs from the slot starting the '
    + 'request to releasing it. Neither includes time queued before a slot was free - llama.cpp '
    + 'does not log a request’s arrival - so "Queued" shows whether any request had to wait at all. '
    + 'Percentiles interpolate between ranks.';
}


export async function loadUsage() {
  try {
    const tz = -new Date().getTimezoneOffset();
    renderUsage(await (await fetch(`/api/usage?range=${state.range}&tz=${tz}`
      + (state.end != null ? `&end=${state.end}` : ''))).json());
  } catch { /* transient: keep the last one */ }
}

function renderUsage(u) {
  state.usage = u;
  const t = u.totals, daily = u.bucket >= 86400;
  const rows = u.buckets.map((b) => ({ ...b, label: usageLabel(b.ts, u.bucket),
                                        tipLabel: usageLabel(b.ts, u.bucket, true) }));
  charts['c-usage-tok'].chart.setData(rows);
  charts['c-usage-wh'].chart.setData(rows.map((r) => ({ label: r.label, tipLabel: r.tipLabel,
                                                        value: r.gpu_wh })));
  legend('lg-usage-tok', probeOn()
    ? [{ label: 'Workload', color: '--series-1' }, { label: 'Probe', color: '--muted' }]
    : [{ label: 'Tokens', color: '--series-1' }]);
  const weekly = u.bucket >= 7 * 86400;
  el('usage-sub').textContent = weekly ? `the last ${rows.length} weeks, per week`
    : daily ? `the last ${rows.length} days, per day` : 'the last 24 hours, per hour';

  const cell = (k, v, small) => `<div class="s"><div class="k">${esc(k)}</div>`
    + `<div class="v">${v}${small ? ` <small>${small}</small>` : ''}</div></div>`;
  const covered = t.covered_s || 0;
  const kwh = t.gpu_wh / 1000;
  const money = (v) => `${esc(t.currency || '')}${v >= 10 ? v.toFixed(0) : v.toFixed(2)}`;
  const cells = [];
  if (covered) {
    cells.push(cell('GPU energy', `${kwh.toFixed(kwh >= 10 ? 1 : 2)} <small>kWh</small>`,
      `${nf(t.gpu_wh / (covered / 3600))} W average`));
    cells.push(cell('Per day', `${(kwh / (covered / 86400)).toFixed(2)} <small>kWh</small>`,
      t.cost_per_day != null ? `${money(t.cost_per_day)} a day` : ''));
    if (t.host_wh != null) {
      cells.push(cell('Whole host', `${(t.host_wh / 1000).toFixed(2)} <small>kWh</small>`, 'from the BMC'));
    }
    cells.push(cell('Idle draw', `${nf(t.idle_w)} <small>W</small>`, 'median, no request running'));
    cells.push(cell('Busy', `${nf(100 * (t.busy_share || 0))}<small>%</small>`, 'of the time'));
  }
  cells.push(cell('Tokens generated', kfmt(t.gen_wl),
    probeOn() && t.gen_probe ? `+ ${kfmt(t.gen_probe)} probe` : ''));
  cells.push(cell('Prompt computed', kfmt(t.prompt_wl), 'tokens, cache misses'));
  cells.push(cell('Requests', nf(t.req_wl), probeOn() && t.req_probe ? `+ ${nf(t.req_probe)} probe` : ''));
  if (t.wh_per_1k_gen != null) {
    cells.push(cell('Per 1k tokens', `${t.wh_per_1k_gen.toFixed(1)} <small>Wh</small>`,
      `${t.wh_per_1k_gen_busy.toFixed(2)} Wh while busy`));
  }
  if (t.cost_per_1m_gen != null) {
    cells.push(cell('Per 1M tokens', money(t.cost_per_1m_gen), 'all-in, idle included'));
  }
  el('usage-stats').innerHTML = cells.join('');
  el('usage-wh-head').textContent = 'GPU energy';
  el('usage-note').textContent = covered
    ? 'Energy integrates the stored GPU power samples; a gap in them counts as missing, not as the '
      + 'last reading held. "Per 1k tokens" divides every watt-hour, idle included, by the tokens '
      + 'generated - what a token really cost; "while busy" counts only windows with a request '
      + 'running.' + (u.source && u.source !== 'raw'
        ? ` At this range the figures come from ${u.source} rollups, so "busy" marks each whole bucket in which a request ran.` : '')
      + (t.price_per_kwh ? '' : ' Set ENERGY_PRICE_PER_KWH to see what it costs.')
    : 'No stored power samples in this range yet.';
  const twin = document.getElementById('c-usage-tok-twin');
  if (twin) twin.innerHTML = tableBuilders['c-usage-tok']();
}

export function renderRequestHealth(snap) {
  const st = snap.stats || {};
  const h1 = st.log_1h || {}, d1 = st.log_24h || {}, w1 = st.log_7d || {};
  // The serving unit's own --cache-ram (or llama.cpp's default for it).
  const lim = snap.cache_ram_mib ?? (snap.journal || {}).cache_limit_mib;
  const cell = (k, v, small, status) => `<div class="s"${status ? ` data-status="${status}"` : ''}>
    <div class="k">${esc(k)}</div><div class="v">${v}${small ? ` <small>${small}</small>` : ''}</div></div>`;
  const n = (v) => (v == null ? '—' : nf(v));
  el('reqhealth').innerHTML = [
    cell('Cold prefills · 24h', n(st.cold_n), st.cold_n ? `${dur(st.cold_seconds)} computing` : ''),
    cell('Cache evictions · 24h', n(d1.cache_evict),
         lim > 0 ? `of a ${nf(lim / 1024, 0)} GiB host-RAM cache` : lim === 0 ? 'cache disabled' : 'host-RAM prompt cache'),
    cell('Too big to cache · 24h', n(d1.cache_skip), lim > 0 ? `limit now ${nf(lim / 1024, 0)} GiB` : '',
         d1.cache_skip ? 'warning' : ''),
    cell('Rejected API key · 1h', n(h1.auth_fail), `${n(d1.auth_fail)} in 24h · ${n(w1.auth_fail)} in 7d`,
         h1.auth_fail ? 'warning' : ''),
    cell('Cancelled · 24h', n(d1.cancels), 'client went away'),
  ].join('');
  const since = d1.counting_since || w1.counting_since;
  el('reqhealth-note').textContent =
    `A cold prefill computed at least ${nf(snap.probe_signature?.cold_prefill_tokens ?? 8192)} prompt tokens `
    + 'from scratch — on a long conversation that is the minute-long wait. Evictions, and states too '
    + 'big for llama.cpp\u2019s host-RAM prompt cache (--cache-ram), are why one happens mid-conversation. '
    + 'llama.cpp does not log which client sent a rejected key.'
    + (since ? ` Log signals counted since ${fmtFull(since)}.` : ' Log signals start counting with this build.');
}

export function renderSlots(snap) {
  const slots = snap.slots || [], l = snap.llama || {};
  const held = l.slots_age != null && l.slots_age > 2 * (snap.poll_interval || 2);
  el('slots-sub').textContent = `${slots.filter((s) => s.processing).length} of ${slots.length} processing`
    + (held ? ` · as of ${dur(l.slots_age)} ago, read only while requests run` : '');
  if (!slots.length) {
    el('slots').innerHTML = `<div class="empty">${l.sleeping
      ? 'The model is asleep — its slots are freed.' : 'No slot data.'}</div>`;
    return;
  }
  el('slots').innerHTML = `<table class="data">
    <thead><tr><th>Slot</th><th>State</th><th class="num">Context</th><th class="opt">Fill</th>
    <th class="num">Cached</th><th class="num">Decoded</th><th class="opt">Spec</th></tr></thead>
    <tbody>${slots.map((s) => {
      const p = s.n_ctx ? (s.prompt_tokens / s.n_ctx) * 100 : 0;
      return `<tr><td>${esc(s.id)}</td>
        <td>${s.processing ? '<span class="badge good">running</span>' : '<span class="badge">idle</span>'}</td>
        <td class="num">${nf(s.prompt_tokens)}</td>
        <td class="opt"><span class="slotbar"><i style="width:${Math.min(100, p).toFixed(2)}%"></i></span></td>
        <td class="num">${nf(s.prompt_cached)}</td>
        <td class="num">${nf(s.decoded)}</td>
        <td class="opt">${s.speculative ? 'yes' : 'no'}</td></tr>`;
    }).join('')}</tbody></table>`;
}
