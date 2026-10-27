// Typed wrappers for the Data Center routes (/api/dc/*, dataviz/datacenter/API.md). Paths are relative to the app
// root (the page is served there, ?data=…), and every call goes through core/api.ts (X-Requested-With, 401 → sign-in).
// Errors are thrown as core/api.ts Errors; errText(e) gives the server's {detail}.

import { getJSON, postJSON, sendJSON } from '../core/api.js';
import type {
  Bucket, CreateBucketBody, Format, FolderStats, Listing, LsSort, Op, OpsResult, SearchResult, SendResult, SendTarget,
  Summary, Tag, TagCount, UndoResult,
} from '../types-datacenter.js';

const B = (bid: string): string => `api/dc/buckets/${encodeURIComponent(bid)}`;
const qs = (o: Record<string, string | number | boolean | null | undefined | readonly string[]>): string => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) {
    if (v == null || v === '') continue;
    if (Array.isArray(v)) for (const x of v) p.append(k, String(x));
    else p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : '';
};

export const summary = (): Promise<Summary> => getJSON<Summary>('api/dc/summary');

/** Buckets list; the server may answer a bare array or {buckets: [...]}: both are accepted. */
export async function listBuckets(q = '', tags: readonly string[] = [], formats: readonly string[] = []): Promise<Bucket[]> {
  const r = await getJSON<Bucket[] | { buckets: Bucket[] }>(`api/dc/buckets${qs({ q, tag: tags, format: formats })}`);
  return Array.isArray(r) ? r : r.buckets || [];
}
export const getBucket = (bid: string): Promise<Bucket> => getJSON<Bucket>(B(bid));
export const createBucket = (body: CreateBucketBody): Promise<Bucket> => postJSON<Bucket>('api/dc/buckets', body);
export const renameBucket = (bid: string, name: string): Promise<Bucket> => sendJSON<Bucket>('PATCH', B(bid), { name });
export const deleteBucket = (bid: string, deleteFiles = false): Promise<{ ok: boolean }> =>
  sendJSON<{ ok: boolean }>('DELETE', `${B(bid)}${deleteFiles ? '?delete_files=1' : ''}`);

export const PAGE = 500;
export const ls = (bid: string, path = '', offset = 0, limit = PAGE, sort: LsSort = 'name', q = ''): Promise<Listing> =>
  getJSON<Listing>(`${B(bid)}/ls${qs({ path, offset, limit, sort, q })}`);

/** URL of a file (raw), or of its scaled JPEG thumbnail when `w` is given. */
export const fileUrl = (bid: string, path: string, w?: number): string => `${B(bid)}/file${qs({ path, w: w ?? null })}`;
export const readText = (bid: string, path: string): Promise<{ text: string; truncated: boolean }> =>
  getJSON<{ text: string; truncated: boolean }>(`${B(bid)}/text${qs({ path })}`);

export const ops = (bid: string, list: Op[]): Promise<OpsResult> => postJSON<OpsResult>(`${B(bid)}/ops`, { ops: list });
export const undo = (bid: string, undoId: string): Promise<UndoResult> => postJSON<UndoResult>(`${B(bid)}/undo`, { undo_id: undoId });
/** The upload URL (multipart field `files`); upload.ts posts it with an XHR for byte progress. */
export const uploadUrl = (bid: string, path: string): string => `${B(bid)}/upload${qs({ path })}`;

/** Set tags; the server may answer the tag list or {tags}. */
export async function setTags(bid: string, path: string, add: string[], remove: string[]): Promise<Tag[]> {
  const r = await sendJSON<Tag[] | { tags: Tag[] }>('PUT', `${B(bid)}/tags`, { path, add, remove });
  return Array.isArray(r) ? r : r.tags || [];
}
export async function allTags(): Promise<TagCount[]> {
  const r = await getJSON<TagCount[] | { tags: TagCount[] }>('api/dc/tags');
  return Array.isArray(r) ? r : r.tags || [];
}
export const detect = (bid: string, path = '', force = false): Promise<Format> =>
  getJSON<Format>(`${B(bid)}/detect${qs({ path, force: force ? 1 : null })}`);
export const stats = (bid: string, path = ''): Promise<FolderStats> => getJSON<FolderStats>(`${B(bid)}/stats${qs({ path })}`);

export interface SearchQuery { q?: string; tags?: string[]; formats?: string[]; bucket?: string; path?: string; glob?: string }
export const search = (o: SearchQuery): Promise<SearchResult> =>
  getJSON<SearchResult>(`api/dc/search${qs({ q: o.q ?? '', tag: o.tags ?? [], format: o.formats ?? [], bucket: o.bucket ?? '', path: o.path ?? '', glob: o.glob ?? '' })}`);

export const send = (bucket: string, paths: string[], target: SendTarget, name: string, taskType: string): Promise<SendResult> =>
  postJSON<SendResult>('api/dc/send', { bucket, paths, target, name, task_type: taskType });
