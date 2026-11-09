// The one page header (kit.css + layout.css .app-header): the Dataset Visualizer brand, the page links (Sessions,
// Data, Analysis; a zone that is not installed has no link) and the header actions (settings, theme; the page mounts
// the user menu into #<actionsId>). The Data Center and Data Analysis render it; the home's copy in index.html has
// the same markup. Clicks go through the page's own data-act handler: settings, theme.

import { hasSessions, zoneOn } from './auth.js';
import { esc, icon, ICONS } from './dom.js';
import { analysisUrl, dataUrl, homeUrl } from './nav.js';

export type HeaderPage = 'home' | 'data' | 'analysis' | 'none';

export interface HeaderOpts {
  page: HeaderPage;
  /** an extra class on the header (the page's own narrow-screen rules) */
  cls?: string;
  /** markup between the links and the actions (the Data Center's search box), after a spacer */
  middle?: string;
  /** buttons in front of settings and theme */
  actions?: string;
  /** the id of the actions box, where the page mounts the user menu */
  actionsId: string;
}

const link = (on: boolean, href: string, ico: string, label: string, extra = ''): string =>
  `<a${on ? ' class="on" aria-current="page"' : ''} href="${esc(href)}">${icon(ico)}${label}${extra}</a>`;

export function appHeader(o: HeaderOpts): string {
  const home = hasSessions() ? homeUrl() : dataUrl();
  return `<header class="app-header${o.cls ? ` ${o.cls}` : ''}">` +
    `<a class="brand" href="${esc(home)}" aria-label="Dataset Visualizer: ${hasSessions() ? 'Sessions' : 'Data'}">` +
    `<svg class="brand-mark" viewBox="0 0 32 32" fill="none" stroke="currentColor" aria-hidden="true"><use href="${ICONS}#i-brand"/></svg>` +
    '<h1>Dataset Visualizer</h1></a>' +
    '<nav class="hdr-nav" aria-label="Pages">' +
    (hasSessions() ? link(o.page === 'home', homeUrl(), 'layers', 'Sessions') : '') +
    link(o.page === 'data', dataUrl(), 'db', 'Data', '<span id="dc-nav-n" class="count num"></span>') +
    (zoneOn('analysis') ? link(o.page === 'analysis', analysisUrl(), 'search', 'Analysis') : '') +
    '</nav><span class="grow"></span>' + (o.middle ?? '') +
    `<div id="${esc(o.actionsId)}" class="header-actions">${o.actions ?? ''}` +
    `<button type="button" class="icon-btn ghost" data-act="settings" aria-label="Settings: connections, labeling, appearance" title="Settings">${icon('gear')}</button>` +
    '<button type="button" class="icon-btn ghost" data-act="theme" aria-label="Switch light / dark theme" title="Switch light / dark theme">' +
    `<svg class="ico only-dark" aria-hidden="true"><use href="${ICONS}#i-sun"/></svg><svg class="ico only-light" aria-hidden="true"><use href="${ICONS}#i-moon"/></svg></button>` +
    '</div></header>';
}
