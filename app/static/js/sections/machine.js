/* The machine sections: GPU cards, cores, memory, the BMC, services, platform,
   faults and events, and the flow diagrams. */

import { CSS, fmtFull } from '../charts.js';
import { Architecture, HardwareFlow, RequestFlow, renderKvMap, renderRequestFeed, renderSlotLanes } from '../flow.js';
import { charts } from '../chart-setup.js';
import { isProbe, state } from '../state.js';
import { ago, bytes, dur, el, esc, gpuColor, meter, nf, statusOf, targetName } from '../util.js';

const panels = {};
function ensurePanels() {
  panels.hw = panels.hw || new HardwareFlow(el('hwflow'));
  panels.req = panels.req || new RequestFlow(el('reqflow'));
  panels.arch = panels.arch || new Architecture(el('arch'));
}

export function renderGpuCards(snap) {
  const gpus = snap.gpus || [];
  const host = el('gpucards');
  if (!gpus.length) {
    host.innerHTML = `<section class="card col-12"><div class="empty">
      ${snap.agent_ok ? 'No GPUs reported by nvidia-smi.' : 'Host agent unreachable — no GPU telemetry.'}
    </div></section>`;
    return;
  }
  el('gpu-meta').textContent = `${gpus.length} card${gpus.length === 1 ? '' : 's'} · ${esc(gpus[0].name || '')}`;
  const span = gpus.length >= 3 ? 'col-4' : 'col-6';
  host.innerHTML = gpus.map((g) => {
    const lim = g.limits || {};
    const pwrPct = g.pwr_limit ? (g.pwr / g.pwr_limit) * 100 : 0;
    const remaps = (g.remap_correctable || 0) + (g.remap_uncorrectable || 0);
    const hot = lim.temp_mem_warn && g.hbm >= lim.temp_mem_warn;
    return `<section class="card ${span}">
      <div class="gpuhead">
        <span class="tag" style="background:${CSS(gpuColor(g.idx))}"></span>
        <span class="name">GPU${g.idx}</span>
        <span class="badge mono">${esc(g.bus)}</span>
        <span class="meta">SN ${esc(g.serial)}</span>
      </div>
      <div class="statrow">
        <div class="s"><div class="k">Core</div><div class="v">${nf(g.temp)}<small> °C</small></div></div>
        <div class="s"><div class="k">Memory</div><div class="v" ${hot ? 'style="color:var(--warning)"' : ''}>${nf(g.hbm)}<small> °C</small></div></div>
        <div class="s"><div class="k">SM clock</div><div class="v">${nf(g.sm_clk)}<small> MHz</small></div></div>
        <div class="s"><div class="k">P-state</div><div class="v">${esc(g.pstate)}</div></div>
      </div>
      ${meter('Utilisation', g.util, 100, `${nf(g.util)} %`, '')}
      ${meter('VRAM', g.mem_used, g.mem_total, `${(g.mem_used / 1024).toFixed(1)} / ${(g.mem_total / 1024).toFixed(0)} GiB`, '')}
      ${meter('Power', g.pwr, g.pwr_limit, `${nf(g.pwr)} / ${nf(g.pwr_limit)} W`, statusOf(pwrPct, 115, 135))}
      <div class="statrow" style="margin-top:10px">
        <div class="s"><div class="k">Link</div><div class="v">Gen${esc(g.pcie_gen)}<small> ×${esc(g.pcie_width)}</small></div></div>
        <div class="s"><div class="k">Remapped</div><div class="v">${nf(remaps)}<small> rows</small></div></div>
        <div class="s"><div class="k">ECC unc.</div><div class="v">${nf(g.ecc_uncorrected)}</div></div>
        <div class="s"><div class="k">Throttle</div><div class="v" style="font-size:12px">${
          (g.throttle || []).length
            ? `<span class="badge ${g.throttle.some((r) => r.includes('thermal') || r.includes('brake')) ? 'serious' : 'warning'}">${esc(g.throttle.join(', '))}</span>`
            : '<span class="badge good">none</span>'}</div></div>
      </div>
      <div class="note">${esc(g.name)} · ${lim.power_limit ? `capped ${nf(lim.power_limit)} W of ${nf(lim.power_max)} W` : ''}
        · shutdown ${nf(lim.temp_shutdown)} °C · ${g.remap_pending === 'Yes' ? 'remap pending' : 'no pending remap'}</div>
    </section>`;
  }).join('');
}

