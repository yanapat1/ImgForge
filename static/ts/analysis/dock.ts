// The ◆ assistant on this page: the app's one dock and chat, with the Analysis specialist. It is told what is open, and
// may fill the editor or add charts; running and changing stay with the user's own Run and confirm.

import { mountChat } from '../core/chat.js';
import { dockHooks, refreshDock, setDockHost, withNav } from '../core/dock.js';
import { esc, icon } from '../core/dom.js';
import { llmOn } from '../core/llm.js';
import { openSettings } from '../settings/dialog.js';
import { render } from './app.js';
import { currentResult } from './analyze.js';
import { addCharts } from './dash.js';
import { A, tab } from './state.js';
import type { UiAction, UiResult } from '../types-chat.js';

const SUGGEST = ['What tables do I have about customers?', 'Why is this query slow?', 'Explain the last result in plain words'];

function pageAction(a: UiAction): UiResult {
  if (a.action === 'set_editor_query') {
    const t = tab();
    t.sql = String(a.args['sql'] ?? '');
    t.dirty = true;
    t.typed = true;
    A.ws = 'query';
    render();
    document.dispatchEvent(new CustomEvent('an-sql-changed'));
    return { ok: true };
  }
  if (a.action === 'build_dashboard') {
    addCharts((a.args['charts'] ?? []) as { kind: string; title: string; x?: string; y?: string; agg?: string }[]);
    A.ws = 'dash';
    render();
    return { ok: true };
  }
  return { ok: false, detail: `${a.action} is not available on this page` };
}

export function mountDock(): void {
  const emptyHTML = (): string => `<div class="dc-ai-hello"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><b>Ask about your data.</b><p class="faint">I read your tables and statistics. I see real rows only if you allow it on the Query toolbar. Changes wait for your OK.</p><div class="dc-ai-sugg">${SUGGEST.map((s) => `<button type="button" class="ct-choice" data-ca="suggest" data-q="${esc(s)}">${esc(s)}</button>`).join('')}</div></div>`;
  setDockHost({
    id: 'ai-analysis',
    label: () => 'Data Analysis',
    enabled: () => llmOn('data_center'),
    mount: (el) => {
      const hk = dockHooks();
      return mountChat(el, {
        role: 'analysis',
        inputId: 'ai-analysis-q',
        placeholder: 'Ask about your data…  / for commands',
        scope: () => ({ page: 'analysis', session: A.sid, connection: tab().conn, database: tab().db, result: currentResult()?.id ?? null, rows_ok: A.aiRows }),
        onUiAction: withNav(pageAction),
        onEvent: hk.onEvent,
        onDone: hk.onDone,
        onUpdate: () => refreshDock(),
        settings: (sec) => { void openSettings(sec === 'llm' ? 'llm' : 'prompts'); },
        emptyHTML,
        exportName: () => 'analysis-chat',
      });
    },
    settings: () => { void openSettings('llm'); },
  });
}
