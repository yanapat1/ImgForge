// Settings dialog renderers: one function per section / form, returning HTML from SET state.

import { getJSON } from '../core/api.js';
import { can, cannot } from '../core/auth.js';
import { $req, esc, icon } from '../core/dom.js';
import { lookName, PALETTES, paletteName, themeMode, THEME } from '../core/theme.js';
import type { PaletteSpec } from '../core/theme.js';
import { MAX_DIST_PICKS } from '../plot/marks.js';
import type { BrowseResult, ConnDraft, ConnItem, ConnKind, ConnTestResult, DatasetItem, EmbeddingsSummary, FolderEntry, LocalForm, S3Form, SectionId, SetForm, StatusLine, UnionForm,
  UnionPart, YoloInfo } from '../types-settings.js';
import { SET } from './dialog.js';

/** `el.dataset[k]` as a string ('' when the attribute is missing). */
export const dd = (el: HTMLElement, k: string): string => el.dataset[k] ?? '';

/** The `key` attribute of the closest `selector` ancestor of `el` ('' when there is none). */
export const rowAttr = (el: HTMLElement, selector: string, key: string): string => {
  const row = el.closest<HTMLElement>(selector);
  return row ? dd(row, key) : '';
};

/** Enable / disable a control (a data-act element that is not a button has no `disabled`). */
export function setDisabled(el: HTMLElement, on: boolean): void {
  if (el instanceof HTMLButtonElement || el instanceof HTMLInputElement) el.disabled = on;
}

/** SET.form as the form of that tag (the caller knows which form is open). */
export function formOf<K extends SetForm['form']>(kind: K): Extract<SetForm, { form: K }> {
  const f = SET.form;
  if (f === null || f.form !== kind) throw new Error(`the ${kind} form is not open`);
  // narrowing a generic Extract by the tag check: the check above proves f.form === kind
  return f as Extract<SetForm, { form: K }>;
}

export const SECTIONS: Record<SectionId, [string, string]> = {
  datasets: ['Sessions', 'One dataset each: local folders are read in place; S3 / RustFS datasets stream or keep a local cache; a labeling task is read live'],
  connections: ['Connections', 'S3 / RustFS buckets, FTP and SFTP servers, PostgreSQL and SQL Server databases, model servers'],
  labeling: ['Labeling', 'Drawing, name tags, committing and the keyboard shortcuts of the labeling page'],
  models: ['Models', 'YOLO, RF-DETR or custom detectors (files or HTTP APIs), scored in the Model tab — and your own backbones as feature spaces of the plot'],
  llm: ['AI assistant', 'Connect an LLM (OpenAI, Claude, Gemini, vLLM, Ollama…) and choose which AI features to show'],
  appearance: ['Appearance', 'Theme for the page and every plot'],
  plot: ['Plot', 'Drawing and measuring on the embedding plot'],
  account: ['Account', 'Your picture, name, username, email and password, and the devices you are signed in on'],
  users: ['Users & admin', 'Everyone who can sign in, sign-up and connection rules, recent activity'],
  workspace: ['Workspace', 'Where this dataset’s crops, features and projections are cached'],
};