export function renderCores(snap) {
  const c = snap.cpu || {};
  const per = c.per_core || [];
  const host = el('cores');
  if (host.children.length !== per.length) {
    host.innerHTML = per.map((_, i) => `<div class="core" data-core="${i}"></div>`).join('');
    host.style.gridTemplateColumns = `repeat(${Math.min(12, Math.ceil(Math.sqrt(per.length * 2)))}, 1fr)`;
  }
  per.forEach((v, i) => {
    const cell = host.children[i];
    if (!cell) return;
    cell.style.background = `var(--seq-${v == null ? 0 : Math.min(11, Math.floor((v / 100) * 11.999))})`;
    cell.setAttribute('aria-label', `cpu${i}: ${v == null ? 'unknown' : v.toFixed(0)}%`);
    cell.dataset.tip = `cpu${i} — ${v == null ? '—' : v.toFixed(0)}% busy`;
  });
  el('cores-sub').textContent = `${nf(c.cores)} threads · ${nf(c.pct, 0)}% busy`;
  el('cpu-sub').textContent = c.cores ? `aggregate across ${nf(c.cores)} threads` : '';
  el('host-meta').textContent = [c.model, c.load ? `load ${c.load.map((x) => x.toFixed(2)).join(' ')}` : null]
    .filter(Boolean).join(' · ');
  if (!el('ramp').children.length) {
    el('ramp').innerHTML = [...Array(12).keys()].map((i) => `<i style="background:var(--seq-${i})"></i>`).join('');
  }
}

export function renderMemory(snap) {
  const m = snap.mem || {}, ct = snap.llm_ct || {};
  const label = targetName(ct) || 'Guest';
  el('memory').innerHTML =
    meter('Host RAM', m.used, m.total, `${bytes(m.used)} / ${bytes(m.total)}`) +
    meter('Page cache', m.cached, m.total, bytes(m.cached), '') +
    (m.swap_total ? meter('Swap', m.swap_used, m.swap_total, `${bytes(m.swap_used)} / ${bytes(m.swap_total)}`) : '') +
    (ct.mem_total ? meter(`${label} RAM`, ct.mem_used, ct.mem_total, `${bytes(ct.mem_used)} / ${bytes(ct.mem_total)}`) : '') +
    (ct.disk_total ? meter(`${label} disk`, ct.disk_used, ct.disk_total, `${bytes(ct.disk_used)} / ${bytes(ct.disk_total)}`) : '') +
    (snap.disks || []).map((d) => meter(d.name, d.used, d.total, `${bytes(d.used)} / ${bytes(d.total)}`)).join('');
}

export function renderIpmi(snap) {
  const s = snap.ipmi_sensors || [];
  el('ipmi-sub').textContent = snap.ipmi_available
    ? `${s.length} sensors · ${nf(snap.ages?.ipmi, 0)} s ago` : 'unavailable';
  if (!s.length) { el('ipmi').innerHTML = '<div class="empty">No IPMI data.</div>'; return; }
  const groups = { temp: 'Temperature', volt: 'Voltage', fan: 'Fan', watt: 'Power', amp: 'Current', other: 'Other' };
  el('ipmi').innerHTML = `<table class="data">
    <thead><tr><th>Sensor</th><th class="opt">Group</th><th class="num">Reading</th><th class="num">Crit high</th><th>State</th></tr></thead>
    <tbody>${s.map((x) => {
      const ok = ['ok', 'na', 'nr', ''].includes(x.state);
      return `<tr><td>${esc(x.name)}</td><td class="opt" style="color:var(--muted)">${esc(groups[x.kind] || x.kind)}</td>
        <td class="num">${nf(x.value, x.kind === 'volt' ? 3 : 0)} ${esc(x.unit.replace('degrees C', '°C'))}</td>
        <td class="num" style="color:var(--muted)">${x.thresholds?.ucr != null ? nf(x.thresholds.ucr, x.kind === 'volt' ? 2 : 0) : '—'}</td>
        <td>${ok ? '<span class="badge good">ok</span>' : `<span class="badge critical">${esc(x.state)}</span>`}</td></tr>`;
    }).join('')}</tbody></table>`;

  // The rail chart names itself after whichever sensor the agent matched.
  const rail = snap.rail;
  if (rail) {
    el('rail-title').textContent = `${rail.name} rail`;
    const th = rail.thresholds || {};
    el('rail-sub').textContent = th.lcr && th.ucr
      ? `critical band ${th.lcr}–${th.ucr} V` : 'board voltage';
    const c = charts['c-rail'];
    if (c && !c.railBound && (th.lcr || th.ucr)) {
      c.opt.thresholds = [
        th.ucr ? { value: th.ucr, label: 'crit high', color: '--muted' } : null,
        th.lcr ? { value: th.lcr, label: 'crit low', color: '--muted' } : null,
      ].filter(Boolean);
      c.chart.opt.thresholds = c.opt.thresholds;
      c.railBound = true;
    }
  }
}

