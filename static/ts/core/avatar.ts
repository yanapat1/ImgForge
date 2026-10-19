// User avatars as HTML strings: the uploaded picture (GET api/users/<id>/avatar?v=<version>, a versioned URL the
// browser caches) or, without one, the initials on a colour picked from the user id (8 avatar tokens in
// css/tokens.css, every one >= 4.5:1 under its text). Also the overlapping stack of a session's members and the
// owner chip of a card. Relative 'api/…' also works under s/<id>/ (the server strips the prefix).

import { esc } from './dom.js';

/** What an avatar needs of a user (any of the server's user shapes fits). */
export interface AvatarUser {
  id: string; username?: string | null; name?: string | null; display_name?: string | null; avatar_ver?: number | null;
}
export interface AvatarOpts { title?: boolean; cls?: string }

const N_COLORS = 8;

export const userName = (u: AvatarUser): string => u.display_name || u.name || u.username || 'user';

/** Up to 2 letters: first + last word's first letter ('Ada Lovelace' -> 'AL'), else the first 2 letters. */
export function initials(u: AvatarUser): string {
  const words = userName(u).trim().split(/[\s._-]+/).filter(Boolean);
  const first = words[0] ?? '?', last = words.length > 1 ? words[words.length - 1] ?? '' : '';
  const chars = last ? [...first][0] + ([...last][0] ?? '') : [...first].slice(0, 2).join('');
  return chars.toUpperCase();
}

// FNV-1a over the id: the same user always gets the same colour, on every page
function colorIndex(id: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) { h ^= id.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return ((h >>> 0) % N_COLORS) + 1;
}

export const avatarSrc = (u: AvatarUser): string => `api/users/${encodeURIComponent(u.id)}/avatar?v=${u.avatar_ver ?? 0}`;

/** One round avatar `size` px wide. title: the name as a tooltip (off when a name is shown next to it). */
export function avatarHTML(u: AvatarUser, size = 24, opts: AvatarOpts = {}): string {
  const name = userName(u);
  const tip = opts.title === false ? '' : ` title="${esc(name)}"`;
  const cls = `avatar${opts.cls ? ` ${opts.cls}` : ''}`;
  const style = `--av-size:${size}px`;
  if (u.avatar_ver && u.avatar_ver > 0) {
    return `<span class="${cls}" style="${style}"${tip}><img src="${esc(avatarSrc(u))}" alt="${opts.title === false ? '' : esc(name)}" ` +
      `width="${size}" height="${size}" loading="lazy" decoding="async"></span>`;
  }
  return `<span class="${cls} av-c${colorIndex(u.id)}" style="${style}"${tip} role="img" aria-label="${esc(name)}">` +
    `<span aria-hidden="true">${esc(initials(u))}</span></span>`;
}

/** Overlapping avatars (no names: they are in each one's tooltip), the rest as "+N". */
export function avatarStackHTML(users: AvatarUser[], max = 5, size = 22): string {
  if (!users.length) return '';
  const shown = users.slice(0, max), rest = users.slice(max);
  const all = users.map(userName).join(', ');
  return `<span class="avatar-stack" role="group" aria-label="${esc(`Shared with ${all}`)}" title="${esc(all)}">` +
    shown.map((u) => avatarHTML(u, size)).join('') +
    (rest.length ? `<span class="avatar av-more" style="--av-size:${size}px" title="${esc(rest.map(userName).join(', '))}">+${rest.length}</span>` : '') +
    '</span>';
}

/** The owner of a session: avatar + name. */
export function ownerChipHTML(owner: AvatarUser | null | undefined, size = 20): string {
  if (!owner) return '';
  return `<span class="owner-chip" title="${esc(`Owner: ${userName(owner)}`)}">${avatarHTML(owner, size, { title: false })}` +
    `<span class="oc-name">${esc(userName(owner))}</span></span>`;
}
