// The Data Center's one mutable state object `D` (buckets, where I am, the listing, the selection, view choices),
// the format table (label + colour token per detected format), small formatters, and `hooks`: the few app-level
// actions (navigate, reload, render) that app.ts fills in, so the pane modules can call them without importing app.ts.

import { esc, icon } from '../core/dom.js';
import type { Bucket, Entry, FolderStats, Format, Listing, LsSort, Retag, SearchResult, Summary, TagCount } from '../types-datacenter.js';

export type ViewMode = 'thumbs' | 'names';
export type CardMode = 'grid' | 'list';
export type BucketSort = 'recent' | 'name' | 'size' | 'images';

export interface DCState {
  buckets: Bucket[];
  summary: Summary | null;
  tags: TagCount[];
  loadErr: string | null;
  /** current bucket id (null = All buckets) and folder path inside it ('' = its root) */
  bid: string | null;
  path: string;
  /** the open folder's listing (entries grow page by page), its loading state and error */
  listing: Listing | null;
  listErr: string | null;
  loading: boolean;
  /** listing request sequence: a stale answer is dropped */
  seq: number;
  /** selected entry paths (in the open folder) and the anchor of a Shift range */
  sel: Set<string>;
  anchor: string | null;
  view: ViewMode;
  cards: CardMode;
  sort: LsSort;
  bucketSort: BucketSort;
  /** All buckets: name filter, tag filter (also the tree's tag chips), format chip */
  q: string;
  tagFilter: Set<string>;
  fmtFilter: string | null;
  /** folder name filter (ls ?q=) */
  nameQ: string;
  /** the bucket whose card is focused on the All buckets page (its details on the right) */
  focus: string | null;
  /** tree: expanded nodes ("bid\u0000path") and their loaded children */
  expanded: Set<string>;
  children: Map<string, Entry[] | 'loading' | 'error'>;
  /** caches: folder stats per "bid\u0000path"; sample thumbnails per bucket */
  stats: Map<string, FolderStats | 'loading' | 'error'>;
  samples: Map<string, string[] | 'loading'>;
  /** the last format change per bucket (img5 banner), with the undo id that reverts it */
  retag: Map<string, Retag & { undo_id: string | null }>;
  /** header search results (shown instead of the folder / overview) */
  search: { q: string; res: SearchResult | null; err: string | null } | null;
  /** narrow screens: details overlay / tree overlay open */
  detailsOpen: boolean;
  treeOpen: boolean;
}

export const D: DCState = {
  buckets: [], summary: null, tags: [], loadErr: null,
  bid: null, path: '', listing: null, listErr: null, loading: false, seq: 0,
  sel: new Set(), anchor: null,
  view: 'thumbs', cards: 'grid', sort: 'name', bucketSort: 'recent',
  q: '', tagFilter: new Set(), fmtFilter: null, nameQ: '', focus: null,
  expanded: new Set(), children: new Map(), stats: new Map(), samples: new Map(), retag: new Map(),
  search: null, detailsOpen: false, treeOpen: false,
};

/** App-level actions, set by app.ts start(). */
export const hooks = {
  navigate: (_bid: string | null, _path?: string, _push?: boolean): void => {},
  /** reload buckets / summary / tags and the open listing (after an op, an upload, a tag change…) */
  reload: (): Promise<void> => Promise.resolve(),
  /** repaint every pane from D (no request) */
  render: (): void => {},
  /** repaint only the selection-dependent parts (classes, selbar, details) */
  selChanged: (): void => {},
};

export const key = (bid: string, path: string): string => `${bid}\u0000${path}`;
export const bucketOf = (bid: string | null): Bucket | undefined => (bid ? D.buckets.find((b) => b.id === bid) : undefined);
export const curBucket = (): Bucket | undefined => bucketOf(D.bid);
export const joinPath = (dir: string, name: string): string => (dir ? `${dir.replace(/\/$/, '')}/${name}` : name);
export const parentOf = (p: string): string => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
export const baseName = (p: string): string => p.slice(p.lastIndexOf('/') + 1);

