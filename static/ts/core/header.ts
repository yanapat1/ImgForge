// The one page header (kit.css + layout.css .app-header): the DataViz brand (opens Home), the page links in one fixed
// order, Bucket · Image · Data, whatever order the zones are installed in (a zone that is not installed has no link),
// and the header actions (settings, theme; the page mounts the user menu into #<actionsId>). Home, Bucket and Data
// render it; the Image page's copy in index.html has the same markup. Clicks go through the page's own data-act
// handler: settings, theme.

import { hasImage, zoneOn } from './auth.js';
import { esc, icon, ICONS } from './dom.js';
import { bucketUrl, dataUrl, homeUrl, imageUrl } from './nav.js';

export type HeaderPage = 'home' | 'bucket' | 'image' | 'data' | 'none';

export interface HeaderOpts {
  page: HeaderPage;
  /** an extra class on the header (the page's own narrow-screen rules) */
  cls?: string;
  /** markup between the links and the actions (the Bucket page's search box), after a spacer */
  middle?: string;
  /** buttons in front of settings and theme */
  actions?: string;
  /** the id of the actions box, where the page mounts the user menu */
  actionsId: string;
}

/** The pages of the header, in their one order. */
export const PAGES: ReadonlyArray<{ page: Exclude<HeaderPage, 'home' | 'none'>; on: () => boolean; href: () => string; icon: string; label: string }> = [
  { page: 'bucket', on: () => zoneOn('bucket'), href: () => bucketUrl(), icon: 'db', label: 'Bucket' },
  { page: 'image', on: hasImage, href: () => imageUrl(), icon: 'layers', label: 'Image' },
  { page: 'data', on: () => zoneOn('data'), href: () => dataUrl(), icon: 'search', label: 'Data' },
];

const link = (on: boolean, href: string, ico: string, label: string): string =>
  `<a${on ? ' class="on" aria-current="page"' : ''} href="${esc(href)}">${icon(ico)}${label}</a>`;

export function appHeader(o: HeaderOpts): string {
  return `<header class="app-header${o.cls ? ` ${o.cls}` : ''}">` +
    `<a class="brand" href="${esc(homeUrl())}" aria-label="DataViz: Home">` +
    `<svg class="brand-mark" viewBox="0 0 32 32" fill="none" stroke="currentColor" aria-hidden="true"><use href="${ICONS}#i-brand"/></svg>` +
    '<h1>DataViz</h1></a>' +
    '<nav class="hdr-nav" aria-label="Pages">' +
    PAGES.filter((p) => p.on()).map((p) => link(o.page === p.page, p.href(), p.icon, p.label)).join('') +
    '</nav><span class="grow"></span>' + (o.middle ?? '') +
    `<div id="${esc(o.actionsId)}" class="header-actions">${o.actions ?? ''}` +
    `<button type="button" class="icon-btn ghost" data-act="settings" aria-label="Settings: connections, labeling, appearance" title="Settings">${icon('gear')}</button>` +
    '<button type="button" class="icon-btn ghost" data-act="theme" aria-label="Switch light / dark theme" title="Switch light / dark theme">' +
    `<svg class="ico only-dark" aria-hidden="true"><use href="${ICONS}#i-sun"/></svg><svg class="ico only-light" aria-hidden="true"><use href="${ICONS}#i-moon"/></svg></button>` +
    '</div></header>';
}
