// Embeddings sessions (dataset kind 'embeddings', dataviz/data/embedding_source.py): a dot is a row of the user's own
// table, not a box in an image. This module draws what a row looks like in a gallery card, the plot's hover tip and the
// detail dialog: its image thumbnail, an audio clip, a video, its text, or (no preview at all) its id and columns.
// Audio plays through one shared <audio> element, so starting a clip stops the last one.

import { esc, icon } from '../core/dom.js';
import { pts, S } from '../core/state.js';

/** True in an embeddings session (rows with metadata, no boxes / images / models). */
export const isRows = (): boolean => S.status?.dataset?.kind === 'embeddings';

/** "3 boxes" in a YOLO session, "3 rows" in an embeddings one. */
export const unit = (n: number): string => (isRows() ? (n === 1 ? 'row' : 'rows') : (n === 1 ? 'box' : 'boxes'));

const kinds = (id: number): string[] => S.P?.preview?.[id] ?? [];
const has = (id: number, k: string): boolean => kinds(id).includes(k);
const textOf = (id: number): string => S.P?.text?.[id] ?? '';
export const rowId = (id: number): string => S.P?.row_id?.[id] ?? `#${id}`;

/** The metadata columns, in table order, as [name, type]. */
export function metaCols(): Array<[string, 'num' | 'cat' | 'text']> {
  return Object.entries(S.P?.meta_types ?? {});
}

/** A metadata value as text: numbers short and tabular, blanks as a dash. */
export function metaText(v: number | string | null | undefined): string {
  if (v === null || v === undefined || v === '') return '–';
  if (typeof v === 'number') return Math.abs(v) >= 1e4 || Number.isInteger(v) ? v.toLocaleString() : String(+v.toPrecision(4));
  return v;
}

// the first two columns, for the corner of a card and the hover tip (categories before numbers: they read faster)
function topMeta(id: number, n = 2): Array<[string, string]> {
  const P = pts();
  const cols = metaCols().filter(([, t]) => t !== 'text').sort((a, b) => (a[1] === 'cat' ? 0 : 1) - (b[1] === 'cat' ? 0 : 1));
  return cols.slice(0, n).map(([c]) => [c, metaText(P.meta?.[c]?.[id])]);
}

// ---------------------------------------------------------------- gallery card body
/** What fills a card's square: the thumbnail for a box or an image row, a player for audio, text, or the row's id. */
export function cardMedia(id: number): string {
  if (!isRows() || has(id, 'image')) return `<img loading="lazy" src="api/crop/${id}" alt="">`;
  if (has(id, 'video')) {
    // the first frame is the poster; it plays muted while hovered (bindMedia)
    // no src until the card scrolls into view (loadVisibleMedia): a page of 100 cards is not 100 requests at once
    return `<video class="rc-video" muted playsinline loop preload="none" data-src="api/preview/${id}/video#t=0.1"></video>` +
      `<span class="rc-kind">${icon('film')}</span>`;
  }
  const meta = topMeta(id).map(([k, v]) => `<span><i>${esc(k)}</i>${esc(v)}</span>`).join('');
  const text = textOf(id);
  if (has(id, 'audio')) {
    return '<div class="rc-body rc-audio">' +
      `<button class="rc-play" data-play="${id}" aria-label="Play ${esc(rowId(id))}" title="Play (click again to stop)">${icon('play')}${icon('pause')}</button>` +
      `<div class="rc-text">${esc(text || rowId(id))}</div><div class="rc-meta">${meta}</div><span class="rc-progress"></span></div>`;
  }
  if (text) return `<div class="rc-body rc-quote"><div class="rc-text">${esc(text)}</div><div class="rc-meta">${meta}</div></div>`;
  return `<div class="rc-body rc-vec">${icon('vectors')}<b class="num">${esc(rowId(id))}</b><div class="rc-meta">${meta}</div></div>`;
}

// ---------------------------------------------------------------- hover tip on the plot
/** The tip's media for a dot: the crop for boxes and image rows, else the row's text or id with its first columns. */
export function tipMedia(id: number): string {
  if (!isRows() || has(id, 'image')) return `<img src="api/crop/${id}" alt="">`;
  const kind = has(id, 'video') ? 'film' : has(id, 'audio') ? 'wave' : has(id, 'text') ? 'text' : 'vectors';
  const meta = topMeta(id, 3).map(([k, v]) => `<span><i>${esc(k)}</i>${esc(v)}</span>`).join('');
  return `<div class="rc-tip">${icon(kind)}<div class="rc-text">${esc(textOf(id) || rowId(id))}</div><div class="rc-meta">${meta}</div></div>`;
}

