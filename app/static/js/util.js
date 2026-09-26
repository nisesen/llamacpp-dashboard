/* Small helpers with no page state: DOM lookup, escaping, and formatting. */

export const el = (id) => document.getElementById(id);

/* ------------------------------------------------------------ formatting */

export const nf = (v, d = 0) => (v == null || !isFinite(v) ? '—'
  : v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));

export function bytes(v, digits = 1) {
  if (v == null || !isFinite(v)) return '—';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i >= 2 ? digits : 0)} ${u[i]}`;
}

export const BINARY_INCRS = [];
for (let k = 0; k <= 4; k++) for (let m = 1; m <= 512; m *= 2) BINARY_INCRS.push(m * 1024 ** k);

export function rateBytes(v) {
  if (v == null || !isFinite(v)) return '—';
  const u = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s'];
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function dur(s) {
  if (s == null || !isFinite(s)) return '—';
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

export const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export const gpuColor = (i) => `--series-${(i % 8) + 1}`;   // GPU0 blue, GPU1 orange, locked to the card

/* The agent's target in words: "CT 100", "Docker", "this host". An agent from
   before the runtimes sends no label, and was always Proxmox. */
export const targetName = (ct) => ct.label || (ct.vmid ? `CT ${ct.vmid}` : null);

export const ago = (ts) => (ts ? `${dur(Date.now() / 1000 - ts)} ago` : '');

/* -------------------------------------------------------------- sections */

export function statusOf(pctVal, warn = 80, serious = 92) {
  if (pctVal == null) return '';
  if (pctVal >= serious) return 'critical';
  if (pctVal >= warn) return 'warning';
  return '';
}

export function meter(label, value, max, text, status) {
  const p = max ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  // `status === undefined` means "derive one". An explicit '' means "this bar
  // is magnitude, not state" - `||` would have swallowed that.
  const st = status === undefined ? statusOf(p) : status;
  return `<div class="meter" data-status="${st}">
    <span class="k">${esc(label)}</span><span class="v">${esc(text)}</span>
    <span class="track"><i class="fill" style="width:${p.toFixed(1)}%"></i></span>
  </div>`;
}

export const ICONS = {
  critical: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6.4"/><path d="M8 4.6v4M8 11.1v.1"/></svg>',
  serious: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M8 2.2 14.6 13.4H1.4z"/><path d="M8 6.4v3.1M8 11.6v.1"/></svg>',
  warning: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M8 2.2 14.6 13.4H1.4z"/><path d="M8 6.4v3.1M8 11.6v.1"/></svg>',
  good: '<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.4 6.4 11.8 13 5.2"/></svg>',
};

export function fmtSecs(s, short = false) {
  if (s == null || !isFinite(s)) return '—';
  if (s < 1) return `${Math.round(s * 1000)}${short ? 'ms' : ' ms'}`;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}${short ? 's' : ' s'}`;
  return dur(s);
}

/* The latency axis is log10(seconds). Its ticks sit on durations people
   say - 10 ms, 1 s, 1 min - not on 1/2/5 steps of a logarithm, which would
   label it 316 ms and 3.2 s. Decades first; the in-between ones only when a
   narrow spread would leave fewer than three. */
const LAT_MAJOR = [0.001, 0.01, 0.1, 1, 10, 60, 600, 3600, 36000];
const LAT_MINOR = [0.003, 0.03, 0.3, 3, 30, 180, 1800, 10800];

export function latencySplits(min, max) {
  const inView = (list) => list.map(Math.log10).filter((v) => v >= min - 1e-9 && v <= max + 1e-9);
  const major = inView(LAT_MAJOR);
  return major.length >= 3 ? major : [...major, ...inView(LAT_MINOR)].sort((a, b) => a - b);
}

export function fmtAxisSecs(s) {
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${+s.toPrecision(2)}s`;
  if (s < 3600) return `${+(s / 60).toPrecision(2)}m`;
  return `${+(s / 3600).toPrecision(2)}h`;
}

/* Linear interpolation between closest ranks (numpy's default), so a hand
   calculation over the same rows gives the same figure. */
export function quantile(sorted, q) {
  if (!sorted.length) return null;
  const pos = (sorted.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

export const kfmt = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M`
  : v >= 1e3 ? `${(v / 1e3).toFixed(v >= 1e4 ? 0 : 1)}k` : String(Math.round(v)));

export function usageLabel(ts, bucket, long = false) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  if (bucket >= 86400) {
    return long ? d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
      : `${d.getMonth() + 1}/${d.getDate()}`;
  }
  return long ? `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:00` : `${p(d.getHours())}:00`;
}
