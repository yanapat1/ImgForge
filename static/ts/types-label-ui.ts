// Extra types of the label dialogs and panels (label/app, panels, export, import, assist, session-actions).
// Pure types: no runtime code. The shared labeling types live in types-label.ts.
import type { Job } from './types.js';
import type { LabelTask, Media } from './types-label.js';

// ------------------------------------------------------------------ session actions (class stats, delete, duplicate)
/** Subset of a project the class manager needs. */
export interface ProjectTasks { name: string; n_media?: number | undefined; tasks: LabelTask[] }
/** GET class-stats → stats[class id] */
export interface ClassStat { n_annotations: number; n_images: number }
export interface ClassStatsPayload { stats: Record<string, ClassStat> | null }
/** POST classes/delete */
export interface DeleteClassesPayload { deleted: number; annotations: number; images: number }
/** What openManageClasses' onDone gets. */
export interface ManageClassesResult extends DeleteClassesPayload { class_ids: string[] }
/** The card of the new session (job result of a duplicate). */
export interface DuplicateResult { id: string; name: string; [k: string]: unknown }
export type DuplicateJob = Job & { result?: DuplicateResult | null };
export interface JobStarted { job_id: number }

// ------------------------------------------------------------------ import
export type ImportFormat = 'auto' | 'yolo' | 'coco' | 'text' | 'db';
export type ImportKind = 'upload' | 'local' | 's3' | 'remote';
export type ImportMode = 'merge' | 'replace' | 'skip_labeled';
export type ImportCoords = 'auto' | 'norm' | 'px';
export type ImportDbMode = 'dataviz' | 'sql';
export interface ImportStatus { kind: 'busy' | 'ok' | 'err'; text: string }
/** POST api/label/annotations/upload */
export interface ImportUpload { upload_id: string; files: number; bytes: number }
export interface ImportPreviewClass { name: string; n: number; match?: string | null }
/** POST api/label/annotations/preview */
export interface ImportPreview {
  format: string; records: number; images: number;
  class_names?: string[] | null;
  skipped?: Record<string, number>;
  errors?: string[];
  matched?: number;
  unmatched_sample?: string[];
  project_images?: number;
  classes?: ImportPreviewClass[];
}
/** The import form's state (one per dialog / wizard). */
export interface ImportState {
  fmt: ImportFormat; kind: ImportKind; taskKey: string | null;
  upload: ImportUpload | null; uploading: boolean;
  path: string; connId: string; bucket: string; prefix: string; rpath: string;
  dbConn: string; dbMode: ImportDbMode; sql: string; taskName: string;
  coords: ImportCoords; mode: ImportMode;
  classMap: Record<string, string>;
  preview: ImportPreview | null; status: ImportStatus | null; busy: boolean;
}
/** A task as the import form sees it (the wizard's draft tasks and the saved session's tasks). */
export interface ImportTask {
  key: string; name: string; task_type: string;
  id?: string | undefined;
  kpText?: string | undefined;
  classes: Array<{ name: string; id?: string | undefined }>;
}
export interface ImportCtx { conns: ImportConn[]; tasks: ImportTask[]; wizard: boolean; pid?: string | undefined }
/** The fields of a saved connection the import / export forms read. */
export interface ImportConn { id: string; kind?: string | undefined; name?: string | undefined; host?: string | undefined; endpoint?: string | undefined; table_prefix?: string | undefined }
export type ImportSource =
  | { kind: 'db'; connection_id: string; mode: ImportDbMode; sql: string; task_name: string | null }
  | { kind: 'upload'; upload_id: string | undefined }
  | { kind: 'local'; path: string }
  | { kind: 's3'; connection_id: string; bucket: string; prefix: string }
  | { kind: string; connection_id: string; path: string };
/** Result of an import job. */
export interface ImportResult {
  format?: string; annotations?: number; images?: number; unmatched?: number; unmatched_sample?: string[];
  classes_added?: string[]; skipped?: Record<string, number>; errors?: string[];
}
export interface ImportHandlers {
  get(): ImportState | null;
  render(): void;
  ctx(): ImportCtx;
  onPreview?(preview: ImportPreview, task: ImportTask | undefined): void;
}

// ------------------------------------------------------------------ export
export type ExportKind = 'zip' | 'local' | 's3' | 'ftp' | 'sftp' | 'db';
export interface ExportTarget {
  kind: ExportKind; path?: string; connection_id?: string; bucket?: string; prefix?: string; create_views?: boolean;
}
/** project.export_default as the server stores it (+ the target fields mirrored at the top level). */
export interface ExportDefault {
  target?: ExportTarget; kind?: ExportKind; path?: string; connection_id?: string; bucket?: string; prefix?: string; create_views?: boolean;
  task_ids?: string[]; classes?: Record<string, string[]>; include_images?: boolean; only_done?: boolean; overwrite?: boolean;
}
export interface ExportBody {
  target: ExportTarget; task_ids: string[]; classes: Record<string, string[]>;
  include_images: boolean; only_done: boolean; overwrite: boolean;
}
export interface ExportSummary { tasks?: Record<string, Record<string, number>>; error?: string; [k: string]: unknown }
/** GET api/label/exports/{run_id} */
export interface ExportRun { run_id?: string; status?: string; summary?: ExportSummary; download?: string }
export interface ExportFolder { name: string; images?: number }
/** GET api/fs/browse, POST api/connections/browse */
export interface ExportBrowse { path: string; parent?: string | null; folders?: ExportFolder[] }

// ------------------------------------------------------------------ assistant
/** A suggested box (normalised coordinates) from POST api/llm/label/{pid}/(prelabel|assist). */
export interface AssistBox { x1: number; y1: number; x2: number; y2: number; class_name?: string; class_id?: string | null; note?: string }
export interface AssistItem extends AssistBox { key: number }
export interface AssistResponse { answer?: string; usage?: { in: number; out: number }; boxes?: AssistBox[] }
export interface VoiceCommand { transcript?: string; action?: string; args?: Record<string, unknown> }

// ------------------------------------------------------------------ panels
/** One button of the small dialog `ask`. */
export interface AskButton { id: string; label: string; primary?: boolean; danger?: boolean }
export interface AskOptions { title: string; html?: string; buttons: AskButton[] }
/** The clicked button and the values of the dialog's named fields; null when dismissed. */
export interface AskResult { id: string; values: Record<string, string> }
/** A row of GET api/label/projects → projects[] (per-task progress for the task tabs). */
export interface ProjectStatsRow { id: string; tasks?: Array<{ id: string; done?: number | undefined }> }
/** The status filter of the image strip: all | todo | in_progress | done | skipped | review:none|manual|bulk. */
export type StripFilterValue = string;
export type ClassMatchMode = 'any' | 'all' | 'none';
/** GET api/label/projects/{pid}/media */
export interface MediaPage { total: number; items?: Media[] }