// ---- formats: label + colour token (the tree dot, badges, chips)
export const FORMATS: ReadonlyArray<[string, string, string]> = [
  ['yolo-detect', 'YOLO detect', 'var(--accent)'], ['yolo-seg', 'YOLO seg', 'var(--err-tp)'],
  ['yolo-pose', 'YOLO pose', 'var(--err-cls)'], ['yolo-obb', 'YOLO OBB', 'var(--err-loc)'],
  ['coco', 'COCO', 'var(--prog-review)'], ['voc', 'Pascal VOC', 'var(--highlight)'],
  ['classification', 'Class folders', 'var(--err-fp)'], ['images', 'Images only', 'var(--dot-ungrouped)'],
  ['embeddings', 'Embeddings', 'var(--gem-1)'], ['other', 'Other', 'var(--text-muted)'],
];
const FMT = new Map(FORMATS.map(([k, l, c]) => [k, { label: l, color: c }]));
export const fmtKey = (f: Format | null | undefined): string => (f ? String(f.format) : 'empty');
export const fmtLabel = (f: Format | string | null | undefined): string => {
  if (!f) return 'Empty';
  if (typeof f === 'string') return FMT.get(f)?.label ?? (f === 'empty' ? 'Empty' : f);
  return f.label || FMT.get(String(f.format))?.label || String(f.format);
};
export const fmtColor = (k: string): string => FMT.get(k)?.color ?? 'var(--text-faint)';
const hollow = (k: string): boolean => k === 'other' || k === 'empty';
export const isYolo = (f: Format | null | undefined): boolean => !!f && String(f.format).startsWith('yolo');

/** The coloured format dot. */
export const fdot = (k: string): string => `<i class="dc-fdot${hollow(k) ? ' hollow' : ''}" style="--fc:${fmtColor(k)}" aria-hidden="true"></i>`;
/** The format badge (dot + label, optional confidence); `lg` for the details panel. */
export function fbadge(f: Format | string | null | undefined, opts: { conf?: boolean; lg?: boolean } = {}): string {
  const k = typeof f === 'string' ? f : fmtKey(f);
  const conf = opts.conf && f && typeof f !== 'string' && f.confidence != null ? `<span class="conf">${Math.round(f.confidence * 100)}%</span>` : '';
  return `<span class="dc-fb${hollow(k) ? ' hollow' : ''}${opts.lg ? ' lg' : ''}" style="--fc:${fmtColor(k)}">${fdot(k)}${esc(fmtLabel(f))}${conf}</span>`;
}

export const SOURCE: Record<string, [string, string]> = { managed: ['db', 'Managed'], local: ['link', 'Local link'], s3: ['cloud', 'S3 link'] };
export const srcIcon = (b: Bucket): string => icon(SOURCE[b.kind]?.[0] ?? 'db');

// ---- numbers
export const fmtN = (n: number | null | undefined): string => (n == null || !Number.isFinite(n) ? '—' : Math.round(n).toLocaleString('en-US'));
/** 12480 → 12.4k, 48000 → 48k */
export function compact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '';
  if (n < 1000) return String(n);
  if (n < 100000) return `${(Math.floor(n / 100) / 10).toString()}k`;
  if (n < 1e6) return `${Math.round(n / 1000)}k`;
  return `${(Math.floor(n / 1e5) / 10).toString()}M`;
}
export function fmtSize(b: number | null | undefined): string {
  if (b == null || !Number.isFinite(b)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = b, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v >= 100 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}
const toMs = (t: number | string | null | undefined): number => {
  if (t == null || t === '') return NaN;
  if (typeof t === 'number') return t < 1e12 ? t * 1000 : t;
  return Date.parse(String(t).replace(' ', 'T'));
};
export function timeAgo(t: number | string | null | undefined): string {
  const ms = toMs(t);
  if (!Number.isFinite(ms)) return '';
  const s = (Date.now() - ms) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return 'yesterday';
  return new Date(ms).toLocaleDateString('en-US', { month: 'short', day: '2-digit' });
}
export function fmtDate(t: number | string | null | undefined): string {
  const ms = toMs(t);
  if (!Number.isFinite(ms)) return '';
  const d = new Date(ms);
  return `${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
export const timeOf = (t: number | string | null | undefined): number => { const ms = toMs(t); return Number.isFinite(ms) ? ms : 0; };

/** A tag chip (auto tags: dashed + lock). */
export const tagChip = (t: string, auto = false, extra = ''): string =>
  `<span class="dc-tag${auto ? ' auto' : ' on'}${extra ? ` ${extra}` : ''}">${auto ? icon('lock') : ''}${esc(t)}</span>`;

/** The entries currently selected, in listing order. */
export const selEntries = (): Entry[] => (D.listing ? D.listing.entries.filter((e) => D.sel.has(e.path)) : []);