export function fmtBytes(bytes: number | undefined): string {
  const b = bytes ?? 0;
  return b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.round(b / 1e3)} kB`;
}

function timeAgo(t: number): string {
  const s = Date.now() / 1000 - t;
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}

export const tagc = (text: string, cls = '', ico = ''): string => `<span class="tagc ${cls}">${ico ? icon(ico) : ''}${esc(text)}</span>`;

// ---- datasets
export function renderDatasetsSec(): string {
  if (SET.view === 'add') {
    const add = can('dataset.create');           // combining datasets they can already open needs no permission
    const tile = (act: string, cls: string, ico: string, title: string, text: string): string =>
      `<button class="tile" data-act="${act}"><span class="ds-icon${cls ? ` ${cls}` : ''}">${icon(ico)}</span><b>${title}</b><span>${text}</span></button>`;
    return '<section>' + (add ? '' : `<p class="sec-note">${esc(cannot('add new datasets'))}. You can still combine the ones you can open.</p>`) +
      '<div class="tiles">' + (add ? tile('new-s3', 's3', 'cloud', 'S3 / RustFS bucket',
        'A bucket prefix of images (labels optional), or a YOLO folder (images/, labels/). Stream it, or sync a local cache for the fastest browsing.') +
      tile('new-local', '', 'folder', 'Local folder',
        'A folder of images (labels optional) or a YOLO folder on this machine. Read in place — nothing is copied, so no extra disk is used.') +
      tile('new-labeling', 'labeling', 'tag', 'Labeling session',
        'One task of a labeling session on this server, read live: no export, no copy. Re-open it to see the latest labels.') +
      tile('new-embeddings', 'embeddings', 'vectors', 'Embeddings folder',
        'Vectors you computed anywhere (audio, video, text, tables) as .npy files, plus a meta.csv of labels, previews and columns.') : '') +
      tile('new-union', 'union', 'layers', 'Combined datasets',
        `2 to ${UNION_MAX} of your visualize sessions as one map, to compare them (what was added, removed, moved or relabelled) or to explore them together. Nothing is copied.`) +
      '</div><div class="form-foot"><button class="ghost" data-act="back">Cancel</button></div></section>';
  }
  if (SET.view === 's3') return renderS3Form();
  if (SET.view === 'local') return renderLocalForm();
  if (SET.view === 'labeling') return renderLabelingForm();
  if (SET.view === 'embeddings') return renderEmbeddingsForm();
  if (SET.view === 'union') return renderUnionForm();
  const cards = SET.datasets.map((d) => {
    const active = d.id === SET.active;
    const tags: string[] = [];
    if (d.kind === 's3') {
      tags.push(d.mode === 'cache' ? tagc('cached locally', 'on', 'download') : tagc('streams from S3', 'on', 'cloud'));
      if (d.mirror) tags.push(tagc(`${fmtBytes(d.mirror.bytes)} · synced ${timeAgo(d.mirror.synced)}`, 'ok', 'check'));
      else if (d.mode === 'cache') tags.push(tagc('not synced yet'));
      if (d.connection) tags.push(tagc(d.connection, '', 'plug'));
      if (d.error) tags.push(tagc(d.error, 'bad', 'alert'));
    } else if (d.kind === 'labeling') {
      tags.push(tagc('live from labeling', 'on', 'tag'));
      if (d.labeling && d.labeling.task) tags.push(tagc(`${d.labeling.project} › ${d.labeling.task}`));
      if (d.only_done) tags.push(tagc('finished images only', '', 'check'));
      if (d.error) tags.push(tagc(d.error, 'bad', 'alert'));
    } else if (d.kind === 'union') {
      tags.push(tagc('combined', 'on', 'layers'));
      for (const p of d.parts || []) tags.push(tagc(`${p.alias} · ${p.name || p.id}`));
    } else if (d.kind === 'embeddings') {
      tags.push(tagc('vectors + metadata', 'on', 'vectors'));
      for (const m of d.modality || []) tags.push(tagc(m, '', MODALITY_ICON[m] || 'vectors'));
      if (d.exists === false) tags.push(tagc('folder not found', 'bad', 'alert'));
    } else {
      tags.push(tagc('read in place', '', 'folder'));
      if (d.exists === false) tags.push(tagc('folder not found', 'bad', 'alert'));
    }
    return `<div class="ds-card${active ? ' active' : ''}" data-id="${esc(d.id)}">` +
      `<div class="ds-icon ${d.kind}">${icon(DS_ICON[d.kind])}</div>` +
      `<div class="ds-main"><div class="ds-title">${esc(d.name)}${active ? tagc('open now', 'on', 'check') : ''}</div>` +
      (d.kind === 'union' ? '' : `<div class="ds-uri" title="${esc(d.uri || '')}">${esc(d.uri || '')}</div>`) +
      `<div class="ds-meta">${tags.join('')}</div></div>` +
      '<div class="ds-actions">' +
      (active ? '' : `<button class="primary" data-act="open-ds">${icon('play')}Open</button>`) +
      (d.kind === 's3' && d.mode === 'cache' ? `<button data-act="sync-ds" title="Download new / changed files">${icon('refresh')}Sync</button>` : '') +
      `<button class="icon-btn ghost" data-act="edit-ds" aria-label="Edit" title="Edit">${icon('edit')}</button>` +
      // the open session is removed from the Sessions home (removing it here would pull the page from under you)
      (active ? '' : `<button class="icon-btn ghost" data-act="remove-ds" aria-label="Remove" title="Remove the session (files are not touched)">${icon('trash')}</button>`) +
      `</div><div class="ds-progress" id="dsp-${esc(d.id)}"></div></div>`;
  }).join('');
  return `<section><div class="sec-actions"><button class="primary" data-act="add">${icon('plus')}New session</button>` +
    '<span class="sec-note">Opening a session reloads the page; its crops, features and views are cached per dataset.</span></div>' +
    cards + '</section>';
}

/** The icon of a session's kind (cards, member rows). */
export const DS_ICON: Record<DatasetItem['kind'], string> = { s3: 'cloud', local: 'folder', labeling: 'tag', embeddings: 'vectors', union: 'layers' };

// ---- S3 dataset form: 1 connection · 2 bucket + prefix · 3 load mode
type ConnFields = ConnDraft & { access_key_masked?: string | undefined };

function connFieldsHTML(c: ConnFields, saved: boolean): string {
  const v = (k: keyof ConnFields) => esc(c[k] || '');
  return '<div class="form-grid">' +
    `<label class="fld">Name<input type="text" data-f="conn.name" value="${v('name')}" placeholder="e.g. lab RustFS"></label>` +
    `<label class="fld mono">Endpoint URL<input type="url" data-f="conn.endpoint" value="${v('endpoint')}" placeholder="http://rustfs.local:9000" required></label>` +
    `<label class="fld mono">Access key<input type="text" data-f="conn.access_key" value="${v('access_key')}" autocomplete="off" placeholder="${saved ? esc(c.access_key_masked || 'unchanged') : ''}"></label>` +
    `<label class="fld mono">Secret key<input type="password" data-f="conn.secret_key" value="${v('secret_key')}" autocomplete="new-password" placeholder="${saved && c.has_secret ? 'unchanged (stored on the server)' : ''}"></label>` +
    `<label class="fld mono">Region<input type="text" data-f="conn.region" value="${v('region')}" placeholder="us-east-1"><span class="help">RustFS / MinIO accept any region</span></label>` +
    `<label class="fld" style="justify-content:flex-end"><span class="check" style="margin:0"><input type="checkbox" data-f="conn.verify_ssl" ${c.verify_ssl === false ? '' : 'checked'}> Verify TLS certificate</span></label>` +
    '</div>';
}

export function statusHTML(st: StatusLine | null | undefined): string {
  if (!st) return '<div class="status-line"></div>';
  const ico = st.kind === 'busy' ? '<span class="spin"></span>' : st.kind === 'ok' ? icon('check') : st.kind === 'err' ? icon('alert') : '';
  return `<div class="status-line ${st.kind}">${ico}<span>${esc(st.text)}</span></div>`;
}

export function crumbsHTML(root: string, parts: string[], act: string): string {
  return `<div class="crumbs"><button data-act="${act}" data-to="">${esc(root)}</button>` +
    parts.map((p, i) => `<span class="sep">/</span><button data-act="${act}" data-to="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</button>`).join('') + '</div>';
}

function foldersHTML(list: FolderEntry[] | null | undefined, act: string, emptyText: string): string {
  if (!list) return '';
  if (!list.length) return `<div class="folders"><div class="none">${esc(emptyText)}</div></div>`;
  return '<div class="folders">' + list.map((f) =>
    `<button class="folder${f.yolo || f.embeddings ? ' yolo' : ''}" data-act="${act}" data-name="${esc(f.name)}">${icon('folder')}` +
    `<span class="name">${esc(f.name)}</span>${f.yolo ? tagc('YOLO', 'on', 'check') : ''}${f.embeddings ? tagc('vectors', 'on', 'vectors') : ''}` +
    (!f.yolo && f.images ? tagc(`${f.images.toLocaleString()}${f.images >= 1000 ? '+' : ''} image${f.images === 1 ? '' : 's'}`, '', 'image') : '') +
    `${icon('chev-r')}</button>`).join('') + '</div>';
}

function yoloSummary(info: YoloInfo): string {
  return `${info.images.toLocaleString()} images · ${info.labels.toLocaleString()} label files · ` +
    `${info.classes ? 'class names ✓' : 'no classes.txt / data.yaml (ids are shown)'}${info.bytes ? ` · ${fmtBytes(info.bytes)}` : ''}`;
}

/** Images in a browsed folder that is not a YOLO folder: [here, in its direct sub-folders]. */
function plainImages(b: BrowseResult | null | undefined): [number, number] {
  if (!b || b.yolo) return [0, 0];
  return [b.images || 0, (b.folders || []).reduce((n, f) => n + (f.images || 0), 0)];
}

/** The location step's status line: a YOLO summary, a count of plain images, or what to open. */
function locationStatus(b: BrowseResult | null | undefined): StatusLine | null {
  if (!b) return null;
  if (b.yolo) return { kind: 'ok', text: b.info ? yoloSummary(b.info) : 'YOLO folder' };
  const [here, below] = plainImages(b);
  if (here + below) {
    const parts = [here ? `${here.toLocaleString()} here` : '', below ? `${below.toLocaleString()}${below >= 1000 ? '+' : ''} in sub-folders` : ''];
    return { kind: 'ok', text: `Images found: ${parts.filter(Boolean).join(' · ')} (deeper folders are included too) · no labels` };
  }
  return { kind: '', text: 'No images here yet: open a folder of images (any depth) or a YOLO folder (images/ + labels/, marked YOLO).' };
}

/** The location is good to save: a YOLO folder, or a folder with images in it / one level down. */
function locationOk(b: BrowseResult | null | undefined): boolean {
  const [here, below] = plainImages(b);
  return !!(b && (b.yolo || here + below));
}

