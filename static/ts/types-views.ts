// Types of the visualizer's views (views/model.ts, eval.ts, curation.ts, viewer.ts, box-editor.ts, controls.ts,
// layout.ts, filter-bar.ts, modal.ts): payloads of dataviz/server/routes/{models,curation,samples}.py and the
// models/insights.py + models/evaluate.py results. Pure types, no runtime code.

import type { Json, ModelEntry, ModelEval, SavedView, Sample } from './types.js';

// ------------------------------------------------------------------ models (GET api/models, api/models/{id}/eval)
/** A saved model as the Models list sends it (types.ts ModelEntry + the fields the views read). */
export interface ModelInfo extends ModelEntry { family: string; format: string; conf?: number }
export interface ModelsPayload { models: ModelInfo[] }

export interface EvalAP {
  ap50: number[]; ap: number[]; map50: number; map: number; n_labels: number[];
  pr: number[][]; pr_all: number[];
  conf_grid: number[]; p_conf_all: number[]; r_conf_all: number[]; f1_conf_all: number[];
  best_conf: number; best_f1: number;
  hist_bins: number[]; hist_tp: number[]; hist_fp: number[];
}
/** One row of the per-class table (models/evaluate.py summary). */
export interface ClassRow {
  cls: number; n: number; tp: number; p: number | null; r: number | null; ap50: number | null; ap: number | null;
  wrong_cls: number; loc: number; missed: number; fp_bg: number; fp_dup: number;
}
export interface EvalTotals {
  labels: number; preds: number; tp: number; images: number; p: number; r: number;
  wrong_cls: number; loc: number; missed: number; fp_bg: number; fp_dup: number; fp_loc: number;
}
/** A likely label error (models/evaluate.py label_issues). */
export interface EvalIssue {
  kind: 'wrong_cls' | 'fp_bg' | 'missed'; score: number; image: string;
  sid?: number; pid?: number; cls?: number; pred_cls?: number;
}
/** A false positive (ModelService.evaluate fps[]). */
export interface EvalFp { pid: number; score: number; pred_cls: number; kind: string; image: string; gt: number }
/** GET api/models/{id}/eval, completely typed (types.ts ModelEval only has the outcome columns). */
export interface ModelEvalFull extends ModelEval {
  model: ModelInfo; splits: Record<string, number>; split_source: string | null | undefined;
  conf: number; match_iou: number; ms: number; unmatched: string[]; n_images: number;
  ap: EvalAP; per_class: ClassRow[]; confusion: number[][]; totals: EvalTotals;
  fps: EvalFp[]; issues: EvalIssue[];
}
/** POST api/models/{id}/predict. */
export type PredictResult = { ready: true } | { ready: false; job_id: number };
/** One drawn prediction (GET api/models/{id}/image-preds). */
export interface ImagePred { box: [number, number, number, number]; score: number; cls: number; pid: number }
/** GET api/models/{id}/pred/{pid}. */
export interface PredInfo { image: string; box: [number, number, number, number]; score: number; cls: number; cls_name: string | null; labels: number[] }