export function renderServices(snap) {
  const units = snap.units || {};
  const ct = snap.llm_ct || {};
  const names = Object.keys(units).sort();
  const where = ct.runtime === 'systemd' ? 'on this host' : `inside ${targetName(ct)}`;
  el('services-sub').textContent = ct.vmid
    ? `${where}${ct.discovered ? ' (discovered)' : ''}` : 'nothing found';
  if (!names.length) { el('services').innerHTML = '<div class="empty">No llama units found.</div>'; return; }
  el('services').innerHTML = `<table class="data">
    <thead><tr><th>Unit</th><th>State</th><th class="num">Restarts</th><th class="num">RSS</th></tr></thead>
    <tbody>${names.map((n) => {
      const u = units[n];
      const active = u.active === 'active';
      return `<tr><td>${esc(n)}${u.alias ? `<div style="color:var(--muted);font-size:10.5px">${esc(u.alias)}</div>` : ''}</td>
        <td><span class="badge ${active ? 'good' : ''}">${esc(u.active || '?')}</span></td>
        <td class="num">${nf(u.restarts)}</td>
        <td class="num">${u.memory && u.memory > 0 ? bytes(u.memory) : '—'}</td></tr>`;
    }).join('')}</tbody></table>
    <div class="note">${ct.runtime === 'docker'
      ? 'Containers are found by name, image or command, so a new model container appears here on its own.'
      : `Units are discovered by pattern, so a new model unit appears here on its own.
      Only one may run at a time — they carry <code>Conflicts=</code> because they would otherwise
      fight over VRAM.`}</div>
    <dl class="kvlist" style="margin-top:10px">
      <dt>Endpoint</dt><dd>${ct.endpoint ? `<code>${esc(ct.endpoint)}</code>` : '—'}</dd>
      <dt>${ct.uptime != null || !ct.runtime ? 'Container' : 'Runtime'}</dt><dd>${!ct.reachable
        ? '<span class="badge critical">unreachable</span>'
        : ct.uptime != null || !ct.runtime ? `up ${dur(ct.uptime)} · load ${(ct.load || []).map((x) => x.toFixed(2)).join(' ')}`
        : esc(ct.runtime)}</dd>
      <dt>API latency</dt><dd>${nf(snap.llama?.latency_ms, 1)} ms</dd>
    </dl>`;
}