/** "What each point is": boxes or images for a YOLO folder; images (and the folder-classes switch) for plain images. */
function unitHTML(f: S3Form | LocalForm, b: BrowseResult | null | undefined): string {
  const yolo = !!(b && b.yolo);
  const unit = f.unit && f.unit !== 'auto' ? f.unit : yolo ? 'boxes' : 'images';
  const card = (v: string, ico: string, title: string, text: string, on: boolean, off = false) =>
    `<label class="radio-card${on ? ' sel' : ''}${off ? ' off' : ''}"><input type="radio" name="unit" value="${v}" data-f="unit" data-act-change="unit-pick" ` +
    `${on ? 'checked' : ''} ${off ? 'disabled' : ''}><div><b>${icon(ico)}${title}</b><p>${text}</p></div></label>`;
  const folders = !yolo && (b?.folders || []).some((x) => x.images);
  return '<div class="radio-cards">' +
    card('images', 'image', 'One point per image', 'Each whole image is one point on the map: find similar scenes, duplicates, outliers and odd ' +
      'cameras, even with no labels. Label them later in a label session.', unit === 'images') +
    card('boxes', 'box', 'One point per labelled box', yolo ? 'Each box of labels/ is one point (crops of the objects): label quality, curation and model scoring.'
      : 'Needs YOLO label files (images/ + labels/): none here.', unit === 'boxes', !yolo) + '</div>' +
    (unit === 'images' ? `<label class="check"><input type="checkbox" data-f="cff" data-act-change="unit-pick" ${f.cff ? 'checked' : ''} ${folders || f.cff ? '' : 'disabled'}>` +
      `Sub-folder names are the classes${folders ? ` <span class="muted">(${(b?.folders || []).filter((x) => x.images).map((x) => esc(x.name)).slice(0, 4).join(', ')}…)</span>` : ' <span class="muted">(no sub-folders with images here)</span>'}</label>` : '');
}