// ------------------------------------------------------------------ Evaluation tab (GET api/eval/overview, api/eval/compare)
export interface LbRow {
  id: string; name: string; family: string; format: string; map50: number; map: number; p: number; r: number; f1: number;
  conf: number; best_conf: number; ms: number; size_mb: number | null; preds: number; tp: number; labels: number; images: number;
  missed: number; wrong_cls: number; fp_bg: number; fp_dup: number; unmatched: string[];
}
/** Sortable / numeric columns of a leaderboard row. */
export type LbKey = 'map' | 'map50' | 'p' | 'r' | 'f1' | 'conf' | 'ms' | 'size_mb' | 'missed' | 'wrong_cls' | 'fp_bg' | 'fp_dup';
export interface Leaderboard {
  rows: LbRow[];
  per_class: { classes: string[]; models: string[]; ap50: Array<Array<number | null>>; winner: Array<string | null> };
  best: string | null; fastest_near_best: string | null; not_run: ModelInfo[]; split: string;
}
export interface FindingAction {
  kind: 'select' | 'model' | 'image'; ids?: number[]; title?: string; mid?: string; cls?: number; sid?: number;
}
export interface Finding { severity: 1 | 2 | 3; title: string; detail: string; action: FindingAction | null; tag: string }
export interface InsightClass extends ClassRow { n: number }
export interface Insights {
  model: ModelInfo; split: string; splits: Record<string, boolean>; conf: number; match_iou: number;
  per_class: ClassRow[];
  balance: { train_labels: number[]; from_train_split: boolean; corr_log_count_ap50: number | null };
  gap: { train_ap50: Array<number | null>; val_ap50: Array<number | null>; n_val: number[]; map50: [number, number]; map: [number, number] } | null;
  sessions: Array<{ session: string; labels: number; tp: number; missed: number; wrong_cls: number; fp: number; images: number;
    recall: number | null; precision: number | null; ids: number[] }>;
  worst: Array<{ image: string; labels: number; errors: number; fp: number; missed: number; sid: number | null; pid: number | null; focus: number | null }>;
  localization: { per_class: Array<{ cls: number; n: number; mean: number | null; p10: number | null; loose: number | null }>; hist: number[]; mean: number | null };
  calibration: { conf: number[]; precision: Array<number | null>; count: number[]; ece: number | null; bias: number | null };
  sizes: Array<{ size: 'small' | 'medium' | 'large'; labels: number; tp: number; recall: number | null; ids: number[] }>;
  knn: null | { feature: string | null } & Record<'both' | 'model_only' | 'knn_only' | 'neither', { n: number; ids: number[] }>;
}
export type KnnCell = 'both' | 'model_only' | 'knn_only' | 'neither';
export interface CompareCounts { tp_a: number; tp_b: number; only_a: number; only_b: number; both_miss: number }
export type CompareKey = 'only_a' | 'only_b' | 'both_miss';
export interface Compare {
  a: ModelInfo; b: ModelInfo; split: string; conf_mode: string; conf_a: number; conf_b: number; match_iou: number; labels: number;
  counts: CompareCounts; ids: Record<CompareKey, number[]>;
  b_lost: Record<string, number>; a_lost: Record<string, number>;
  per_class: { classes: string[]; labels: number[]; tp_a: number[]; tp_b: number[]; only_a: number[]; only_b: number[]; both_miss: number[];
    fp_a: number[]; fp_b: number[]; ap50_a: Array<number | null>; ap50_b: Array<number | null> };
  map_a: number; map_b: number; map50_a: number; map50_b: number;
}
export interface EvalOverview {
  leaderboard: Leaderboard; focus: string | null; insights: Insights | null; split_source: string | null;
  findings: Finding[];
  dataset: { name: string; uri: string; images: number; labels: number; classes: string[] };
  /** added client side before the JSON export */
  compare?: Compare;
}

