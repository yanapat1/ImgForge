// Shapes of the Data Center API (dataviz/datacenter/API.md): /api/dc/* and the NDJSON events of /api/llm/dc/chat.
// Type-only module: build.mjs never bundles types*.ts, so import it with `import type` only.

export type FormatKey = 'yolo-detect' | 'yolo-seg' | 'yolo-pose' | 'yolo-obb' | 'coco' | 'voc' | 'classification' |
  'images' | 'embeddings' | 'other' | 'empty';

export type BucketKind = 'managed' | 'local' | 's3';

export interface FormatCheck { name: string; ok: boolean; detail?: string | null; count?: number | null; warn?: boolean }

/** formats.detect: what a folder (or bucket root) holds. */
export interface Format {
  format: FormatKey | string;
  label: string;
  confidence: number;
  checks: FormatCheck[];
  classes?: number | null;
  reason?: string | null;
}

/** A tag on (bucket, path); auto tags (format:<key>) are set by detection and cannot be removed by hand. */
export interface Tag { tag: string; auto: boolean }
/** GET /api/dc/tags */
export interface TagCount { tag: string; count: number; auto: boolean }

export interface BucketStats { images?: number | null; labels?: number | null; classes?: number | null; size?: number | null; vectors?: number | null; dims?: number | null }
export interface BucketSession { id: string; kind: 'visualize' | 'label'; name: string; url: string; when?: string | null; progress?: number | null }

export interface Bucket {
  id: string; name: string; kind: BucketKind; root: string; writable: boolean;
  owner?: { id: string; username?: string; display_name?: string } | string | null;
  created_at?: string | number | null; updated_at?: string | number | null;
  format: Format | null;
  tags: Tag[];
  stats: BucketStats;
  sessions: BucketSession[];
  /** optional sample image paths for the card mosaic (not in the contract yet: else the page asks search) */
  samples?: string[];
  /** S3 links: the connection + bucket + prefix, the load mode, whether the local copy is in use (pulled at least
   *  once; root is then that folder), the last finished pull, and the s3:// root */
  connection_id?: string | null; s3_bucket?: string | null; prefix?: string;
  mode?: S3Mode; local_ready?: boolean; synced_at?: number | null; s3_root?: string;
  /** PATCH / POST answers that queued a pull */
  job_id?: number | null;
}

/** An S3 link is streamed (read-only) or loaded into an editable local folder (re-synced on demand). */
export type S3Mode = 'stream' | 'local';

/** GET /api/dc/summary */
export interface Summary {
  buckets: number; images: number; size: number;
  managed: { used: number; free: number | null };
  links: { local: number; s3: number };
}

export type EntryKind = 'image' | 'label' | 'yaml' | 'json' | 'xml' | 'text' | 'code' | 'table' | 'audio' | 'video' | 'npy' | 'archive' | 'other';
export interface Entry {
  name: string; path: string; type: 'dir' | 'file'; size: number; mtime: number | string | null;
  kind?: EntryKind; items?: number; w?: number; h?: number; labels?: number;
}

export type LsSort = 'name' | '-mtime' | 'size';
/** GET /api/dc/buckets/{bid}/ls */
export interface Listing { path: string; total: number; entries: Entry[]; format: Format | null; tags: Tag[]; writable: boolean }

/** GET /api/dc/buckets/{bid}/stats */
export interface FolderStats { images: number; labels: number; boxes: number; classes: number; size: number; splits: Record<string, number> }

export interface Retag { path: string; from: string; to: string; reason?: string | null }

export type Op =
  | { op: 'mkdir'; path: string }
  | { op: 'create'; path: string }
  | { op: 'move'; path: string; to: string }
  | { op: 'copy'; path: string; to: string }
  | { op: 'rename'; path: string; name: string }
  | { op: 'delete'; path: string };

/** GET /api/dc/buckets/{bid}/text: `text` has \n line ends; mtime_ns is a string (null on S3) */
export interface TextFile {
  text: string; truncated: boolean; size: number; mtime_ns: string | null; encoding: 'utf-8' | 'utf-8-sig';
  newline: '\n' | '\r\n' | '\r'; editable: boolean; reason?: string;
}
/** PUT /api/dc/buckets/{bid}/text */
export interface TextSaved { path: string; size: number; mtime_ns: string; undo_id: string; created: boolean; retagged: Retag | null }

/** POST /api/dc/buckets/{bid}/ops */
export interface OpsResult { applied: unknown[]; errors: Array<{ op: Op; error: string }>; undo_id: string | null; retagged: Retag | null }
/** POST /api/dc/buckets/{bid}/undo */
export interface UndoResult { restored: number | unknown[]; errors: unknown[]; retagged: Retag | null }
/** POST /api/dc/buckets/{bid}/upload */
export interface UploadResult { added: number | unknown[]; skipped: number | unknown[]; retagged: Retag | null }

export interface SearchFile { bucket: string; path: string; name: string; type: string; size: number }
export interface SearchResult { buckets: Bucket[]; files: SearchFile[] }

export type SendTarget = 'visualize' | 'label';
export interface SendResult { target: SendTarget; id: string; url: string }

export interface CreateBucketBody {
  name: string; kind: BucketKind; path?: string; connection_id?: string; s3_bucket?: string; prefix?: string; tags?: string[];
  mode?: S3Mode;
}

/** POST /api/dc/buckets/{bid}/sync */
export interface SyncResult { job_id: number | null; result: { downloaded: number; bytes: number; total: number } | null }

/** GET /api/dc/buckets/{bid}/source: what New label › Data Center needs to read a bucket folder */
export interface DcSource {
  source: { kind: 'local'; path: string } | { kind: 's3'; connection_id: string; bucket: string; prefix: string };
  format: Format | null; classes: string[]; bucket: string; path: string; name: string;
}

/** POST /api/dc/autolabel */
export interface AutolabelBody {
  bucket: string; path: string;
  model: { registry_id?: string; connection_id?: string; model_id?: string; version?: string | null; name?: string };
  target: 'inplace' | 'copy'; new_name?: string; conf?: number; name?: string;
  /** labeling/writers keys (GET api/label/formats); [] = keep the labels in the session only. write_yolo is the older flag */
  formats?: string[]; write_yolo?: boolean;
}
export interface AutolabelResult { job_id: number | null; bucket: string }
/** the job's result */
export interface AutolabelDone {
  bucket: string; session?: { id: string; name: string; url: string };
  labeled?: { n_images: number; n_shapes: number; failed: number; cancelled: boolean };
  written?: { labels?: number; data_yaml?: string; classes: string[]; formats?: Record<string, { files?: number }> };
  copied?: { files: number; bytes: number } | null;
}

// ---- AI assistant: POST /api/llm/dc/chat (NDJSON) and /api/llm/dc/confirm
export interface AiScope { bucket: string | null; path: string; selection: string[] }
export interface AiAction { id: string; tool: string; args: Record<string, unknown>; summary: string }
export type AiEvent =
  | { type: 'meta'; model?: string; in_est?: number }
  | { type: 'delta'; text: string }
  | { type: 'tool'; id: string; name: string; args: Record<string, unknown>; summary: string; ok: boolean }
  | { type: 'plan'; plan_id: string; actions: AiAction[] }
  | { type: 'ask'; question: string; options: string[] }
  | { type: 'cannot'; reason: string; alternative: string | null }
  | { type: 'done'; usage?: Record<string, unknown> }
  | { type: 'error'; detail: string };
export interface ConfirmResult { results: Array<{ id: string; ok: boolean; summary: string; error?: string }>; retagged: Retag[] }