// ---------------------------------------------------------------- detail dialog
/** The dialog's stage for a row: the full image, a playable audio / video, or the text, largest first. */
export function detailMedia(id: number): string {
  const parts: string[] = [];
  if (has(id, 'video')) parts.push(`<video class="rd-video" controls preload="metadata" src="api/preview/${id}/video"></video>`);
  else if (has(id, 'image')) parts.push(`<img class="rd-image" src="api/preview/${id}/image" alt="${esc(rowId(id))}">`);
  if (has(id, 'audio')) parts.push(`<div class="rd-audio">${icon('wave')}<audio controls preload="metadata" src="api/preview/${id}/audio"></audio></div>`);
  const text = textOf(id);
  if (text) parts.push(`<blockquote class="rd-text">${esc(text)}</blockquote>`);
  if (!parts.length) parts.push(`<div class="rd-none">${icon('vectors')}<b class="num">${esc(rowId(id))}</b><span>This row has no preview: only its vectors and columns.</span></div>`);
  return parts.join('');
}

// ---------------------------------------------------------------- audio + video behaviour
let player: HTMLAudioElement | null = null;
let playing: number | null = null;

function cardsOf(id: number): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>(`.card[data-id="${id}"]`)];
}

function setPlaying(id: number | null): void {
  if (playing !== null) for (const c of cardsOf(playing)) { c.classList.remove('playing'); c.style.removeProperty('--p'); }
  playing = id;
  if (id !== null) for (const c of cardsOf(id)) c.classList.add('playing');
}

/** Play row `id`'s audio (or stop it when it is the one playing). */
export function togglePlay(id: number): void {
  if (!player) {
    player = new Audio();
    player.preload = 'none';
    player.addEventListener('ended', () => setPlaying(null));
    player.addEventListener('error', () => setPlaying(null));
    // the card's bottom edge fills as the clip plays: a real position, not a decoration
    player.addEventListener('timeupdate', () => {
      if (playing === null || !player || !player.duration) return;
      for (const c of cardsOf(playing)) c.style.setProperty('--p', String(player.currentTime / player.duration));
    });
  }
  if (playing === id) { player.pause(); setPlaying(null); return; }
  player.src = `api/preview/${id}/audio`;
  setPlaying(id);
  player.play().catch(() => setPlaying(null));
}

export function stopAudio(): void {
  if (player) player.pause();
  setPlaying(null);
}

/** Wire a container of cards: play buttons, and videos that play while hovered. Call once per container. */
export function bindMedia(root: HTMLElement): void {
  root.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-play]') : null;
    if (!b) return;
    e.stopPropagation();                    // the card itself would open the dialog
    togglePlay(+(b.dataset['play'] ?? NaN));
  }, true);
  root.addEventListener('mouseover', (e) => {
    const v = e.target instanceof Element ? e.target.closest('.card')?.querySelector<HTMLVideoElement>('video.rc-video') : null;
    if (v && !v.getAttribute('src')) startVideo(v);
    if (v && v.paused) v.play().catch(() => {});
  });
  root.addEventListener('mouseout', (e) => {
    const card = e.target instanceof Element ? e.target.closest('.card') : null;
    const to = e.relatedTarget instanceof Element ? e.relatedTarget.closest('.card') : null;
    const v = card && card !== to ? card.querySelector<HTMLVideoElement>('video.rc-video') : null;
    if (v) { v.pause(); }
  });
}

/** After a gallery re-render: the playing clip's cards are new elements, so mark them again. */
export function syncPlaying(): void {
  if (playing !== null) setPlaying(playing);
}

function startVideo(v: HTMLVideoElement): void {
  const src = v.dataset['src'];
  if (!src || v.getAttribute('src')) return;
  v.preload = 'metadata';                   // just the first frame (the poster) and the duration
  v.src = src;
}

const videoObserver = typeof IntersectionObserver === 'function'
  ? new IntersectionObserver((entries, obs) => {
    for (const e of entries) {
      if (!e.isIntersecting || !(e.target instanceof HTMLVideoElement)) continue;
      startVideo(e.target);
      obs.unobserve(e.target);
    }
  }, { rootMargin: '200px' })                // a little ahead of the scroll, so frames are there when seen
  : null;

/** Give the video cards under `root` their source once they come near the viewport (all at once without an observer). */
export function loadVisibleMedia(root: HTMLElement): void {
  for (const v of root.querySelectorAll<HTMLVideoElement>('video.rc-video[data-src]:not([src])')) {
    if (videoObserver) videoObserver.observe(v);
    else startVideo(v);
  }
}

/** The header pills of an embeddings session: rows, feature spaces, classes and what the rows carry. */
export function rowsPills(nRows: number, nClasses: number): string {
  const ds = S.status?.dataset;
  const nFeat = S.status?.features?.length ?? 0;
  const pill = (v: string | number, k: string): string => `<span class="pill"><b>${esc(v)}</b>${k}</span>`;
  const mods = (ds?.modality ?? []).filter((m) => m !== 'vector');
  return pill(nRows.toLocaleString(), 'rows') + pill(nFeat, nFeat === 1 ? 'feature space' : 'feature spaces') +
    pill(nClasses, 'classes') +
    (mods.length ? `<span class="pill modality">${mods.map((m) => `${icon(m === 'audio' ? 'wave' : m === 'video' ? 'film' : m)}${esc(m)}`).join('')}</span>` : '');
}

/** Gallery "nothing selected" state of an embeddings session: every row, in table order (no image browser). */
export function allRows(): number[] {
  return pts().id;
}