// ------------------------------------------------------------------ curation (routes/curation.py)
export type CurSeverity = 'error' | 'warn' | 'info';
export interface CurFix { op: string; label: string; line?: number; cls?: number; [k: string]: unknown }
export interface CurItem {
  id: string; type: string; severity: CurSeverity; group: string; image: string | null; label: string | null;
  line: number | null; box_ids: number[]; boxes: Array<[number, number, number, number]>; detail: string; fixes: CurFix[];
  sha1: string | null; wh: [number, number] | null; cls?: number;
}
export interface CurTypeSummary { n: number; severity: CurSeverity; group: string; label: string; bulk: boolean }
export type CurSummary = Record<string, CurTypeSummary>;
export interface CurHistoryEntry { undo_id: string; time: number; summary: string; applied: number; files: number; current: boolean }
/** GET api/curation/summary. */
export interface CurInfo {
  ready: boolean; scanning: boolean; scanned_at: number | null; scan_s: number | null; summary: CurSummary; n_fixes: number;
  writable: boolean; scores: string | null; model: string | null;
  types: Record<string, { group: string; severity: CurSeverity; label: string; bulk: boolean }>;
  history: CurHistoryEntry[];
}
/** The in-memory change of the points a fix / undo / edit made. */
export interface CurPatch { cls?: Record<string, number>; deleted?: number[]; restored?: number[] }
export interface CurFileError { file: string; error: string }
/** POST api/curation/fix, api/edits and api/curation/undo answers (the fields the page reads). */
export interface CurResult {
  applied?: number; files?: number; conflicts?: string[]; errors?: CurFileError[]; undo_id?: string | null; patch?: CurPatch;
  stale?: boolean;
}
export interface CurIssuesPage { summary: CurSummary; total: number; offset: number; items: CurItem[] }
export interface CurIds { images: string[]; boxes: number[] }
/** One op of POST api/curation/fix / api/edits. */
export type CurOp = { op: string; line?: number; cls?: number; cx?: number; cy?: number; w?: number; h?: number; [k: string]: unknown };

// ------------------------------------------------------------------ image viewer / box editor
/** One label box of GET api/img/boxes: normalised centre box. `line` = the box's line in the label file. */
export interface ImgBox { line: number; cls: number; cx: number; cy: number; w: number; h: number; sid: number | null }
export interface ImgBoxes { label: string; sha1: string; writable: boolean; img_w: number; img_h: number; boxes: ImgBox[] }
/** POST api/edits answer. */
export interface EditResult extends CurResult { label: string; sha1: string; boxes: ImgBox[]; img_w: number; img_h: number; writable: boolean;
  /** GET api/edits/status after the save (same shape as image-grid.ts EditStatus) */
  status: { pending_new: number; moved: number; images?: unknown } }
/** GET api/sample/{id}, as the dialog reads it (types.ts Sample + the server's extra fields). */
export type SampleInfo = Sample & {
  cls: number; cls_name: string; image: string; label: string; box_idx: number | null; area: number; aspect: number;
  brightness: number; blur: number; session: string; img_w: number; img_h: number;
};

// ------------------------------------------------------------------ settings (GET/POST api/settings)
/** The remembered UI settings: widget values by id plus the keys the hooks add. */
export type SettingsBlob = Record<string, Json | undefined>;
export interface SettingHook { collect: (out: Record<string, unknown>) => void; apply: (st: Record<string, unknown>) => void }
export interface ProjectionsPayload { views: SavedView[] }

// ------------------------------------------------------------------ viewer state (views/viewer.ts `V`, read by box-editor.ts)
export interface ViewerState {
  name: string | null; W: number; H: number; boxes: ImgBox[]; sha1: string | null; label: string | null; writable: boolean;
  focus: number | null; preds: ImagePred[]; seq: number; fixed: boolean; focusPid: number | null;
}
/** A class tag drawn above a box (viewer.ts tag()). */
export type TagFn = (x: number, y: number, text: string, col: string, k: number, dashed?: boolean, bottom?: number) => SVGElement[];
/** What the viewer hands the editor to draw with. */
export interface EditorDraw { names: boolean; color: (c: number) => string; nameOf: (c: number) => string; tag: TagFn }

/** A card of the Model tab's grids / the dialog's prev-next list: a label box (sid) or a bare prediction (pid). */
export interface CardRef { sid?: number | undefined; pid?: number | undefined }

// ------------------------------------------------------------------ fields views/ add to the page state S
// (S is the AppState of types.ts: the saved projections list and the panel layout live on it too)
declare module './types.js' {
  interface AppState {
    /** GET api/projections → views[] (controls.ts refreshSavedViews) */
    views?: SavedView[];
    /** dragged width of the left panel in px, null = the CSS default (layout.ts) */
    leftW?: number | null;
    /** which side panel is collapsed (layout.ts) */
    collapsed?: 'left' | 'right' | null;
  }
}