function renderS3Form(): string {
  const f = formOf('s3');
  const saved = f.connId !== 'new';
  const conn: ConnDraft = saved ? (SET.conns.find((c) => c.id === f.connId) ?? {}) : f.conn;
  const okConn = !!f.buckets;
  const b = f.browse;
  const okLoc = locationOk(b);
  const step = (n: number, title: string, done: boolean, locked: boolean, inner: string) =>
    `<div class="form-card${done ? ' done' : ''}${locked ? ' locked' : ''}"><div class="step-head"><span class="step-n">${done ? '✓' : n}</span>${title}</div>${inner}</div>`;
  const connOpts = SET.conns.map((c) => `<option value="${esc(c.id)}" ${c.id === f.connId ? 'selected' : ''}>${esc(c.name)} — ${esc(c.endpoint)}</option>`).join('') +
    `<option value="new" ${saved ? '' : 'selected'}>+ New connection…</option>`;
  const s1 = `<label class="fld">Connection<select data-f="connId" data-act-change="conn-pick">${connOpts}</select></label>` +
    (saved ? `<div class="sec-note mono">${esc(conn.endpoint || '')} · key ${esc(conn.access_key || '—')}${conn.has_secret ? ' · secret stored' : ''}</div>`
      : connFieldsHTML({ ...f.conn }, false)) +
    `<div class="sec-actions"><button data-act="test-conn">${icon('plug')}Test connection</button>${statusHTML(f.connStatus)}</div>`;
  const parts = (f.prefix || '').split('/').filter(Boolean);
  const s2 = '<div class="form-grid">' +
    `<label class="fld">Bucket<select data-f="bucket" data-act-change="bucket-pick">${(f.buckets || []).map((x) =>
      `<option ${x === f.bucket ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>` +
    `<label class="fld mono">Prefix (a folder of images, or a YOLO folder)<input type="text" data-f="prefix" value="${esc(f.prefix || '')}" placeholder="datasets/my-images" data-enter="go-prefix"></label></div>` +
    (f.bucket ? `<div class="browser">${crumbsHTML(f.bucket, parts, 's3-crumb')}${foldersHTML(b && b.folders, 's3-folder', 'no sub-folders here')}</div>` : '') +
    statusHTML(f.locStatus || locationStatus(b));
  const est = b && b.info && b.info.bytes ? ` (~${fmtBytes(b.info.bytes)} on disk)` : '';
  const s3 = '<div class="radio-cards">' +
    `<label class="radio-card${f.mode !== 'cache' ? ' sel' : ''}"><input type="radio" name="s3mode" value="stream" data-f="mode" data-act-change="mode-pick" ${f.mode !== 'cache' ? 'checked' : ''}><div><b>${icon('cloud')}Stream from S3</b>` +
    '<p>Always loads from the bucket. Images are downloaded in parallel while indexing, turned into small crops and dropped; the full image is fetched when you open it. No local copy.</p></div></label>' +
    `<label class="radio-card${f.mode === 'cache' ? ' sel' : ''}"><input type="radio" name="s3mode" value="cache" data-f="mode" data-act-change="mode-pick" ${f.mode === 'cache' ? 'checked' : ''}><div><b>${icon('download')}Sync &amp; cache locally</b>` +
    `<p>Mirrors the prefix to disk${esc(est)} with parallel downloads; later syncs only fetch new or changed files. Fastest browsing.</p></div></label></div>` +
    `<label class="fld">Display name<input type="text" data-f="name" value="${esc(f.name || '')}" placeholder="${esc(parts[parts.length - 1] ?? (f.bucket || 'dataset'))}"></label>`;
  return '<section>' +
    step(1, 'Connection', okConn, false, s1) +
    step(2, 'Bucket and prefix', okLoc, !okConn, s2) +
    step(3, 'What each point is', okLoc, !okLoc, unitHTML(f, b)) +
    step(4, 'How to load it', false, !okLoc, s3) +
    `<div class="form-foot">${statusHTML(f.saveStatus)}<span class="grow"></span><button class="ghost" data-act="back">Cancel</button>` +
    `<button data-act="save-s3" ${okLoc ? '' : 'disabled'}>Save</button>` +
    `<button class="primary" data-act="save-s3" data-open="1" ${okLoc ? '' : 'disabled'}>${icon('play')}Save &amp; open</button></div></section>`;
}

function renderLocalForm(): string {
  const f = formOf('local');
  const b = f.browse;
  const ok = locationOk(b);
  const parts = b ? b.path.split('/').filter(Boolean) : [];
  return '<section><div class="form-card done"><div class="step-head">' + icon('folder') + 'Folder on this machine: images, or a YOLO folder</div>' +
    `<div class="sec-actions"><input class="txt mono" style="flex:1" type="text" data-f="path" value="${esc(f.path || '')}" placeholder="/data/my-images" data-enter="go-path">` +
    `<button data-act="go-path">${icon('arrow-r')}Go</button>${b && b.parent ? `<button class="icon-btn" data-act="up-path" aria-label="Parent folder" title="Parent folder">${icon('up')}</button>` : ''}</div>` +
    (b ? `<div class="browser">${crumbsHTML('/', parts, 'local-crumb')}${foldersHTML(b.folders, 'local-folder', 'no sub-folders here')}</div>` : '') +
    statusHTML(f.status || locationStatus(b)) +
    '<p class="sec-note">Read in place: nothing is copied. Only the small crops, features and projections go to the workspace cache.</p></div>' +
    (ok ? `<div class="form-card"><div class="step-head">${icon('image')}What each point is</div>${unitHTML(f, b)}</div>` : '') +
    `<div class="form-card"><label class="fld">Display name<input type="text" data-f="name" value="${esc(f.name || '')}" placeholder="${esc(parts[parts.length - 1] ?? '')}"></label></div>` +
    `<div class="form-foot">${statusHTML(f.saveStatus)}<span class="grow"></span><button class="ghost" data-act="back">Cancel</button>` +
    `<button data-act="save-local" ${ok ? '' : 'disabled'}>Save</button>` +
    `<button class="primary" data-act="save-local" data-open="1" ${ok ? '' : 'disabled'}>${icon('play')}Save &amp; open</button></div></section>`;
}

// ---- embeddings folder: <name>.npy vector files + meta.csv (dataviz/data/embedding_source.py), read in place
export const MODALITY_ICON: Record<string, string> = { image: 'image', audio: 'wave', video: 'film', text: 'text', vector: 'vectors' };
const COL_TYPE: Record<string, string> = { num: 'number', cat: 'category', text: 'free text' };

// what the session will hold, before it is saved: the reader checks the folder exactly as the index will
function summaryHTML(s: EmbeddingsSummary): string {
  const prev = s.previews.length ? s.previews.map((p) => `<span class="emb-col prev">${icon(MODALITY_ICON[p] || 'vectors')}${esc(p)}</span>`).join('')
    : '<span class="muted">none: cards show the label and columns</span>';
  const cols = s.columns.length ? s.columns.map((c) => `<span class="emb-col ${c.type}" title="${esc(COL_TYPE[c.type] || c.type)}">` +
    `<i>${c.type === 'num' ? '#' : c.type === 'cat' ? 'A' : '¶'}</i>${esc(c.name)}</span>`).join('') : '<span class="muted">none</span>';
  return '<div class="emb-summary">' +
    `<div class="emb-figs"><span><b class="num">${s.n.toLocaleString()}</b>rows</span>` +
    `<span><b class="num">${s.features.length}</b>feature space${s.features.length === 1 ? '' : 's'}</span>` +
    `<span><b class="num">${s.label ? s.classes : 1}</b>${s.label ? 'classes' : 'class (no label column)'}</span></div>` +
    '<dl class="emb-dl">' +
    `<dt>Vectors</dt><dd>${s.features.map((f) => `<span class="emb-feat"><b>${esc(f.name)}</b><span class="num">${f.dim} dims</span></span>`).join('')}</dd>` +
    `<dt>Previews</dt><dd>${prev}${s.url_previews ? `<span class="muted">${s.url_previews.toLocaleString()} ` +
      'are web links: shown only when the server allows it (config embeddings.allow_url_previews)</span>' : ''}</dd>` +
    `<dt>Columns</dt><dd>${cols}</dd></dl></div>`;
}

function renderEmbeddingsForm(): string {
  const f = formOf('embeddings');
  const b = f.browse;
  const ok = !!f.summary;
  const parts = b ? b.path.split('/').filter(Boolean) : [];
  const hint: StatusLine | null = f.status || (b && !b.embeddings
    ? { kind: '', text: 'Open a folder that holds meta.csv (or meta.parquet) and one or more .npy files (marked vectors).' } : null);
  return '<section><div class="form-card done"><div class="step-head">' + icon('vectors') + 'Embeddings folder on this machine</div>' +
    `<div class="sec-actions"><input class="txt mono" style="flex:1" type="text" data-f="path" value="${esc(f.path || '')}" placeholder="/data/speech-embeddings" data-enter="emb-go">` +
    `<button data-act="emb-go">${icon('arrow-r')}Go</button>${b && b.parent ? `<button class="icon-btn" data-act="emb-up" aria-label="Parent folder" title="Parent folder">${icon('up')}</button>` : ''}</div>` +
    (b ? `<div class="browser">${crumbsHTML('/', parts, 'emb-crumb')}${foldersHTML(b.folders, 'emb-folder', 'no sub-folders here')}</div>` : '') +
    (f.summary ? summaryHTML(f.summary) : '') + statusHTML(hint) +
    '<details class="emb-format"><summary>Folder format</summary>' +
    '<pre class="mono">meta.csv      one row per sample: label, id, image | audio | video (paths in the folder), text, any other columns\n' +
    '&lt;name&gt;.npy    (rows × dims) float vectors, row i = meta row i; each file is one feature space</pre>' +
    '<p class="sec-note">Columns of numbers colour the plot on a scale; columns with up to 50 values colour it by category. ' +
    'Unit-length vectors are compared by cosine, others by Euclidean distance.</p></details>' +
    `<label class="fld">Display name<input type="text" data-f="name" value="${esc(f.name || '')}" placeholder="${esc(parts[parts.length - 1] ?? '')}"></label></div>` +
    `<div class="form-foot">${statusHTML(f.saveStatus)}<span class="grow"></span><button class="ghost" data-act="back">Cancel</button>` +
    `<button data-act="save-embeddings" ${ok ? '' : 'disabled'}>Save</button>` +
    `<button class="primary" data-act="save-embeddings" data-open="1" ${ok ? '' : 'disabled'}>${icon('play')}Save &amp; open</button></div></section>`;
}

// ---- labeling-backed dataset: a labeling session + one of its box tasks, read live (dataviz/data/labeling_source.py)
export const VIZ_TASKS = new Set(['object_detection', 'segmentation', 'keypoints']);
const TASK_NAMES: Record<string, string> = { object_detection: 'detection', segmentation: 'segmentation', keypoints: 'keypoints', classification: 'classification' };

function renderLabelingForm(): string {
  const f = formOf('labeling');
  const ps = f.projects;
  if (!ps) return `<section>${statusHTML(f.status || { kind: 'busy', text: 'loading the labeling sessions…' })}</section>`;
  if (!ps.length) {
    return '<section><div class="form-card done"><p class="sec-note">No labeling sessions on this server yet. Create one on the Sessions home ' +
      '(Label tab), draw some boxes, then come back to visualize it.</p></div>' +
      '<div class="form-foot"><span class="grow"></span><button class="ghost" data-act="back">Cancel</button></div></section>';
  }
  const p = ps.find((x) => x.id === f.project_id);
  const tasks = (p && p.tasks) || [];
  const t = tasks.find((x) => x.id === f.task_id);
  const ok = !!(p && t && VIZ_TASKS.has(t.task_type));
  const popts = (f.project_id && !p ? `<option value="${esc(f.project_id)}" selected disabled>(deleted labeling session)</option>` : '') +
    ps.map((x) => `<option value="${esc(x.id)}" ${x.id === f.project_id ? 'selected' : ''}>${esc(x.name)} — ${(x.n_media || 0).toLocaleString()} images</option>`).join('');
  const topts = tasks.map((x) => {
    const can = VIZ_TASKS.has(x.task_type);
    const kind = TASK_NAMES[x.task_type] || x.task_type;
    return `<option value="${esc(x.id)}" ${x.id === f.task_id ? 'selected' : ''} ${can ? '' : 'disabled'}>${esc(x.name)} (${esc(kind)}` +
      `${can ? `, ${(x.done || 0).toLocaleString()} finished` : ': no boxes to visualize'})</option>`;
  }).join('') || '<option value="" disabled selected>this session has no tasks</option>';
  const note = !p ? 'Pick a labeling session.' : !tasks.some((x) => VIZ_TASKS.has(x.task_type))
    ? 'This session has only classification tasks: there are no boxes to visualize.'
    : t && t.task_type !== 'object_detection' ? `Each ${t.task_type === 'segmentation' ? 'polygon' : 'keypoint instance'} becomes one box (its extent).` : '';
  return '<section><div class="form-card done"><div class="step-head">' + icon('tag') + 'Labeling session and task</div>' +
    '<div class="form-grid">' +
    `<label class="fld">Labeling session<select data-f="project_id" data-act-change="lds-project">${popts}</select></label>` +
    `<label class="fld">Task<select data-f="task_id" data-act-change="lds-task">${topts}</select></label></div>` +
    `<label class="check"><input type="checkbox" data-f="only_done" ${f.only_done ? 'checked' : ''}> <span><b>Only finished images</b><br>` +
    '<span class="muted">Only images committed as done in this task; otherwise every image with at least one shape.</span></span></label>' +
    (note ? `<p class="sec-note">${esc(note)}</p>` : '') +
    '<p class="sec-note">Read live: labels come straight from the labeling database and images from its cache, nothing is exported or copied. ' +
    'Edits made while labeling show up the next time the session is opened (the index is rebuilt for the new version). ' +
    'There are no train / val folders here, so the Model view scores every image as one split.</p>' +
    `<label class="fld">Display name<input type="text" data-f="name" value="${esc(f.name || '')}" placeholder="${esc(p && t ? `${p.name} · ${t.name}` : '')}"></label></div>` +
    `<div class="form-foot">${statusHTML(f.saveStatus)}<span class="grow"></span><button class="ghost" data-act="back">Cancel</button>` +
    `<button data-act="save-labeling" ${ok ? '' : 'disabled'}>Save</button>` +
    `<button class="primary" data-act="save-labeling" data-open="1" ${ok ? '' : 'disabled'}>${icon('play')}Save &amp; open</button></div></section>`;
}

// ---- combined dataset (kind 'union', data/union.py): 2–8 visualize sessions in priority order, each with an alias.
// The order is the priority (the first member's class order and image wins); the alias prefixes its class names (A:cat).
// The Sessions home builds one from 2+ selected cards (Compare / Combine); this form edits it, or builds one by hand.
export const UNION_MAX = 8;
const ALIAS_RE = /^[A-Za-z0-9_-]{1,24}$/;                 // = data/union.py ALIAS_RE
export const defaultAlias = (i: number): string => (i < 26 ? String.fromCharCode(65 + i) : `M${i + 1}`);
/** The name the server gives a combined dataset with no name of its own: 'A vs B', or 'A + B + C'. */
export const unionName = (names: string[]): string => names.join(names.length === 2 ? ' vs ' : ' + ');

const KIND_TEXT: Record<DatasetItem['kind'], string> = { s3: 'S3', local: 'local folder', labeling: 'labeling task', embeddings: 'embeddings', union: 'combined' };

/** Why a session cannot be a member of a combined dataset ('' = it can). */
export function unionBlock(d: Pick<DatasetItem, 'kind'>): string {
  if (d.kind === 'union') return 'A combined dataset cannot be combined again: edit its members instead';
  if (d.kind === 'embeddings') return 'An embeddings session has no images or boxes to compare or combine';
  return '';
}

/** What is wrong with the alias of member i ('' = nothing). */
function aliasProblem(parts: UnionPart[], i: number): string {
  const a = (parts[i]?.alias ?? '').trim();
  if (!a) return 'needs an alias';
  if (!ALIAS_RE.test(a)) return '1 to 24 letters, digits, - or _';
  if (parts.some((p, k) => k !== i && p.alias.trim().toLowerCase() === a.toLowerCase())) return 'used twice';
  return '';
}

/** The first problem that stops a save ('' = ready). */
function unionProblem(f: UnionForm): string {
  if (f.parts.length < 2) return `Add at least 2 sessions (up to ${UNION_MAX}).`;
  const bad = f.parts.findIndex((_, i) => aliasProblem(f.parts, i));
  return bad < 0 ? '' : `Alias of “${f.parts[bad]?.name || f.parts[bad]?.id}”: ${aliasProblem(f.parts, bad)}.`;
}

/** The sessions that may still be added: mine to see, not a union / embeddings, not this one, not already in. */
export function unionCandidates(f: UnionForm): DatasetItem[] {
  const have = new Set(f.parts.map((p) => p.id));
  return SET.datasets.filter((d) => !unionBlock(d) && d.id !== f.id && !have.has(d.id));
}

function unionRowHTML(f: UnionForm, p: UnionPart, i: number): string {
  const d = SET.datasets.find((x) => x.id === p.id);
  const kind = d?.kind ?? p.kind;
  const name = d?.name || p.name || p.id;
  const err = aliasProblem(f.parts, i);
  const btn = (act: string, ico: string, label: string, off: boolean, cls = '') =>
    `<button class="icon-btn ghost ${cls}" data-act="${act}" aria-label="${esc(label)}" title="${esc(label)}" ${off ? 'disabled' : ''}>${icon(ico)}</button>`;
  return `<li class="un-row${err ? ' bad' : ''}" data-i="${i}"><span class="un-n num" title="Priority ${i + 1}">${i + 1}</span>` +
    `<input class="txt mono un-alias" type="text" data-f="parts.${i}.alias" value="${esc(p.alias)}" maxlength="24" spellcheck="false" ` +
    `autocomplete="off" aria-label="Alias of ${esc(name)}" aria-invalid="${!!err}" aria-describedby="un-err-${i}">` +
    `<span class="un-name">${icon(kind ? DS_ICON[kind] : 'folder')}<b title="${esc(name)}">${esc(name)}</b>` +
    `<span class="muted">${esc(kind ? KIND_TEXT[kind] : d ? '' : 'not visible to you')}</span></span>` +
    btn('un-up', 'up', `Move ${name} up`, i === 0) + btn('un-down', 'up', `Move ${name} down`, i === f.parts.length - 1, 'un-flip') +
    btn('un-rm', 'x', `Remove ${name} from the list`, false, 'danger') +
    `<span class="un-err" id="un-err-${i}" role="alert">${esc(err)}</span></li>`;
}

function renderUnionForm(): string {
  const f = formOf('union');
  const cand = unionCandidates(f);
  const full = f.parts.length >= UNION_MAX;
  const problem = unionProblem(f);
  const names = f.parts.map((p) => SET.datasets.find((x) => x.id === p.id)?.name || p.name || p.id);
  const opts = cand.length ? '<option value="">Pick a session…</option>' + cand.map((d) =>
    `<option value="${esc(d.id)}" ${d.id === f.pick ? 'selected' : ''}>${esc(d.name)} — ${esc(KIND_TEXT[d.kind])}</option>`).join('')
    : '<option value="" disabled selected>no other session to add</option>';
  return '<section><div class="form-card done"><div class="step-head">' + icon('layers') + 'Sessions, in priority order</div>' +
    (f.parts.length ? `<ol class="un-list">${f.parts.map((p, i) => unionRowHTML(f, p, i)).join('')}</ol>`
      : '<p class="sec-note">No sessions yet: add 2 or more below.</p>') +
    `<div class="sec-actions un-add"><select data-f="pick" aria-label="Session to add" ${cand.length && !full ? '' : 'disabled'}>${opts}</select>` +
    `<button data-act="un-add" ${cand.length && !full ? '' : 'disabled'}>${icon('plus')}Add</button>` +
    `<span class="sec-note">${full ? `That is the limit: ${UNION_MAX} sessions.` : `${f.parts.length} of 2–${UNION_MAX}`}</span></div>` +
    '<p class="sec-note">The first session wins where two disagree (its class order, its copy of an image). ' +
    'The alias goes in front of each class name (A:cat), so the members stay apart on the map. ' +
    'Combined datasets and embeddings sessions cannot be members. Nothing is copied: the members’ own crops and features are reused.</p>' +
    `<label class="fld">Display name<input type="text" data-f="name" value="${esc(f.name || '')}" placeholder="${esc(unionName(names) || 'combined')}"></label></div>` +
    `<div class="form-foot"><div id="un-problem">${statusHTML(f.saveStatus || (problem && f.parts.length ? { kind: '', text: problem } : null))}</div>` +
    '<span class="grow"></span><button class="ghost" data-act="back">Cancel</button>' +
    `<button data-act="save-union" ${problem ? 'disabled' : ''}>Save</button>` +
    `<button class="primary" data-act="save-union" data-open="1" ${problem ? 'disabled' : ''}>${icon('play')}Save &amp; open</button></div></section>`;
}

/** After an alias was typed (no re-render, the focus stays): each row's message, the save buttons, the footer line. */
export function unionInput(): void {
  const f = SET.form;
  if (!f || f.form !== 'union') return;
  f.saveStatus = null;
  document.querySelectorAll<HTMLElement>('#settings-body .un-row').forEach((row) => {
    const i = Number(row.dataset['i']);
    const err = aliasProblem(f.parts, i);
    row.classList.toggle('bad', !!err);
    row.querySelector('.un-alias')?.setAttribute('aria-invalid', String(!!err));
    const out = row.querySelector('.un-err');
    if (out) out.textContent = err;
  });
  const problem = unionProblem(f);
  document.querySelectorAll<HTMLElement>('#settings-body [data-act="save-union"]').forEach((b) => setDisabled(b, !!problem));
  const line = document.getElementById('un-problem');
  if (line) line.innerHTML = statusHTML(problem ? { kind: '', text: problem } : null);
}

// ---- connections: one kind each (dataviz/labeling/README.md): s3 (the default, datasets + labeling images),
// ftp / sftp (labeling images and exports), postgres / mssql (labeling exports)
export const CONN_KINDS: Record<ConnKind, { label: string; icon: string; port?: number; note: string }> = {
  s3: { label: 'S3 / RustFS', icon: 'cloud', note: 'Buckets for datasets, labeling images and exports' },
  ftp: { label: 'FTP', icon: 'folder', port: 21, note: 'Labeling images and exports over FTP / FTPS' },
  sftp: { label: 'SFTP', icon: 'folder', port: 22, note: 'Labeling images and exports over SSH' },
  postgres: { label: 'PostgreSQL', icon: 'db', port: 5432, note: 'Labeling exports, upserted per task' },
  mssql: { label: 'SQL Server', icon: 'db', port: 1433, note: 'Labeling exports, merged per task' },
  model: { label: 'Model', icon: 'robot', note: 'Your model server: labels images for you (YOLO serving or the generic API)' },
};

export const connKind = (c: ConnDraft | null | undefined): ConnKind => (c && c.kind) || 's3';

// a blank form of that kind (secrets always start empty: blank on save = keep the stored one)
export function connDefaults(kind: string): ConnDraft {
  if (kind === 'ftp') return { kind, name: '', host: '', port: 21, username: '', secret_key: '', tls: false, passive: true, base_path: '/' };
  if (kind === 'sftp') return { kind, name: '', host: '', port: 22, username: '', secret_key: '', private_key: '', base_path: '/' };
  if (kind === 'model') return { kind, name: '', url: '', api_key: '', timeout_s: 60 };
  if (kind === 'postgres' || kind === 'mssql') {
    return { kind, name: '', host: '', port: CONN_KINDS[kind].port, database: '', username: '', secret_key: '',
      schema: kind === 'postgres' ? 'public' : 'dbo', table_prefix: 'lbl_', encrypt: kind === 'mssql' };
  }
  return { kind: 's3', endpoint: '', region: '', verify_ssl: true };
}

const SECRET_KEYS = new Set(['secret_key', 'private_key', 'api_key']);

// the editable copy of a saved connection (masked secrets are left out: blank = keep)
export function connEditable(c: ConnItem): ConnDraft {
  const kind = connKind(c);
  if (kind === 's3') return { id: c.id, kind, name: c.name, endpoint: c.endpoint, region: c.region, verify_ssl: c.verify_ssl };
  const o: ConnDraft = { ...connDefaults(kind), id: c.id };
  const dst: Record<string, unknown> = o, src: Record<string, unknown> = c;       // copy field by field, by name
  for (const k of Object.keys(o)) if (!SECRET_KEYS.has(k) && src[k] != null) dst[k] = src[k];
  return o;
}

export function connUri(c: ConnDraft): string {
  const kind = connKind(c);
  if (kind === 's3') return c.endpoint || '';
  if (kind === 'model') return c.url || '';
  const host = `${c.host || '?'}${c.port ? `:${c.port}` : ''}`;
  if (kind === 'ftp' || kind === 'sftp') return `${kind}://${c.username ? `${c.username}@` : ''}${host}${c.base_path || '/'}`;
  return `${kind}://${host}/${c.database || ''}${c.schema ? ` · ${c.schema}` : ''}`;
}

// what a test answer says, for every kind (s3: buckets, ftp/sftp: the base path listing, db: version + tables)
export function connTestText(r: ConnTestResult): string {
  const out = r.ms != null ? [`connected in ${r.ms} ms`] : ['connected'];
  if (r.protocol) {                                    // model: the detected API and its models
    out.push(r.protocol === 'generic' ? 'generic API (dataviz-label/1)' : r.protocol === 'serving' ? 'YOLO serving API' : String(r.protocol));
    const ms = r.models || [];
    const TT: Record<string, string> = { object_detection: 'detect', segmentation: 'segment', keypoints: 'keypoints', classification: 'classify' };
    out.push(ms.length ? `${ms.length} model${ms.length === 1 ? '' : 's'}: ${ms.slice(0, 4).map((m) => `${m.name || m.id} (${(m.task_type && TT[m.task_type]) || m.task_type}, ` +
      `${(m.classes || []).length} classes)`).join(', ')}${ms.length > 4 ? ', …' : ''}` : 'no models listed');
  }
  const names = (l: Array<string | { name: string }>) => l.map((x) => (typeof x === 'string' ? x : x.name)).slice(0, 6).join(', ') + (l.length > 6 ? ', …' : '');
  if (r.buckets) out.push(r.buckets.length ? `buckets: ${names(r.buckets)}` : 'no buckets');
  if (r.folders != null) {                             // ftp / sftp: a count (or a list) of the folders under the base path
    const n = Array.isArray(r.folders) ? r.folders.length : Number(r.folders);
    out.push(`${r.path ? `${r.path}: ` : ''}${n} folder${n === 1 ? '' : 's'}${Array.isArray(r.folders) && n ? ` (${names(r.folders)})` : ''}`);
  }
  if (r.images != null) out.push(`${Number(r.images).toLocaleString()} images here`);
  if (r.server_version) out.push(r.server_version);
  if (r.tables_exist != null) out.push(r.tables_exist ? `labeling tables exist${r.schema_version ? ` (schema v${r.schema_version})` : ''}` : 'no labeling tables yet (created on the first export)');
  return out.join(' · ');
}

type FieldSpec = [key: keyof ConnDraft & string, label: string, type: 'text' | 'int' | 'password' | 'textarea' | 'check', cls?: string, ph?: string];
const KIND_FIELDS: Record<'ftp' | 'sftp' | 'model' | 'db', FieldSpec[]> = {
  ftp: [['name', 'Name', 'text', '', 'e.g. lab NAS'], ['host', 'Host', 'text', 'mono', 'ftp.example.org'], ['port', 'Port', 'int', 'mono', '21'],
    ['username', 'User', 'text', 'mono', 'anonymous'], ['secret_key', 'Password', 'password', 'mono'], ['base_path', 'Base path', 'text', 'mono', '/'],
    ['tls', 'Use TLS (FTPS)', 'check'], ['passive', 'Passive mode (behind NAT / firewalls)', 'check']],
  sftp: [['name', 'Name', 'text', '', 'e.g. GPU box'], ['host', 'Host', 'text', 'mono', 'server.example.org'], ['port', 'Port', 'int', 'mono', '22'],
    ['username', 'User', 'text', 'mono'], ['secret_key', 'Password', 'password', 'mono'], ['base_path', 'Base path', 'text', 'mono', '/'],
    ['private_key', 'Private key (optional, PEM / OpenSSH; used instead of the password)', 'textarea', 'mono full']],
  model: [['name', 'Name', 'text', '', 'e.g. GPU box detector'], ['url', 'Server URL', 'text', 'mono', 'http://gpu-box:8000'],
    ['api_key', 'API key (optional, sent as Authorization: Bearer)', 'password', 'mono'], ['timeout_s', 'Timeout (s)', 'int', 'mono', '60']],
  db: [['name', 'Name', 'text', '', 'e.g. annotations DB'], ['host', 'Host', 'text', 'mono', 'db.example.org'], ['port', 'Port', 'int', 'mono'],
    ['database', 'Database', 'text', 'mono'], ['username', 'User', 'text', 'mono'], ['secret_key', 'Password', 'password', 'mono'],
    ['schema', 'Schema', 'text', 'mono'], ['table_prefix', 'Table prefix', 'text', 'mono', 'lbl_'], ['encrypt', 'Encrypt the connection (TLS)', 'check']],
};

// the form fields of any kind; `saved` = the stored connection (its masked secrets become placeholders)
export function connFormHTML(c: ConnDraft, saved?: ConnItem | null): string {
  const kind = connKind(c);
  if (kind === 's3') return connFieldsHTML({ ...c, access_key_masked: saved?.access_key, has_secret: saved?.has_secret }, !!saved);
  const fields = KIND_FIELDS[kind === 'postgres' || kind === 'mssql' ? 'db' : kind];
  const v = (k: keyof ConnDraft) => esc(c[k] == null ? '' : c[k]);
  return '<div class="form-grid">' + fields.map(([k, label, type, cls = '', ph = '']) => {
    if (type === 'check') {
      return `<label class="fld"><span class="check" style="margin:0"><input type="checkbox" data-f="conn.${k}" ${c[k] ? 'checked' : ''}> ${esc(label)}</span></label>`;
    }
    const stored = saved && ((k === 'secret_key' && saved.has_secret) || (k === 'private_key' && saved.has_private_key) ||
      (k === 'api_key' && (saved.has_api_key || saved.has_secret)));
    const place = esc(stored ? 'unchanged (stored on the server)' : k === 'port' ? String(CONN_KINDS[kind].port ?? '') : ph);
    const ctl = type === 'textarea'
      ? `<textarea rows="3" data-f="conn.${k}" placeholder="${place}" spellcheck="false">${v(k)}</textarea>`
      : `<input type="${type === 'password' ? 'password' : 'text'}" ${type === 'int' ? 'inputmode="numeric" data-type="int"' : ''} data-f="conn.${k}" ` +
        `value="${v(k)}" placeholder="${place}" ${type === 'password' ? 'autocomplete="new-password"' : 'autocomplete="off"'} spellcheck="false">`;
    return `<label class="fld ${cls}">${esc(label)}${ctl}</label>`;
  }).join('') + '</div>';
}

export function renderConnsSec(): string {
  if (SET.view === 'conn') {
    const f = formOf('conn');
    const saved = SET.conns.find((c) => c.id === f.conn.id);
    const kind = connKind(f.conn);
    // the kind is picked when adding; a saved connection keeps its kind
    const kinds = saved ? '' : '<div class="kind-pick" role="radiogroup" aria-label="Connection kind">' + Object.entries(CONN_KINDS).map(([k, d]) =>
      `<button role="radio" aria-checked="${k === kind}" class="kind-opt${k === kind ? ' sel' : ''}" data-act="conn-kind" data-kind="${k}">` +
      `${icon(d.icon)}<b>${esc(d.label)}</b><span>${esc(d.note)}</span></button>`).join('') + '</div>';
    return '<section><div class="form-card done"><div class="step-head">' + icon('plug') +
      (saved ? `Edit ${esc(CONN_KINDS[kind].label)} connection` : 'New connection') + '</div>' + kinds +
      connFormHTML(f.conn, saved) +
      `<div class="sec-actions"><button data-act="test-conn">${icon('plug')}Test connection</button>${statusHTML(f.connStatus)}</div></div>` +
      `<div class="form-foot"><span class="grow"></span><button class="ghost" data-act="back">Cancel</button>` +
      `<button class="primary" data-act="save-conn">${icon('check')}Save connection</button></div></section>`;
  }
  const cards = SET.conns.map((c) => {
    const kind = connKind(c);
    const tags = [tagc(CONN_KINDS[kind] ? CONN_KINDS[kind].label : kind, 'on')];
    if (kind === 's3') {
      const used = SET.datasets.filter((d) => d.connection_id === c.id).length;
      tags.push(tagc(`key ${c.access_key || '—'}`), c.has_secret ? tagc('secret stored', 'ok', 'check') : tagc('no secret', 'bad'),
        tagc(`${used} dataset${used === 1 ? '' : 's'}`));
      if (!c.verify_ssl) tags.push(tagc('TLS not verified', 'bad'));
    } else if (kind === 'model') {
      tags.push(c.has_api_key || c.has_secret ? tagc('API key stored', 'ok', 'check') : tagc('no API key'));
      if (c.timeout_s) tags.push(tagc(`timeout ${c.timeout_s} s`));
    } else {
      if (c.username) tags.push(tagc(`user ${c.username}`));
      tags.push(c.has_secret || c.has_private_key ? tagc(c.has_private_key ? 'key stored' : 'password stored', 'ok', 'check') : tagc('no password'));
      if (kind === 'ftp' && c.tls) tags.push(tagc('TLS', 'ok'));
      if ((kind === 'postgres' || kind === 'mssql') && c.table_prefix) tags.push(tagc(`tables ${c.table_prefix}*`));
    }
    return `<div class="ds-card" data-cid="${esc(c.id)}"><div class="ds-icon ${kind === 's3' ? 's3' : `k-${esc(kind)}`}">${icon((CONN_KINDS[kind] || CONN_KINDS.s3).icon)}</div>` +
      `<div class="ds-main"><div class="ds-title">${esc(c.name || connUri(c))}</div><div class="ds-uri">${esc(connUri(c))}</div>` +
      `<div class="ds-meta">${tags.join('')}</div></div>` +
      `<div class="ds-actions"><button data-act="test-saved">${icon('plug')}Test</button>` +
      `<button class="icon-btn ghost" data-act="edit-conn" aria-label="Edit" title="Edit">${icon('edit')}</button>` +
      `<button class="icon-btn ghost" data-act="remove-conn" aria-label="Delete" title="Delete">${icon('trash')}</button></div>` +
      `<div class="ds-progress" id="cst-${esc(c.id)}"></div></div>`;
  }).join('') || '<p class="sec-note">No connections yet. Add one here, while adding an S3 dataset, or while creating a labeling session.</p>';
  const add = can('connection.add') ? `<button class="primary" data-act="new-conn">${icon('plus')}New connection</button>`
    : `<span class="sec-note">${esc(cannot('add connections'))}</span>`;
  return `<section><div class="sec-actions">${add}</div>${cards}</section>`;
}

// ---- appearance / plot / workspace
export function renderAppearanceSec(): string {
  const look = lookName();
  const mode = themeMode();
  const styleChoice = (l: string, label: string, ico: string) => `<button class="theme-choice" data-act="look" data-look="${l}" aria-pressed="${look === l}">` +
    `<span class="swatch ${l}"><i></i></span><span>${icon(ico)}${label}</span></button>`;
  const choice = (m: string, label: string, ico: string) => `<button class="theme-choice" data-act="theme" data-mode="${m}" aria-pressed="${mode === m}">` +
    `<span class="swatch ${m}"><i></i></span><span>${icon(ico)}${label}</span></button>`;
  const pal = paletteName();
  const palChoice = (p: PaletteSpec) => {
    const [bg, sf, ac] = p[THEME.name] || [];
    const sw = p.id === 'default' ? '<span class="swatch default"><i></i></span>'
      : `<span class="swatch pal" style="--sw-bg:${bg};--sw-sf:${sf};--sw-ac:${ac}"><i></i></span>`;
    return `<button class="theme-choice" data-act="palette" data-palette="${p.id}" aria-pressed="${pal === p.id}">${sw}<span>${esc(p.label)}</span></button>`;
  };
  const palGroups = [...new Set(PALETTES.map((p) => p.group))].map((g) =>
    `<h4 class="sec-sub2">${g}</h4><div class="theme-choices">${PALETTES.filter((p) => p.group === g).map(palChoice).join('')}</div>`).join('');
  return '<section><h3 class="sec-sub">Style</h3><div class="theme-choices">' + styleChoice('default', 'Instrument', 'monitor') +
    styleChoice('editorial', 'Editorial', 'edit') + styleChoice('blueprint', 'Blueprint', 'layers') +
    styleChoice('brutalist', 'Brutalist', 'box') + styleChoice('terminal', 'Terminal', 'server') + styleChoice('soft', 'Soft', 'circle') + '</div>' +
    '<h3 class="sec-sub">Mode</h3><div class="theme-choices">' + choice('system', 'System', 'monitor') + choice('light', 'Light', 'sun') +
    choice('dark', 'Dark', 'moon') + '</div><h3 class="sec-sub">Palette</h3>' + palGroups +
    '<p class="sec-note">Colour-blind safe and High contrast also replace the class and error colours with the Okabe-Ito set (no red-green pairs). ' +
    'Class colours switch to a darker set in the light theme ' +
    '(config.yaml <code>ui.palette_light</code>) so every class stays readable on the white plot.</p></section>';
}

export function renderPlotSec(): string {
  return '<section><div class="form-card done">' +
    `<label class="check"><input type="checkbox" data-act-change="dist-toggle" ${$req<HTMLInputElement>('show-dist').checked ? 'checked' : ''}> ` +
    '<span><b>Distance lines between picked thumbnails</b><br><span class="muted">Ctrl+click 2 or more thumbnails: each pair of dots ' +
    `is joined by a line labelled with their distance in the current feature space (cosine distance; up to ${MAX_DIST_PICKS} picks).</span></span></label></div>` +
    '<div class="form-card"><div class="step-head">Mouse on the plot</div><dl class="kv">' +
    '<dt>left-drag</dt><dd>draw a lasso (selects on release)</dd><dt>middle-drag</dt><dd>move the lasso, or pan when there is none</dd>' +
    '<dt>right-drag</dt><dd>pan</dd><dt>wheel</dt><dd>mouse: zoom around the cursor · trackpad: two-finger pan, pinch to zoom</dd><dt>Esc</dt><dd>unpin thumbnails, then clear the lasso</dd>' +
    '</dl></div></section>';
}

export function renderWorkspaceSec(): string {
  return '<section><div class="form-card done"><dl id="ws-kv" class="kv"><dt>loading…</dt><dd></dd></dl></div></section>';
}

/** GET api/workspace */
interface WorkspaceInfo {
  name: string; path: string; created?: string | null; size_mb: number; n_features: number; n_projections: number;
  others: Array<{ name: string }>;
}

export async function fillWorkspaceSec(): Promise<void> {
  try {
    const w = await getJSON<WorkspaceInfo>('api/workspace');
    const el = document.getElementById('ws-kv');
    if (!el) return;
    const rows: Array<[string, string | number]> = [['folder', w.path], ['created', w.created || '?'], ['size on disk', `${w.size_mb} MB`],
      ['feature spaces', w.n_features], ['saved views', w.n_projections],
      ['other versions', w.others.length ? w.others.map((o) => o.name).join(', ') : 'none']];
    el.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
  } catch (e) { /* dataset still opening */ }
}
