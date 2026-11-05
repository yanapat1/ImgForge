// Builds the static mockup: ?theme=light|dark  &scene=viz|data  &collapsed=1. window.VARIANT = 1 | 2 | 3.
(() => {
  const Q = new URLSearchParams(location.search);
  document.documentElement.dataset.theme = Q.get('theme') === 'light' ? 'light' : 'dark';
  const V = window.VARIANT || 1, scene = Q.get('scene') || 'viz', collapsed = Q.get('collapsed') === '1';
  const I = (p) => `<svg class="ico" viewBox="0 0 24 24">${p}</svg>`;
  const ic = {
    explore: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>', stats: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    cur: '<path d="M12 3 4 7v6c0 4 3.5 7 8 8 4.5-1 8-4 8-8V7z"/>', model: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6v6H9z"/>',
    eval: '<path d="M4 12l5 5L20 6"/>', cmp: '<path d="M7 4v16M17 4v16M3 8h8M13 16h8"/>', send: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>', side: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>', pin: '<path d="M9 4h6l-1 6 3 3H7l3-3zM12 13v7"/>',
    hist: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2"/>', plus: '<path d="M12 5v14M5 12h14"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  };
  // seeded scatter: 4 clusters
  let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const cols = ['#5ccbff', '#ff9f43', '#b98cff', '#5fd17a'], cent = [[30, 40], [62, 30], [48, 68], [76, 66]];
  let dots = '';
  for (let k = 0; k < 4; k++) for (let i = 0; i < 160; i++) {
    const a = rnd() * 6.283, r = Math.sqrt(-2 * Math.log(rnd() + 1e-9)) * 6;
    dots += `<circle cx="${cent[k][0] + Math.cos(a) * r}%" cy="${cent[k][1] + Math.sin(a) * r * 1.1}%" r="3" fill="${cols[k]}" fill-opacity=".75"/>`;
  }
  const names = ['car', 'person', 'truck', 'bicycle'];

  const actBar = `<nav class="act-bar">
    <div class="act">${I(ic.explore)}</div><div class="act">${I(ic.stats)}</div><div class="act">${I(ic.cur)}</div><div class="act-sep"></div>
    <div class="act on ai-touch">${I(ic.model)}</div><div class="act">${I(ic.eval)}</div><div class="act">${I(ic.cmp)}</div></nav>`;
  const side = scene === 'data' ? `<aside class="side"><h3>Buckets</h3>
      <div class="sel">traffic-2026</div><div class="sel">warehouse-cams</div><div class="sel">s3 · fleet-raw</div></aside>`
    : `<aside class="side"><h3>Model</h3>
      <label class="field">Model<div class="sel">yolov8s-best.pt</div></label>
      <label class="field">Split<div class="sel">val · 1,204 images</div></label>
      <label class="field">Confidence<div class="sel">0.25</div></label>
      <button class="primary run ai-touch">Run on this dataset</button>
      <div><div class="mrow"><span class="muted">mAP50</span><b class="mono">0.712</b></div>
      <div class="mrow"><span class="muted">Precision</span><b class="mono">0.781</b></div>
      <div class="mrow"><span class="muted">Recall</span><b class="mono">0.664</b></div></div></aside>`;
  const main = scene === 'data' ? `<section class="main"><div class="dc-table">
      ${['images/train', 'images/val', 'labels/train', 'labels/val', 'data.yaml'].map((n, i) => `<div class="dc-row ${i === 1 ? 'on' : ''}"><span>▸</span><span>${n}</span><span class="muted mono">${[8412, 1204, 8412, 1204, 1][i]} files</span><span class="muted">${i < 4 ? 'folder' : 'yaml'}</span><span class="faint">2026-10-2${i}</span></div>`).join('')}
    </div></section>`
    : `<section class="main"><div class="tabs"><span>Map</span><span class="on">Model</span><span>Eval</span></div>
      <svg class="plot">${dots}</svg>
      <div class="legend">${names.map((n, k) => `<span><i style="background:${cols[k]}"></i>${n}</span>`).join('')}</div></section>`;

  const ring = (pct) => `<div class="ring" title="context used · auto-compacts at 80%"><svg viewBox="0 0 36 36">
      <circle cx="18" cy="18" r="15" fill="none" stroke="var(--prog-track)" stroke-width="4"/>
      <circle cx="18" cy="18" r="15" fill="none" stroke="var(--warn)" stroke-width="1.5" stroke-dasharray="1 94.2" stroke-dashoffset="-75.4"/>
      <circle cx="18" cy="18" r="15" fill="none" stroke="var(--gem-2)" stroke-width="4" stroke-linecap="round" stroke-dasharray="${pct * 0.942} 94.2"/></svg><b>${pct}</b></div>`;
  const mode = V === 1 ? 'ask' : 'auto';
  const seg = `<span class="seg"><span class="${mode === 'ask' ? 'on' : ''}">Ask</span><span class="auto ${mode === 'auto' ? 'on' : ''}">Auto</span><span>Plan</span></span>`;
  const head = `<div class="d-head"><span class="gem">◆</span><div class="grow"><div class="d-title">Assistant</div>
      <div class="d-sub">Worst class on val · claude-sonnet-5-5</div></div>
      ${ring(34)}<button class="icon ghost" title="History">${I(ic.hist)}</button><button class="icon ghost" title="New chat">${I(ic.plus)}</button>
      ${V === 2 ? `<button class="icon ghost" title="Pin as dock">${I(ic.pin)}</button>` : ''}
      <button class="icon ghost" title="Collapse (Ctrl+.)">${I(V === 2 ? ic.x : ic.side)}</button></div>`;
  const ctx = scene === 'data'
    ? `<div class="ctx"><span class="chip">Page <b>Data Center</b></span><span class="chip">Bucket <b>traffic-2026</b></span><span class="chip">Folder <b>images/val</b></span></div>`
    : `<div class="ctx"><span class="chip">Page <b>Visualizer · Model</b></span><span class="chip">Dataset <b>traffic-2026</b></span><span class="chip">Map <b>DINOv2 · UMAP 2D</b></span>${seg}</div>`;

  const compacted = `<div class="divider">Context compacted · 38 turns → 2.1k tokens · <a>view summary</a></div>
    ${V !== 2 ? `<div class="summary"><div><b>Goal:</b> find why recall dropped after the v3 relabel.</div>
      <div><b>Done:</b> DINOv2 UMAP map; 214 near-duplicates tagged <span class="mono">dup-v3</span> (undo u-81f2).</div>
      <div><b>Prefs:</b> conf 0.25, IoU 0.5; never delete, only tag.</div><div><b>Next:</b> run yolov8s on val, rank classes.</div></div>` : ''}`;
  const approve = `<div class="card approve"><div class="t"><span class="badge run">Run</span>Run model on dataset</div>
      <div class="kv"><span>Model</span><b>yolov8s-best.pt</b><span>Data</span><b>val split · 1,204 images</b><span>Settings</span><b>conf 0.25 · IoU 0.5</b><span>Takes</span><b>about 2 min on GPU 0</b></div>
      <div class="acts"><button>Reject</button><button class="primary">Approve &amp; run</button></div></div>`;
  const job = `<div class="card"><div class="t"><span class="badge run">Job</span>Running yolov8s-best.pt<span class="grow"></span><span class="mono muted">64%</span></div>
      <div class="bar"><i style="width:64%"></i></div><div class="muted">772 / 1,204 images · 0:41 left · <a style="color:var(--accent-text)">Stop</a></div></div>`;
  const answer = `<p>Done. <b>truck</b> is the weakest class: most misses are small, far trucks.</p>
      <table class="mini"><tr><th>Class</th><th style="text-align:right">AP50</th><th style="text-align:right">Missed</th></tr>
      <tr class="worst"><td>truck</td><td class="n">0.48</td><td class="n">131</td></tr><tr><td>bicycle</td><td class="n">0.63</td><td class="n">88</td></tr>
      <tr><td>person</td><td class="n">0.77</td><td class="n">64</td></tr><tr><td>car</td><td class="n">0.86</td><td class="n">40</td></tr></table>
      <p class="muted">I coloured the map by error type. Want me to filter to missed trucks?</p>`;
  const cant = `<div class="card cant"><div class="t"><span class="badge cant">Can't</span>Create a labeling project</div>
      <div class="muted">Starting new projects is not something I'm allowed to do. Open <b>Home → New session → Label</b> and pick <span class="mono">images/val</span>; I can fill in the classes for you after.</div></div>`;

  let log;
  if (V === 1) log = `${compacted}
      <div class="u">Run yolov8s on val and tell me which class is worst</div>
      <div class="a"><span class="gem">◆</span><div class="body"><div class="activity"><span class="ok">✓</span> Opened <b>Model</b> tab · read eval summary · 2 steps</div>${approve}</div></div>`;
  else if (V === 2) log = `${compacted}
      <div class="u">Run yolov8s on val and tell me which class is worst</div>
      <div class="a"><span class="gem">◆</span><div class="body"><div class="activity"><span class="ok">✓</span> Opened <b>Model</b> tab · started run (Auto)</div>${job}</div></div>
      <div class="u">Then make a labeling project for the val misses</div>
      <div class="a"><span class="gem">◆</span><div class="body">${cant}</div></div>`;
  else log = `${compacted}
      <div class="u">Run yolov8s on val and tell me which class is worst</div>
      <div class="a"><span class="gem">◆</span><div class="body"><div class="activity"><span class="ok">✓</span> Opened <b>Model</b> · ran yolov8s (1m 52s) · read per-class AP</div>${answer}</div></div>`;
  if (scene === 'data') log = `<div class="u">Show me this folder on the map with a 3D view</div>
      <div class="a"><span class="gem">◆</span><div class="body"><div class="activity"><span class="ok">✓</span> Found session <b>traffic-2026</b> for this folder</div>
      <p>Opening the Visualizer and generating <b>DINOv2 · UMAP 3D</b>. I'll continue there, nothing to retype.</p>
      <div class="card"><div class="t"><span class="badge run">Go</span>Data Center → Visualizer</div><div class="bar"><i style="width:30%"></i></div><div class="muted">Handing off this chat…</div></div></div></div>`;

  const tasks = V === 3 ? `<div class="tasks"><div class="th">Tasks<span class="grow"></span><span class="mono">2 / 4</span></div>
      <div class="drive">◆ <b>is driving</b><span class="sep">›</span>Model tab<span class="sep">›</span>Run yolov8s<span class="grow"></span><button class="ghost" style="height:20px">${I(ic.stop)}Stop</button></div>
      <div class="step done"><span class="dot">✓</span><span>Open Model tab</span></div>
      <div class="step done"><span class="dot">✓</span><span>Run yolov8s on val</span><span class="pct">1m 52s</span></div>
      <div class="step doing"><span class="dot"></span><span>Read per-class AP and misses</span><span class="pct">…</span></div>
      <div class="step"><span class="dot"></span><span>Explain + suggest a filter</span></div></div>` : '';
  const slash = V === 2 ? '' : '';
  const composer = `<div class="composer"><div class="ph">Ask, or type <span class="mono">/</span> for commands (/compact, /model, /mode…)</div>
      <div class="row"><span class="chip">sonnet-5-5</span><span class="chip">${mode === 'ask' ? 'Ask' : 'Auto'} <kbd>⇧Tab</kbd></span><span class="chip">effort med</span><span class="grow"></span>
      <button class="send">${I(ic.send)}</button></div></div>`;
  const dock = collapsed ? `<aside class="rail"><span class="gem">◆</span>${ring(34)}<kbd>Ctrl .</kbd></aside>`
    : `<aside class="dock ${V === 2 ? 'driving' : ''}"><div class="dock-grip"></div>${head}${ctx}${tasks}<div class="log">${log}</div>${slash}${composer}</aside>`;

  const dockW = collapsed ? '44px' : (V === 2 ? '0px' : '420px');
  document.body.className = 'v' + V;
  document.body.innerHTML = `<header class="hdr"><span class="brand"><span class="gem">◆</span>dataviz</span>
      <span class="pill">traffic-2026</span><span class="pill">9,616 images · 4 classes</span><span class="grow"></span>
      ${V !== 2 && !collapsed ? '' : `<button class="ghost">◆ Assistant <kbd>Ctrl .</kbd></button>`}</header>
    <div class="shell" style="--dock-w:${dockW}">${actBar}${side}${main}${V === 2 ? '' : dock}</div>
    ${V === 2 ? `<div class="drivebar"><span class="gem" style="width:18px;height:18px;font-size:9px">◆</span><b>Assistant is driving</b> · Model tab › Run yolov8s · <kbd>Esc</kbd> to stop</div>${dock}` : ''}
    <div class="label-tag">Variant ${V} · ${['', 'Push dock', 'Floating sheet', 'Dock + task rail'][V]}${collapsed ? ' · collapsed' : ''}${scene === 'data' ? ' · Data Center' : ''}</div>`;
})();
