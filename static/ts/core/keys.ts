// The keyboard shortcut sheet: "?" (not while typing, nor over a dialog) opens a list of the keys of the screen on
// screen. Each screen's keys are listed here once, next to where they are named for people; the label page adds its
// own (user-mappable) hotkeys with registerKeys(). The handlers themselves stay in their modules.

import { esc, isTypingTarget } from './dom.js';
import { dialogOpen, openDialog } from '../views/dialog.js';

export type KeyRow = [keys: string, what: string];
interface Section { title: string; when: () => boolean; rows: () => KeyRow[] }

const screen = (): string => document.documentElement.dataset['screen'] ?? '';
const shown = (id: string): boolean => { const el = document.getElementById(id); return !!el && !el.classList.contains('hidden'); };

const sections: Section[] = [
  { title: 'Everywhere', when: () => true, rows: () => [['?', 'This list of keys'], ['Tab / Shift+Tab', 'Move between controls'], ['Esc', 'Close a dialog, menu or popover']] },
  { title: 'Visualizer', when: () => screen() === 'viz', rows: () => [
    ['/', 'Describe boxes (text search)'], ['Ctrl+Alt+← / →', 'Collapse or bring back the left / right side'],
    ['Enter', 'Open the focused card'], ['Delete', 'Delete the picked boxes'], ['Esc', 'Clear the lasso / selection']] },
  { title: 'Image viewer', when: () => screen() === 'viz' && shown('modal'), rows: () => [
    ['← / →', 'Previous / next image'], ['+ / − / 0 / 1', 'Zoom in / out / fit / actual size'], ['Shift+arrows, W A S D', 'Move the zoomed image'],
    ['B', 'Show / hide boxes'], ['L', 'Show / hide class names'], ['P', 'Show / hide predictions'], ['E', 'Edit boxes'],
    ['Space + drag', 'Pan while editing'], ['Ctrl+S / Ctrl+Z', 'Save / undo box edits']] },
  { title: 'Curation', when: () => screen() === 'viz' && shown('tab-curation'), rows: () => [
    ['J / ↓, K / ↑', 'Next / previous issue'], ['F or Enter', 'Apply the first fix'], ['I', 'Ignore the issue'], ['U', 'Undo the last fix']] },
  { title: 'Compare viewer', when: () => screen() === 'viz' && !!document.querySelector('.cmpv-wrap:not(.hidden)'), rows: () => [
    ['← / →', 'Previous / next image'], ['1 / 2 / 3', 'Side by side / flicker / swipe'], ['Space', 'Start / stop flicker'], ['0', 'Reset the zoom']] },
  { title: 'Bucket', when: () => screen() === 'bucket', rows: () => [
    ['/', 'Search files, folders, tags'], ['Arrow keys', 'Move in the tree or the file grid'], ['→ / ←', 'Open / close a folder in the tree'],
    ['Space', 'Select the focused file'], ['Shift+arrows', 'Extend the selection'], ['Enter', 'Open'], ['Ctrl+A', 'Select all'],
    ['Delete', 'Delete the selection (with Undo)'], ['F2', 'Rename'], ['Menu key / Shift+F10', 'Actions of the focused file'], ['Esc', 'Clear the selection']] },
];

/** Add a section (the label page's hotkeys); `rows` is read each time the sheet opens, so remapped keys show. */
export function registerKeys(title: string, when: () => boolean, rows: () => KeyRow[]): void {
  sections.push({ title, when, rows });
}

export function openKeyHelp(): void {
  const body = sections.filter((s) => s.when()).map((s) => {
    const rows = s.rows();
    return rows.length ? `<section class="kh-sec"><h4>${esc(s.title)}</h4><dl class="kh">` +
      rows.map(([k, w]) => `<dt>${k.split(' / ').map((x) => `<kbd>${esc(x)}</kbd>`).join(' / ')}</dt><dd>${esc(w)}</dd>`).join('') + '</dl></section>' : '';
  }).join('');
  void openDialog({ title: 'Keyboard shortcuts', body: `<div class="kh-wrap">${body}</div>`, ok: 'Close', wide: true, noCancel: true, onOk: () => null, focus: '[data-dlg="ok"]' });
}

let bound = false;
export function bindKeyHelp(): void {
  if (bound) return;
  bound = true;
  document.addEventListener('keydown', (e) => {
    if (e.key !== '?' || e.ctrlKey || e.metaKey || e.altKey || isTypingTarget(e.target) || dialogOpen()) return;
    if (document.querySelector('.modal:not(.hidden):not(#modal)')) return;      // another dialog has the keys
    e.preventDefault();
    openKeyHelp();
  });
}
