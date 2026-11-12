// Scripts: named command boxes kept with the session, such as "3 temp with doc". Coming back to a session shows them;
// nothing runs until Run all. A statement that is not a plain read asks first, like everywhere else.

import { uuid } from '../core/uuid.js';
import { errText } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import { renderTree } from './app.js';
import { A, api } from './state.js';
import type { RunState, Script } from './state.js';

let saveTimer: ReturnType<typeof setTimeout> | undefined;
let running: RunState | null = null;

const cur = (): Script | undefined => A.scripts[A.script];

export async function renderScripts(reload = true): Promise<void> {
  const w = $req('an-work');
  if (reload) {
    try { A.scripts = (await api.scripts()).scripts; } catch (e) { w.innerHTML = `<div class="an-empty empty">${esc(errText(e))}</div>`; return; }
    if (!A.scripts.length) A.scripts = [{ id: uuid(), name: 'command1', position: 0, connection: A.sources[0]?.id ?? null, database: A.sources[0]?.database || null, body: '' }];
  }
  const s = cur() ?? A.scripts[0];
  if (!s) return;
  w.innerHTML = `<div class="an-toolbar toolbar"><input class="txt" id="an-sname" aria-label="Command name" value="${esc(s.name)}"><button type="button" class="primary" data-act="script-run"${running ? ' disabled' : ''}>${icon('play')}Run all</button>` +
    `<select id="an-sconn" aria-label="Connection"><option value="">Choose a connection</option>${A.sources.map((x) => `<option value="${esc(x.id)}" ${x.id === s.connection ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select>` +
    `<input class="txt" id="an-sdb" aria-label="Database" placeholder="Database" value="${esc(s.database ?? '')}"><span class="grow"></span><span class="pill" id="an-sprog"></span></div>` +
    `<div class="an-scr"><div class="an-scrl">${A.scripts.map((x, i) => `<button type="button" class="an-cmd ${i === A.script ? 'on' : ''}" data-act="script-open" data-i="${i}"><span class="lbl">${esc(x.name)}</span></button>`).join('')}<button type="button" class="an-cmd faint" data-act="script-new">+ New command box</button><button type="button" class="an-cmd faint" data-act="script-del">Delete this box</button><p class="faint an-scrnote">When you come back to this session, your commands are here. Nothing runs until you press Run all.</p></div>` +
    `<textarea class="an-sbody" id="an-sbody" spellcheck="false" aria-label="Commands" placeholder="SELECT * INTO #recent FROM dbo.Orders;&#10;SELECT * FROM #recent;">${esc(s.body)}</textarea></div>`;
}

function save(): void {
  const s = cur();
  if (!s) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { void api.saveScript(s).catch((e: unknown) => toast(errText(e), 'err')); }, 500);
}

export function scriptInput(t: HTMLInputElement): boolean {
  const s = cur();
  if (!s) return false;
  if (t.id === 'an-sbody') s.body = t.value;
  else if (t.id === 'an-sname') s.name = t.value;
  else if (t.id === 'an-sdb') s.database = t.value || null;
  else if (t.id === 'an-sconn') s.connection = t.value || null;
  else return false;
  save();
  return true;
}

async function runAll(): Promise<void> {
  const s = cur();
  if (!s || running) return;
  if (!s.connection) { toast('Choose a connection first', 'err'); return; }
  if (!s.body.trim()) { toast('This box is empty', 'err'); return; }
  const chk = await api.check(s.connection, s.database, s.body);
  if (chk.confirm) {
    const list = chk.statements.filter((x) => x.kind !== 'read');
    const writes = list.some((x) => x.kind === 'write');
    const ok = await openDialog({
      title: `Confirm ${list.length} statement${list.length > 1 ? 's' : ''} in “${s.name}”`, wide: true, okClass: writes ? 'danger-btn' : 'primary', onOk: () => null, ok: `Run ${list.length} statement${list.length > 1 ? 's' : ''}`,
      body: `<div class="an-confirm">${list.map((x) => `<div class="an-cs"><span class="badge solid ${x.kind}">${x.kind === 'write' ? 'change' : 'temp'}</span><code>${esc(x.sql)}</code></div>`).join('')}</div>`,
    });
    if (!ok) return;
  }
  running = await api.run(s.connection, s.database, s.body, chk.confirm, s.name);
  await renderScripts(false);
  while (running.status === 'running') {
    await new Promise((r) => setTimeout(r, 300));
    running = await api.runState(running.id);
    const p = $('an-sprog');
    if (p) p.textContent = `Statement ${running.statement} of ${running.total}`;
  }
  const done = running;
  running = null;
  await renderScripts(false);
  const p = $('an-sprog');
  if (p) p.textContent = done.status === 'done' ? `Done in ${(done.ms / 1000).toFixed(2)} s, ${done.total} statement${done.total > 1 ? 's' : ''}` : done.error ?? done.status;
  if (done.error) toast(done.error, 'err');
  A.temps = (await api.heartbeat()).temps;
  renderTree();
}

export const S_ACTIONS: Record<string, (el: HTMLElement) => void | Promise<void>> = {
  'script-run': () => runAll(),
  'script-open': (el) => { A.script = Number(el.dataset['i']); return renderScripts(false); },
  'script-new': async () => {
    const c = cur();
    A.scripts.push({ id: uuid(), name: `command${A.scripts.length + 1}`, position: A.scripts.length, connection: c?.connection ?? null, database: c?.database ?? null, body: '' });
    A.script = A.scripts.length - 1;
    await renderScripts(false);
    save();
  },
  'script-del': async () => {
    const s = cur();
    if (!s) return;
    await api.dropScript(s.id);
    A.scripts.splice(A.script, 1);
    A.script = 0;
    await renderScripts(!A.scripts.length);
  },
};
