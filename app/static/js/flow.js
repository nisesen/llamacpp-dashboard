/* ==========================================================================
   Flow and anatomy panels.

   Three diagrams, built once and then updated in place so the animations do
   not restart on every poll.

   A rule runs through this file: **everything drawn is either measured or
   marked as not measured.** Byte rates on the hardware edges are real
   (nvidia-smi dmon, /proc/diskstats). Slot phases, per-request timings and
   the prompt-cache similarity are real (llama.cpp's own journal). The model's
   shape is real (its GGUF header). What is NOT observable - which layer is
   executing, which experts fired - is drawn as static structure and labelled
   as such, never animated to imply telemetry we do not have.
   ========================================================================== */

import { CSS } from './charts.js';

const SVG = 'http://www.w3.org/2000/svg';
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const nf = (v, d = 0) => (v == null || !isFinite(v) ? '—'
  : v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));

function rateBytes(v) {
  if (v == null || !isFinite(v)) return '—';
  const u = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s'];
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

/** Attach a hover explainer. The dashboard should teach, not just report. */
function explain(el, title, body) {
  el.dataset.explainTitle = title;
  el.dataset.explainBody = body;
  el.classList.add('explainable');
  return el;
}

function h(tag, attrs = {}, kids = []) {
  const e = document.createElementNS(SVG, tag);
  for (const k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
  for (const k of [].concat(kids)) e.appendChild(k);
  return e;
}

function text(x, y, str, cls, anchor = 'start') {
  const t = h('text', { x, y, 'text-anchor': anchor, class: cls || '' });
  t.textContent = str;
  return t;
}

/* =========================================================================
   1. Hardware dataflow - where the bytes actually go
   ========================================================================= */

export class HardwareFlow {
  constructor(host) {
    this.host = host;
    this.sig = null;
  }

  update(snap) {
    const gpus = snap.gpus || [];
    const sig = gpus.map((g) => g.idx).join(',');
    if (sig !== this.sig) { this.build(gpus); this.sig = sig; }
    this.paint(snap);
  }

  build(gpus) {
    this.host.innerHTML = '';
    const n = Math.max(1, gpus.length);
    // Geometry note: every coordinate below must stay inside the viewBox.
    // The SVG is clipped, so anything that strays is cut off rather than
    // escaping the card - but it should not stray in the first place.
    const W = 820, rowH = 116, GPU_X = 560, GPU_W = 200, HOST_R = 380;
    const BOX_H = 78;
    const H = 36 + n * rowH;
    const svg = h('svg', { viewBox: `0 0 ${W} ${H}`, class: 'flowsvg',
                           preserveAspectRatio: 'xMidYMid meet' });
    this.svg = svg;
    this.nodes = {};
    this.host.appendChild(svg);

    const rowY = (i) => 20 + i * rowH;          // top of row i
    const midY = (i) => rowY(i) + BOX_H / 2;    // vertical centre of row i

    const box = (id, x, y, w, hh, label, sub, tipT, tipB) => {
      const g = h('g', { class: 'fnode', transform: `translate(${x},${y})` });
      g.appendChild(h('rect', { x: 0, y: 0, width: w, height: hh, rx: 10, class: 'fbox' }));
      g.appendChild(text(13, 22, label, 'flabel'));
      const s1 = text(13, 44, '', 'fvalue');
      // y=60 leaves ~6px between this text's descenders and the bar at y=68.
      const s2 = text(13, 60, sub || '', 'fsub');
      g.appendChild(s1); g.appendChild(s2);
      svg.appendChild(g);
      this.nodes[id] = { g, v: s1, sub: s2 };
      if (tipT) explain(g, tipT, tipB);
      return this.nodes[id];
    };

    const hostY = midY(0);
    box('disk', 8, rowY(0), 150, BOX_H, 'NVMe', '',
        'Model storage',
        'Weights stream from here into page cache at load. During steady serving this should '
        + 'be near zero \u2014 a sustained read usually means a model is loading or something is '
        + 'paging. Measured from /proc/diskstats.');
    box('host', 210, rowY(0), 170, BOX_H, 'Host', '',
        'Host memory',
        'The GGUF is memory-mapped, so the host page cache holds whatever the GPUs have not '
        + 'taken \u2014 including any layers, experts or tables deliberately left on the CPU '
        + 'side. llama.cpp\u2019s host-RAM prompt cache lives here too.');

    // disk -> host, no label: the NVMe node already shows the rate.
    const dEdge = h('g', { class: 'fedge' });
    const dd = `M 158 ${hostY} L 210 ${hostY}`;
    dEdge.appendChild(h('path', { d: dd, class: 'fline' }));
    const dflow = h('path', { d: dd, class: 'fflow' });
    dEdge.appendChild(dflow);
    svg.appendChild(dEdge);
    this.nodes['e-disk'] = { flow: dflow, lbl: null };

    gpus.forEach((g, i) => {
      const y = rowY(i), my = midY(i);
      const node = box(`gpu${g.idx}`, GPU_X, y, GPU_W, BOX_H, `GPU${g.idx}`, '',
        `GPU${g.idx} \u00b7 ${g.name || ''}`,
        'Board power, VRAM and PCIe throughput are read per card from nvidia-smi. The PCIe '
        + 'figure is that card\u2019s total over the link \u2014 host transfers and peer-to-peer '
        + 'traffic share it, so it cannot be split by origin.');
      node.g.querySelector('.fbox').setAttribute('style',
        `stroke:${CSS(`--series-${(g.idx % 8) + 1}`)}`);
      node.g.appendChild(h('rect', { x: 13, y: 68, width: GPU_W - 26, height: 5, rx: 3, class: 'fbar-bg' }));
      const fill = h('rect', { x: 13, y: 68, width: 0, height: 5, rx: 3, class: 'fbar' });
      fill.setAttribute('style', `fill:${CSS(`--series-${(g.idx % 8) + 1}`)}`);
      node.g.appendChild(fill);
      node.bar = fill;

      // Each edge carries its own label on its OWN row. Placing them all at
      // the midpoint of the first row is what stacked them on top of
      // each other.
      const eg = h('g', { class: 'fedge' });
      const d = `M ${HOST_R} ${hostY} C ${(HOST_R + GPU_X) / 2} ${hostY}, `
              + `${(HOST_R + GPU_X) / 2} ${my}, ${GPU_X} ${my}`;
      eg.appendChild(h('path', { d, class: 'fline' }));
      const flow = h('path', { d, class: 'fflow' });
      eg.appendChild(flow);
      const lbl = text((HOST_R + GPU_X) / 2, my - 10, '', 'fedgelabel', 'middle');
      eg.appendChild(lbl);
      svg.appendChild(eg);
      this.nodes[`e-gpu${g.idx}`] = { flow, lbl };
    });

    if (gpus.length > 1) {
      const y0 = midY(0), y1 = midY(gpus.length - 1);
      const right = GPU_X + GPU_W;              // 760
      const g = h('g', { class: 'fedge peer' });
      // Bulge to 800, comfortably inside the 820 viewBox.
      g.appendChild(h('path', {
        d: `M ${right} ${y0} C 800 ${y0}, 800 ${y1}, ${right} ${y1}`,
        class: 'fline dashed',
      }));
      // Label goes in the gap BETWEEN the cards, not over them.
      const lbl = text(GPU_X + GPU_W / 2, (y0 + y1) / 2 + 4, 'card to card',
                       'fedgelabel', 'middle');
      g.appendChild(lbl);
      svg.appendChild(g);
      explain(g, 'Cross-card traffic',
        'With llama.cpp\u2019s default layer split, activations hand off from one card to the '
        + 'next once per token. Without an NVLink bridge that traffic crosses PCIe (through '
        + 'the root complex, if the cards sit on different CPU dies); it is included in each '
        + 'card\u2019s PCIe counters but cannot be isolated from host transfers.');
    }
  }

  paint(snap) {
    const d = snap.disk || {};
    const mem = snap.mem || {};
    const set = (id, v, sub) => {
      const n = this.nodes[id];
      if (!n) return;
      n.v.textContent = v;
      if (sub != null) n.sub.textContent = sub;
    };
    set('disk', rateBytes(d.read_bps), `read · ${(snap.disk?.devices || []).join(', ')}`);
    set('host', `${(mem.cached / 1073741824 || 0).toFixed(1)} GiB cached`,
        `${(mem.used / 1073741824 || 0).toFixed(0)} of ${(mem.total / 1073741824 || 0).toFixed(0)} GiB used`);

    const flowSpeed = (el, v, max) => {
      if (!el) return;
      const f = Math.max(0, Math.min(1, (v || 0) / max));
      el.style.opacity = f > 0.002 ? String(0.25 + 0.75 * f) : '0';
      el.style.animationDuration = f > 0.002 ? `${(2.4 - 2.0 * f).toFixed(2)}s` : '0s';
    };

    flowSpeed(this.nodes['e-disk']?.flow, d.read_bps, 800 * 1024 * 1024);

    for (const g of snap.gpus || []) {
      const n = this.nodes[`gpu${g.idx}`];
      if (!n) continue;
      n.v.textContent = `${nf(g.pwr)} W · ${nf(g.util)}%`;
      n.sub.textContent = `${((g.mem_used || 0) / 1024).toFixed(1)} / ${((g.mem_total || 0) / 1024).toFixed(0)} GiB`;
      if (n.bar) n.bar.setAttribute('width', String(168 * Math.min(1, (g.mem_pct || 0) / 100)));
      const e = this.nodes[`e-gpu${g.idx}`];
      if (e) {
        const total = (g.pcie_rx || 0) + (g.pcie_tx || 0);
        e.lbl.textContent = total ? `${nf(g.pcie_rx)}↓ ${nf(g.pcie_tx)}↑ MB/s` : 'idle';
        flowSpeed(e.flow, total, 4000);
      }
    }
  }
}

/* =========================================================================
   2. Request lifecycle - stages, live from llama.cpp's own journal
   ========================================================================= */

const STAGES = [
  { key: 'queued', label: 'Arrive', tip: ['A request arrives',
    'The server assigns it to one of its parallel slots. Nothing is queued unless every slot is busy — "deferred" on the concurrency chart counts those.'] },
  { key: 'pick', label: 'Pick slot', tip: ['Slot selection',
    'llama.cpp prefers a slot whose existing context shares a prefix with the new prompt, scored by longest-common-prefix similarity. A high score means most of the prompt is already in that slot’s KV cache and never has to be recomputed — for a long conversation, the difference between a sub-second and a minute-long first token. Failing that it picks least-recently-used.'] },
  { key: 'prefill', label: 'Prefill', tip: ['Prefill',
    'The prompt is processed in parallel, filling the KV cache. It is usually the most power-hungry phase: short bursts above a card’s power cap are normal here. Only the tokens not already cached are computed.'] },
  { key: 'decode', label: 'Decode', tip: ['Decode',
    'Tokens are generated one at a time, each attending over the whole KV cache. This phase is memory- and latency-bound rather than compute-bound, which is why it runs at tens of tokens per second while prefill runs at hundreds.'] },
  { key: 'spec', label: 'Draft ⟳ verify', tip: ['Speculative decoding',
    'A drafter — a small draft model, an MTP head or n-gram lookup — proposes several tokens ahead; the full model verifies them in one pass. Accepted tokens are nearly free — that is the whole trick. The acceptance rate and mean accepted length here are llama.cpp’s own per-request figures.'] },
  { key: 'release', label: 'Release', tip: ['Release',
    'The slot is freed but its context is kept — in the slot, or in llama.cpp’s host-RAM prompt cache on builds that park idle slots there — so the next request with the same prefix can reuse it instead of recomputing it.'] },
];

export class RequestFlow {
  constructor(host) {
    this.host = host;
    this.built = false;
  }

  update(snap, isProbe) {
    if (!this.built) { this.build(); this.built = true; }
    this.paint(snap, isProbe || (() => false));
  }

  build() {
    this.host.innerHTML = '';
    const W = 820, H = 92;
    const svg = h('svg', { viewBox: `0 0 ${W} ${H}`, class: 'flowsvg',
                           preserveAspectRatio: 'xMidYMid meet' });
    this.host.appendChild(svg);
    this.stages = {};
    const gap = 10;
    const w = (W - gap * (STAGES.length - 1)) / STAGES.length;

    STAGES.forEach((st, i) => {
      const x = i * (w + gap);
      const g = h('g', { class: 'stage', transform: `translate(${x},18)` });
      g.appendChild(h('rect', { x: 0, y: 0, width: w, height: 52, rx: 9, class: 'sbox' }));
      g.appendChild(text(w / 2, 22, st.label, 'slabel', 'middle'));
      const v = text(w / 2, 40, '', 'svalue', 'middle');
      g.appendChild(v);
      svg.appendChild(g);
      explain(g, st.tip[0], st.tip[1]);
      this.stages[st.key] = { g, v };
      if (i < STAGES.length - 1) {
        svg.appendChild(h('path', {
          d: `M ${x + w + 1} 44 L ${x + w + gap - 1} 44`, class: 'fline',
        }));
      }
    });
  }

  paint(snap, isProbe) {
    const j = snap.journal || {};
    const slots = j.slots || [];
    const totals = snap.totals || {};
    const counts = { prefill: 0, decode: 0 };
    let bestPick = null;
    for (const s of slots) {
      if (s.stale) continue;
      if (s.phase === 'prefill') counts.prefill++;
      if (s.phase === 'decode') counts.decode++;
      if (s.pick && (!bestPick || (s.pick.similarity || 0) > (bestPick.similarity || 0))) bestPick = s.pick;
    }
    const active = counts.prefill + counts.decode;
    // Idle, the stages describe the last real request - not a probe request,
    // whose few-token "prefill rate" is overhead.
    const recent = j.requests || [];
    const req = recent.find((r) => !isProbe(r)) || recent[0];

    const set = (key, txt, on) => {
      const st = this.stages[key];
      if (!st) return;
      st.v.textContent = txt;
      st.g.classList.toggle('on', !!on);
    };

    set('queued', `${nf(snap.slots ? snap.slots.length : 0)} slots`, active > 0);
    set('pick', bestPick
      ? (bestPick.how === 'cache' ? `cache ${(bestPick.similarity * 100).toFixed(0)}%` : 'LRU')
      : '—', !!bestPick);
    set('prefill', counts.prefill ? `${counts.prefill} active`
      : !req ? '—' : (req.prompt_tokens || 0) >= 256 ? `${nf(req.prefill_tps)} tok/s` : 'cached',
        counts.prefill > 0);
    const live = slots.filter((s) => !s.stale && s.phase === 'decode');
    const liveTg = Math.max(0, ...live.map((s) => s.tg_3s || 0));
    set('decode', live.length ? `${liveTg.toFixed(1)} tok/s` : (req ? `${nf(req.decode_tps, 1)} tok/s` : '—'),
        counts.decode > 0);
    set('spec', totals.accept_rate_avg != null
      ? `${(100 * totals.accept_rate_avg).toFixed(0)}% · ${nf(req?.mean_len, 2)} len` : 'off',
      counts.decode > 0);
    set('release', req ? `${nf(req.gen_tokens)} tok` : '—', false);
  }
}

/* =========================================================================
   3. Model anatomy - the real shape of what is loaded
   ========================================================================= */

export class Architecture {
  constructor(host) {
    this.host = host;
    this.sig = null;
  }

  update(snap) {
    const a = snap.arch || {};
    const sig = `${a.architecture}:${a.n_layer}:${a.expert_count}`;
    if (sig !== this.sig) { this.build(a, snap); this.sig = sig; }
    this.paint(snap);
  }

  build(a, snap) {
    void snap;
    this.host.innerHTML = '';
    if (!a.available) {
      this.host.innerHTML = '<div class="empty">Model architecture not read yet — '
        + 'the agent reads it from the GGUF header shortly after start.</div>';
      return;
    }

    const nLayer = a.n_layer || 0;
    const full = new Set(a.full_attention_layers || []);
    // Three shapes this panel has to cope with, because the box serves
    // whatever is loaded: sparse mixture-of-experts or dense; a hybrid
    // attention stack or a uniform one; grouped-query attention or plain
    // multi-head. Assuming any of them produces confident nonsense.
    const isMoE = !!(a.expert_count && a.expert_used);
    const isHybrid = full.size > 0 && full.size < nLayer;
    const gqa = a.gqa_reported && a.n_head_kv && a.n_head_kv !== a.n_head;

    const facts = [
      ['Architecture', a.architecture, 'Architecture id',
       'Read from the GGUF header, so it is whatever is actually loaded — not a guess from the filename.'],
      ['Blocks', nf(nLayer), 'Transformer blocks',
       `${nLayer} stacked blocks. Every token passes through all of them, once per generated token.`],
      ['Hidden size', nf(a.n_embd), 'Embedding width',
       `Each token is a vector of ${nf(a.n_embd)} numbers as it moves down the stack.`],
      ['Attention heads', gqa ? `${a.n_head} / ${a.n_head_kv} KV` : `${a.n_head}`,
       gqa ? 'Grouped-query attention' : 'Multi-head attention',
       gqa
         ? `${a.n_head} query heads share just ${a.n_head_kv} key/value heads — a `
           + `${Math.round(a.n_head / a.n_head_kv)}:1 ratio. Fewer KV heads means a much smaller KV `
           + 'cache, which is what makes a very long context affordable.'
         : 'Every query head carries its own key/value head; this model reports no grouped-query '
           + 'sharing, so its KV cache grows with the full head count.'],
      ['Head dim', `${nf(a.key_length)} K / ${nf(a.value_length)} V`, 'Per-head width',
       'The width of each key and value vector stored per token, per KV head.'],
      ['Feed-forward', isMoE ? `${nf(a.expert_used)} of ${nf(a.expert_count)}` : 'Dense',
       isMoE ? 'Mixture of experts' : 'Dense feed-forward',
       isMoE
         ? `A sparse model: each block holds ${nf(a.expert_count)} feed-forward experts but routes `
           + `every token to only ${nf(a.expert_used)} of them `
           + `(${((a.expert_used / a.expert_count) * 100).toFixed(1)}%). All of them must be loaded; `
           + 'only a few do work per token. That is why such a model occupies a lot of memory while '
           + 'decoding no faster than a much smaller dense one. Which experts fire is not exposed by '
           + 'llama.cpp, so it is not shown.'
         : 'This model reports no expert count, so every block runs one dense feed-forward network '
           + 'and all of its parameters are used for every token. Nothing to route, nothing to show.'],
      ['Context', nf(a.context_length), 'Trained context length',
       snap?.model?.n_ctx
         ? `The context the model was trained for. Each slot on this server gets ${nf(snap.model.n_ctx)}.`
         : 'The context the model was trained for.'],
    ];
    if (isMoE) {
      facts.push(['Expert FFN', nf(a.expert_ffn), 'Expert width',
        `Each routed expert is a ${nf(a.expert_ffn)}-wide feed-forward network`
        + (a.shared_ffn ? `, plus a shared one of ${nf(a.shared_ffn)}.` : '.')]);
    }
    if (a.indexer_top_k) {
      facts.push(['Sparse indexer', `top-${nf(a.indexer_top_k)}`, 'Attention indexer',
        `Full-attention blocks do not attend over every previous token: an indexer with `
        + `${a.indexer_heads} heads selects the top ${nf(a.indexer_top_k)} to attend to. That keeps `
        + 'attention cost roughly flat as context grows, instead of quadratic.']);
    }

    const factHtml = facts.map(([k, v, tt, tb]) =>
      `<div class="fact explainable" data-explain-title="${esc(tt)}" data-explain-body="${esc(tb)}">
         <div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('');

    // The block stack. Structure is real; nothing animates, because per-layer
    // execution is not observable.
    const blocks = [...Array(nLayer).keys()].map((i) => {
      const isFull = full.has(i);
      const ratio = (a.compress_ratios || [])[i];
      let tip;
      if (!isHybrid) {
        tip = [`Block ${i}`,
          'This model reports no hybrid attention layout, so every block is the same kind: '
          + 'attention followed by a feed-forward network. Each generated token passes through '
          + `all ${nLayer} of them in order.`];
      } else if (isFull) {
        tip = [`Block ${i} — full attention`,
          `Marked by the model as part of its full-attention interval (every `
          + `${a.full_attention_interval} blocks)`
          + (ratio ? `, with compression ratio ${ratio}` : '')
          + '. These blocks are the ones that maintain a conventional key/value cache '
          + 'over the context.'];
      } else {
        tip = [`Block ${i} — linear attention`,
          'A linear/recurrent attention block. It carries a fixed-size state instead of a growing '
          + 'key/value cache, so its cost per token does not grow with context length. '
          + `${nLayer - full.size} of ${nLayer} blocks are of this kind.`];
      }
      const cls = !isHybrid ? 'uni' : (isFull ? 'full' : 'lin');
      return `<div class="blk ${cls} explainable"
        data-explain-title="${esc(tip[0])}" data-explain-body="${esc(tip[1])}"></div>`;
    }).join('');

    const legend = isHybrid
      ? `<span class="item"><span class="swatch block" style="background:var(--series-7)"></span>full attention · ${full.size}</span>
         <span class="item"><span class="swatch block" style="background:var(--series-1)"></span>linear · ${nLayer - full.size}</span>`
      : `<span class="item"><span class="swatch block" style="background:var(--series-1)"></span>attention · ${nLayer}</span>`;

    const stackNote = isHybrid
      ? `Every generated token traverses all ${nLayer} blocks in order.
         ${Math.round((full.size / nLayer) * 100)}% of them are full-attention. Hover a block for what it does.`
      : `Every generated token traverses all ${nLayer} blocks in order. This model reports no hybrid
         attention layout, so they are all the same kind.`;

    const expertSection = isMoE ? `
      <div class="archsection">
        <div class="archhead"><span>Expert routing</span>
          <span class="hint">${nf(a.expert_used)} of ${nf(a.expert_count)} per token</span></div>
        <div class="experts explainable"
             data-explain-title="Sparsity"
             data-explain-body="${esc(
               `Each of the ${nf(a.expert_count)} cells is one feed-forward expert in a block. Every `
               + `token activates ${nf(a.expert_used)} of them — `
               + `${((a.expert_used / a.expert_count) * 100).toFixed(1)}% of the feed-forward `
               + 'parameters. All of them still have to be resident. Which ones fire is '
               + 'decided by a learned router and is not reported by the server, so none are '
               + 'highlighted here.')}">
          ${[...Array(Math.min(a.expert_count, 1024)).keys()].map(() => '<i></i>').join('')}
        </div>
        <div class="note">${nf(a.expert_count)} experts resident, ${nf(a.expert_used)} active per token
          (${((a.expert_used / a.expert_count) * 100).toFixed(1)}%). Routing is not exposed, so no cell
          is marked "firing".</div>
      </div>` : '';

    this.host.innerHTML = `
      <div class="archgrid">${factHtml}</div>

      <div class="archsection">
        <div class="archhead">
          <span>Block stack</span>
          <span class="legend">${legend}</span>
        </div>
        <div class="blocks">${blocks}</div>
        <div class="note">${stackNote}
          <strong>Nothing here animates:</strong> llama.cpp exposes no per-layer timing,
          so showing a pulse travelling the stack would be decoration, not telemetry.</div>
      </div>

      ${expertSection}`;
  }

  paint(snap) {
    void snap;   // structure only - nothing here is live
  }
}

/* =========================================================================
   4. Live request feed
   ========================================================================= */

export function renderSlotLanes(host, snap) {
  const j = snap.journal || {};
  const slots = j.slots || [];
  if (!slots.length) {
    host.innerHTML = `<div class="empty">${j.alive
      ? 'Following the journal — waiting for the first request.'
      : 'Not following the service journal.'}</div>`;
    return;
  }
  host.innerHTML = slots.map((s) => {
    const phase = s.stale ? 'idle' : (s.phase || 'idle');
    const pick = s.pick;
    return `<div class="lane ${esc(phase)}">
      <div class="lid">slot ${esc(s.id)}</div>
      <div class="lphase"><span class="pdot"></span>${esc(phase)}</div>
      <div class="lmeta">${s.task != null && !s.stale ? `task ${esc(s.task)}` : ''}</div>
      <div class="lrate">${phase === 'decode' && s.tg_3s != null
        ? `${s.tg_3s.toFixed(1)}<small> tok/s</small>` : ''}</div>
      <div class="lgen">${s.n_gen && !s.stale ? `${nf(s.n_gen)} gen` : ''}</div>
      <div class="lpick">${pick
        ? (pick.how === 'cache'
            ? `<span class="badge good" title="prompt prefix reused">cache ${(pick.similarity * 100).toFixed(0)}%</span>`
            : '<span class="badge">LRU</span>')
        : ''}</div>
    </div>`;
  }).join('');
}

function ago(ts) {
  if (ts == null) return '—';
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m`
    : s < 86400 ? `${(s / 3600).toFixed(1)}h` : `${(s / 86400).toFixed(1)}d`;
}

export function renderRequestFeed(host, snap, isProbe = () => false, stored = []) {
  const all = (snap.journal || {}).requests || [];
  // Probe requests (a configured health-check fingerprint) can outnumber real
  // work; they have their own series on the decode chart, so here they are
  // counted rather than listed.
  let reqs = all.filter((r) => !isProbe(r));
  const probes = all.length - reqs.length;
  // The live list only holds the last couple of dozen completions. When those
  // are all probes, show the latest stored real ones rather than nothing.
  const fromStore = !reqs.length && stored.some((r) => !isProbe(r));
  if (fromStore) reqs = stored.filter((r) => !isProbe(r)).slice(0, 12);
  if (!reqs.length) {
    host.innerHTML = `<div class="empty">${all.length
      ? `The last ${all.length} completed requests were all probes, and none of the range's `
        + 'stored requests are workload.' : 'No completed requests seen yet.'}</div>`;
    return;
  }
  host.innerHTML = `<div class="tablewrap"><table class="data">
    <thead><tr>
      <th>When</th><th>Task</th><th class="num">Slot</th><th class="num">Prompt</th>
      <th class="num">Prefill</th><th class="num">Generated</th><th class="num">Decode</th>
      <th class="num">Draft acc.</th><th class="num">Wall</th>
    </tr></thead><tbody>${reqs.map((r) => `<tr>
      <td>${ago(r.finished)}</td>
      <td>${esc(r.task)}</td>
      <td class="num">${esc(r.slot)}</td>
      <td class="num">${nf(r.prompt_tokens)}</td>
      <td class="num">${nf(r.prefill_tps)} <small style="color:var(--muted)">tok/s</small></td>
      <td class="num">${nf(r.gen_tokens)}</td>
      <td class="num">${nf(r.decode_tps, 1)} <small style="color:var(--muted)">tok/s</small></td>
      <td class="num">${r.accept_rate != null
        ? `${(r.accept_rate * 100).toFixed(0)}% <small style="color:var(--muted)">×${nf(r.mean_len, 2)}</small>` : '—'}</td>
      <td class="num">${r.wall_s != null ? `${r.wall_s.toFixed(1)}s` : '—'}</td>
    </tr>`).join('')}</tbody></table></div>${fromStore
    ? `<div class="note">The last ${all.length} completed requests were all probes; these are the `
      + 'most recent stored workload requests.</div>'
    : probes
    ? `<div class="note">${probes} probe request${probes === 1 ? '' : 's'} among the last ${all.length} not listed.</div>` : ''}`;
}

/* =========================================================================
   5. KV cache map - what each slot is actually holding
   ========================================================================= */

export function renderKvMap(host, snap) {
  const slots = snap.slots || [];
  const nctx = snap.model?.n_ctx;
  if (!slots.length || !nctx) {
    host.innerHTML = '<div class="empty">No slot context data.</div>';
    return;
  }

  // n_prompt_tokens_cache is reported PER REQUEST: it holds the reuse figure
  // while a request runs and resets on release. And on builds with unified KV
  // and idle-slot caching, an idle slot's own count reads 0 once another slot
  // starts: its state is parked in llama.cpp's host-RAM prompt cache and
  // restored on a matching request, so "0" does not mean the context is gone.
  const busy = slots.filter((s) => s.processing);
  const usedBusy = busy.reduce((a, s) => a + (s.prompt_tokens || 0), 0);
  const cachedBusy = busy.reduce((a, s) => a + (s.prompt_cached || 0), 0);
  const resident = slots.reduce((a, s) => a + (s.prompt_tokens || 0), 0);

  const headline = busy.length
    ? `${nf(cachedBusy)} of ${nf(usedBusy)} prompt tokens in flight were reused `
      + `(${usedBusy ? ((cachedBusy / usedBusy) * 100).toFixed(1) : '0'}%)`
    : `no request in flight · idle state lives in the host-RAM prompt cache`;

  host.innerHTML = `
    <div class="kvlegend">
      <span class="item"><i style="background:var(--series-3)"></i>reused from cache</span>
      <span class="item"><i style="background:var(--series-1)"></i>computed this request</span>
      <span class="item"><i class="held"></i>resident, kept for reuse</span>
      <span class="item"><i class="free"></i>free</span>
      <span class="kvtotal">${headline}</span>
    </div>
    ${slots.map((s) => {
      const used = s.prompt_tokens || 0;
      const pctUsed = (used / nctx) * 100;
      const running = !!s.processing;
      const cached = running ? Math.min(s.prompt_cached || 0, used) : 0;
      const fresh = running ? Math.max(0, used - cached) : 0;
      // A non-zero segment must stay visible, or a 1% slice vanishes.
      const vis = (n) => (n ? Math.max(0.35, (n / nctx) * 100) : 0);
      const segs = running
        ? `<i class="seg cached" style="width:${vis(cached).toFixed(3)}%"></i><i
             class="seg fresh" style="width:${vis(fresh).toFixed(3)}%"></i>`
        : `<i class="seg held" style="width:${vis(used).toFixed(3)}%"></i>`;
      const body = used === 0
        ? `Slot ${s.id} reports no tokens. Either it has not been used, or - on builds that `
          + `park idle slots in llama.cpp's host-RAM prompt cache - its state was moved there when `
          + `another slot started, and a request with the same prefix restores it.`
        : running
          ? `Processing. Of the ${nf(used)} tokens in this slot, ${nf(cached)} were already in `
            + `the cache from a previous request and cost nothing to re-establish; `
            + `${nf(fresh)} had to be computed. That reuse is decided by prefix similarity when `
            + `the slot is picked — it is why prefill on a long conversation stays cheap.`
          : `Idle, holding ${nf(used)} of ${nf(nctx)} tokens (${pctUsed.toFixed(1)}%) — usually the `
            + `most recently used slot. llama.cpp only reports the reused/computed split while a `
            + `request is in flight, so this bar is shown undivided rather than claiming 0% reuse.`;
      return `<div class="kvrow">
        <div class="kvid">slot ${esc(s.id)}</div>
        <div class="kvbar explainable"
             data-explain-title="Slot ${esc(s.id)} key/value cache"
             data-explain-body="${esc(body)}">${segs}</div>
        <div class="kvnum">${nf(used)}<small> / ${nf(nctx)}</small></div>
        <div class="kvpct">${pctUsed.toFixed(1)}%</div>
      </div>`;
    }).join('')}`;
}

export { explain };
