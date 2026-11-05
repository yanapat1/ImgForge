// The ◆ assistant dock on the sessions home and the labeling page (core/dock.ts; #ai-page is the host section).
// Both run the Data assistant (role 'data': buckets, folders, tags, sending to a session); the scope says which
// page the user is on, so it can point to the right place. The labeling page keeps its own Label assistant
// (label/assist.ts) for boxes: this dock answers questions about the data around it.

import { mountChat } from '../core/chat.js';
import { dockHooks, refreshDock, setDockHost, withNav } from '../core/dock.js';
import { esc, icon } from '../core/dom.js';
import { llmOn } from '../core/llm.js';
import { openSettings } from '../settings/dialog.js';

type Page = 'home' | 'label';

const SUGGEST: Record<Page, string[]> = {
  home: ['Which of my buckets are not YOLO yet?', 'Find folders with images but no labels', 'What can you do for me?'],
  label: ['How many images are left in this project?', 'Which folder did these images come from?', 'What can you do for me?'],
};

export function initPageDock(page: Page, project: string | null = null): void {
  const emptyHTML = (): string => `<div class="dc-ai-hello"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><div>` +
    `<b>${page === 'home' ? 'Ask about your datasets, or tell me what to do.' : 'Ask about the data behind this labeling project.'}</b>` +
    '<p class="faint">I read freely and can open the right page for you. Changes wait for your OK in <b>ask</b> mode; Shift+Tab switches to auto or plan.</p>' +
    `<div class="dc-ai-sugg">${SUGGEST[page].map((s) => `<button type="button" class="ct-choice" data-ca="say" data-text="${esc(s)}">${esc(s)}</button>`).join('')}</div></div></div>`;
  setDockHost({
    id: 'ai-page',
    label: () => (page === 'home' ? 'Home' : 'Labeling'),
    enabled: () => llmOn('data_center'),
    mount: (el) => {
      const hk = dockHooks();
      return mountChat(el, {
        role: 'data',
        inputId: 'ai-page-q',
        placeholder: 'Ask or tell the assistant…  / for commands',
        scope: () => ({ page, ...(project ? { label_project: project } : {}) }),
        onUiAction: withNav(),
        onEvent: hk.onEvent,
        onDone: hk.onDone,
        onUpdate: () => refreshDock(),
        settings: (sec) => { void openSettings(sec === 'llm' ? 'llm' : 'prompts'); },
        emptyHTML,
        exportName: () => `${page}-chat`,
      });
    },
    settings: () => { void openSettings('llm'); },
  });
}
