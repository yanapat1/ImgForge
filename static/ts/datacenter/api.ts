// Typed wrappers for the Data Center routes (/api/dc/*, dataviz/datacenter/API.md). Paths are relative to the app
// root (the page is served there, ?data=…), and every call goes through core/api.ts (X-Requested-With, 401 → sign-in).
// Errors are thrown as core/api.ts Errors; errText(e) gives the server's {detail}.

import { getJSON, postJSON, sendJSON } from '../core/api.js';
import type {
  AutolabelBody, AutolabelResult, Bucket, CreateBucketBody, DcSource, ExportCheck, ExportRemoteBody, ExportRemoteResult, Format, FolderStats,
  FramesBody, FramesEstimate, FramesOpts, FramesResult, ImportBody, Listing, LocalJob, LsSort, Op, OpsResult,
  S3Mode, SourceInfo, WebSource, SearchResult, SendResult, SendTarget, Summary, SyncResult, Tag, TagCount, TextFile, TextSaved, UndoResult, VideoInfo,
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
export const updateBucket = (bid: string, body: { name?: string; mode?: S3Mode }): Promise<Bucket> => sendJSON<Bucket>('PATCH', B(bid), body);
export const syncBucket = (bid: string): Promise<SyncResult> => postJSON<SyncResult>(`${B(bid)}/sync`, {});
export const exportUrl = (bid: string, path = ''): string => `${B(bid)}/export${qs({ path })}`;
/** Ask first whether the zip can be made (the download itself is an anchor, so its error would be a broken file). */
export const exportCheck = (bid: string, path = ''): Promise<ExportCheck> => getJSON<ExportCheck>(`${B(bid)}/export/check${qs({ path })}`);
/** Copy a folder to S3 / FTP / SFTP through a saved connection (a job on the dc lane, dc-export:<bid>). */
export const exportTo = (bid: string, body: ExportRemoteBody): Promise<ExportRemoteResult> => postJSON<ExportRemoteResult>(`${B(bid)}/export`, body);
export const createRemoteBucket = (id: string, bucket: string): Promise<{ ok: boolean; bucket: string; created: boolean }> =>
  postJSON<{ ok: boolean; bucket: string; created: boolean }>('api/connections/create-bucket', { id, bucket });
export const source = (bid: string, path = ''): Promise<DcSource> => getJSON<DcSource>(`${B(bid)}/source${qs({ path })}`);
export const convert = (bid: string, path: string, to: string): Promise<{ job_id: number | null; path: string }> =>
  postJSON<{ job_id: number | null; path: string }>(`${B(bid)}/convert`, { path, to });
export const sources = (): Promise<{ sources: WebSource[] }> => getJSON<{ sources: WebSource[] }>('api/dc/sources');
export const inspectSource = (source: string, ref: string, connectionId: string | null, opts: Record<string, unknown>): Promise<SourceInfo> =>
  postJSON<SourceInfo>('api/dc/sources/inspect', { source, ref, connection_id: connectionId, opts });
export const importSource = (body: ImportBody): Promise<{ job_id: number | null; bucket: string }> =>
  postJSON<{ job_id: number | null; bucket: string }>('api/dc/import', body);
export const reimport = (bid: string): Promise<{ job_id: number | null; bucket: string }> =>
  postJSON<{ job_id: number | null; bucket: string }>(`${B(bid)}/reimport`);
export const autolabel = (body: AutolabelBody): Promise<AutolabelResult> => postJSON<AutolabelResult>('api/dc/autolabel', body);
export const deleteBucket = (bid: string, deleteFiles = false): Promise<{ ok: boolean }> =>
  sendJSON<{ ok: boolean }>('DELETE', `${B(bid)}${deleteFiles ? '?delete_files=1' : ''}`);

export const PAGE = 500;
export const ls = (bid: string, path = '', offset = 0, limit = PAGE, sort: LsSort = 'name', q = '', retry = false): Promise<Listing> =>
  getJSON<Listing>(`${B(bid)}/ls${qs({ path, offset, limit, sort, q, retry: retry ? 1 : null })}`);
/** A streamed S3 link: download folders into its read-only local copy (again = update; mirror removes what S3 lost). */
export const fetchLocal = (bid: string, paths: string[], mirror = false): Promise<LocalJob> =>
  postJSON<LocalJob>(`${B(bid)}/local`, { paths, mirror });
export const checkLocal = (bid: string): Promise<LocalJob> => postJSON<LocalJob>(`${B(bid)}/local/check`, {});
export const dropLocal = (bid: string, paths: string[]): Promise<{ removed: string[] }> =>
  postJSON<{ removed: string[] }>(`${B(bid)}/local/remove`, { paths });

/** URL of a file (raw), or of its scaled JPEG thumbnail when `w` is given (a video: its frame at `t` seconds, else a poster). */
export const fileUrl = (bid: string, path: string, w?: number, t?: number): string => `${B(bid)}/file${qs({ path, w: w ?? null, t: t ?? null })}`;
export const videoInfo = (bid: string, path: string): Promise<VideoInfo> => getJSON<VideoInfo>(`${B(bid)}/video${qs({ path })}`);
export const framesEstimate = (bucket: string, paths: string[], opts: FramesOpts): Promise<FramesEstimate> =>
  postJSON<FramesEstimate>('api/dc/frames/estimate', { bucket, paths, opts });
/** Cut videos into frames: one dc-lane job (dc-frames:<bucket>:<out path>). */
export const frames = (body: FramesBody): Promise<FramesResult> => postJSON<FramesResult>('api/dc/frames', body);
export const setVideoAuto = (bid: string, opts: FramesOpts | null): Promise<{ video_auto: FramesOpts | null }> =>
  postJSON<{ video_auto: FramesOpts | null }>(`${B(bid)}/video-auto`, { opts });
export const readText = (bid: string, path: string, limit?: number): Promise<TextFile> =>
  getJSON<TextFile>(`${B(bid)}/text${qs({ path, limit: limit ?? null })}`);
/** Save (or with create, make) a text file; 409 when it changed since `expectMtime` (null overwrites). */
export const writeText = (bid: string, path: string, text: string, expectMtime: string | null, create = false): Promise<TextSaved> =>
  sendJSON<TextSaved>('PUT', `${B(bid)}/text`, { path, text, expect_mtime_ns: expectMtime, create });

export const ops = (bid: string, list: Op[]): Promise<OpsResult> => postJSON<OpsResult>(`${B(bid)}/ops`, { ops: list });
export const undo = (bid: string, undoId: string): Promise<UndoResult> => postJSON<UndoResult>(`${B(bid)}/undo`, { undo_id: undoId });
/** The upload URL (multipart field `files`); upload.ts posts it with an XHR for byte progress. */
export const uploadUrl = (bid: string, path: string): string => `${B(bid)}/upload${qs({ path })}`;
/** One chunk (raw body) of a big file `name`: bytes from `offset` of its `total`, gathered under upload id `id`. */
export const uploadPartUrl = (bid: string, path: string, name: string, id: string, offset: number, total: number): string =>
  `${B(bid)}/upload/part${qs({ path, name, id, offset, total })}`;

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