export function renderPlatform(snap) {
  const s = snap.static || {};
  el('platform').innerHTML = [
    ['Host', esc(s.hostname)],
    ...(s.pve_version ? [['Proxmox', esc(s.pve_version)]] : []),
    ['Kernel', esc(s.kernel)],
    ['NVIDIA driver', esc(s.driver_version)],
    ['CPU', esc(s.cpu_model)],
    ['Host uptime', dur(snap.host_uptime)],
    ...((snap.guests || []).length ? [['Guests', `<span class="guests">${(snap.guests || []).map((g) =>
      `<span class="guest">${g.type === 'vm' ? 'VM' : 'CT'} ${esc(g.vmid)} ${esc(g.name)} <span class="badge ${g.status === 'running' ? 'good' : ''}">${esc(g.status)}</span></span>`).join('')}</span>`]] : []),
    ['Network', `${esc(snap.net?.primary)} <span style="color:var(--muted)">of ${esc((snap.net?.ifaces || []).join(', '))}</span>`],
    ['Agent', `v${esc(s.agent_version)} · polling every ${snap.poll_interval}s`],
    ['Dashboard', `${state.meta.version ? `v${esc(state.meta.version)} · ` : ''}up ${dur(snap.dashboard_uptime)}`],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('');
}

export function renderFaults(snap) {
  const xid = snap.xid || {}, sel = snap.sel || {};
  el('faults').innerHTML = `
    <div class="statrow">
      <div class="s"><div class="k">Xid faults</div><div class="v" style="color:${xid.count ? 'var(--critical)' : 'inherit'}">${nf(xid.count)}</div></div>
      <div class="s"><div class="k">BMC events</div><div class="v">${nf(sel.real_count ?? sel.count)}</div></div>
    </div>
    ${xid.count ? `<div class="note" style="color:var(--critical)">${esc((xid.recent || []).slice(-2).join(' / '))}</div>` : ''}
    ${(sel.entries || []).length ? `<div class="tablewrap" style="margin-top:10px"><table class="data">
      <thead><tr><th>When</th><th>Event</th></tr></thead><tbody>${
        sel.entries.slice().reverse().map((e) => `<tr><td style="white-space:nowrap">${esc(e.when)}</td><td>${esc(e.what)}</td></tr>`).join('')
      }</tbody></table></div>` : '<div class="note">BMC event log is empty.</div>'}
    ${sel.noise_count ? `<div class="note">${nf(sel.noise_count)} sensor-less "Unknown #0xff" records hidden —
      usually left by a BMC reset, often with a clock-less date.</div>` : ''}`;
}

function renderRecentEvents(rows) {
  const list = rows.slice(0, 7);
  el('recent-events').innerHTML = list.length ? `<div class="recent-list">${list.map((e) => `
    <div class="recent-row">
      <span class="when">${esc(dur(Date.now() / 1000 - e.ts))}</span>
      <span class="what">${esc(e.title)}${e.detail ? ` <small>${esc(e.detail)}</small>` : ''}</span>
      ${e.level === 'info'
        ? `<span class="badge">${e.state === 'raised' ? 'started' : 'done'}</span>`
        : e.state === 'raised' ? `<span class="badge ${esc(e.level)}">${esc(e.level)}</span>`
        : '<span class="badge good">cleared</span>'}
    </div>`).join('')}</div>`
    : '<div class="empty">Nothing logged yet: no alert, restart or model change.</div>';
}

export async function renderEvents() {
  try {
    const rows = await (await fetch('/api/events?limit=60')).json();
    renderRecentEvents(rows);
    el('events').innerHTML = rows.length ? `<table class="data">
      <thead><tr><th>When</th><th>State</th><th class="opt">Level</th><th>What</th></tr></thead>
      <tbody>${rows.map((e) => `<tr>
        <td style="white-space:nowrap">${fmtFull(e.ts)}</td>
        <td>${e.level === 'info'
          ? `<span class="badge">${e.state === 'raised' ? 'started' : 'done'}</span>`
          : e.state === 'raised' ? '<span class="badge warning">raised</span>' : '<span class="badge good">cleared</span>'}</td>
        <td class="opt"><span class="badge ${esc(e.level)}">${esc(e.level)}</span></td>
        <td>${esc(e.title)}${e.detail ? ` <span style="color:var(--muted)">— ${esc(e.detail)}</span>` : ''}</td>
      </tr>`).join('')}</tbody></table>`
      : '<div class="empty">Nothing logged yet — no alert has fired since this dashboard started.</div>';
  } catch { /* transient */ }
}

export function renderFlow(snap) {
  ensurePanels();
  panels.hw.update(snap);
  panels.req.update(snap, isProbe);
  panels.arch.update(snap);
  renderSlotLanes(el('lanes'), snap);
  renderRequestFeed(el('reqfeed'), snap, isProbe, state.requests || []);
  renderKvMap(el('kvmap'), snap);
  el('kv-sub').textContent = snap.model?.n_ctx
    ? `${nf(snap.model.n_ctx)} tokens per slot` : 'what each slot is holding right now';

  const j = snap.journal || {};
  el('lifecycle-hint').textContent = j.alive
    ? `following ${j.following || '?'}` : 'journal not attached';
  const gpus = snap.gpus || [];
  const pcie = gpus.reduce((a, g) => a + (g.pcie_rx || 0) + (g.pcie_tx || 0), 0);
  el('pcie-hint').textContent = snap.pcie_available ? `${nf(pcie)} MB/s total` : '';
  const a = snap.arch || {};
  el('arch-sub').textContent = a.available
    ? `${a.architecture} · read from ${a.file}` : 'reading the GGUF header…';
  el('flow-meta').textContent = !a.available ? ''
    : a.expert_count ? `${nf(a.n_layer)} blocks · ${nf(a.expert_used)} of ${nf(a.expert_count)} experts per token`
    : `${nf(a.n_layer)} blocks · dense`;

  // A model load is worth interrupting the page for: it is the one moment
  // the whole machine is visibly doing something different.
  const load = j.load || {};
  const banner = el('loadbanner');
  if (load.state === 'loading') {
    const secs = load.started ? Math.round(Date.now() / 1000 - load.started) : null;
    banner.innerHTML = `<div class="loadbanner"><span class="spin"></span>
      <span><strong>Loading model</strong> — ${esc((load.path || '').split('/').pop())}
      ${secs != null ? `· ${secs}s elapsed` : ''} · weights are streaming from disk into VRAM.</span></div>`;
  } else if (snap.transition) {
    const tr = snap.transition;
    banner.innerHTML = `<div class="loadbanner${tr.stalled ? ' stalled' : ''}"><span class="spin"></span>
      <span><strong>${tr.stalled ? 'Still not back' : 'Restarting or switching'}</strong> — ${esc(tr.reason)}
      · ${Math.round(tr.elapsed)}s${tr.stalled ? ' · alerting normally now'
        : ' · reachability alerts are held until it finishes'}</span></div>`;
  } else if ((load.warnings || []).length) {
    banner.innerHTML = `<div class="loadbanner"><span style="color:var(--warning)">⚠</span>
      <span><strong>Startup warning</strong> — ${esc(load.warnings[load.warnings.length - 1])}</span></div>`;
  } else {
    banner.innerHTML = '';
  }
}
