// The ◆ assistant dock on Home, the Image page and the labeling page (core/dock.ts; #ai-page is the host section).
// They run the Bucket assistant (role 'bucket': buckets, folders, tags, sending to a session); the scope says which
// page the user is on, so it can point to the right place. The labeling page keeps its own Label assistant
// (label/assist.ts) for boxes: this dock answers questions about the data around it.

import { mountChat } from '../core/chat.js';
import { dockHooks, refreshDock, setDockHost, withNav } from '../core/dock.js';
import { esc, icon } from '../core/dom.js';
import { llmOn } from '../core/llm.js';
import { openSettings } from '../settings/dialog.js';

type Page = 'home' | 'image' | 'label';

// every suggestion is something the Bucket specialist's tools answer (it has none for labeling progress or history)
const SUGGEST: Record<Page, string[]> = {
  home: ['Which of my buckets are not YOLO yet?', 'Find folders with images but no labels', 'What can you do for me?'],
  image: ['Which sessions use the bucket cars?', 'Find folders with images but no labels', 'What can you do for me?'],
  label: ['Which of my folders still have no labels?', 'Which sessions can I open in the visualizer?', 'What can you do for me?'],
};

export function initPageDock(page: Page, project: string | null = null): void {
  const emptyHTML = (): string => `<div class="bk-ai-hello"><span class="ai-mark" aria-hidden="true">${icon('gem')}</span><div>` +
    `<b>${page === 'label' ? 'Ask about your buckets and sessions while you label.' : 'Ask about your datasets, or tell me what to do.'}</b>` +
    '<p class="faint">I read freely and can open the right page for you. Changes wait for your OK in <b>ask</b> mode; Shift+Tab switches to auto or plan.</p>' +
    `<div class="bk-ai-sugg">${SUGGEST[page].map((s) => `<button type="button" class="ct-choice" data-ca="say" data-text="${esc(s)}">${esc(s)}</button>`).join('')}</div></div></div>`;
  setDockHost({
    id: 'ai-page',
    label: () => ({ home: 'Home', image: 'Image', label: 'Labeling' })[page],
    enabled: () => llmOn('bucket_data'),
    mount: (el) => {
      const hk = dockHooks();
      return mountChat(el, {
        role: 'bucket',
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
