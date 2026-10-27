// Shared domain types: API payloads (dataviz/server/routes/*.py), dataset / image / annotation / label shapes and the
// page state S. Pure types: no runtime code. Fields the server may omit are optional (`?`); `| null` means the
// server sends an explicit null. Payloads that other modules extend carry an index signature (`[k: string]: unknown`).

// ------------------------------------------------------------------ generic
/** Plain JSON value (what `JSON.parse` returns). */
export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
/** Lower / upper bound, null = open (filter ranges, slider pairs). */
export type Range = [number | null, number | null];
/** Numeric column of the points payload (JSON array, or a typed array once converted). */
export type NumCol = ArrayLike<number>;
/** `#rrggbb` colour. */
export type Hex = string;
/** Plotly colour scale: [[0, '#...'], ..., [1, '#...']]. */
export type ColorScale = Array<[number, string]>;

// ------------------------------------------------------------------ jobs (dataviz/core/jobs.py Job.to_dict)
export type JobStatus = 'queued' | 'running' | 'done' | 'error' | 'cancelled';
export interface Job {
  id: number;
  key: string;
  label: string;
  status: JobStatus;
  stage: string;
  done: number;
  total: number;
  extra: string;
  pct: number | null;            // null while total is unknown
  eta: number | null;            // seconds left
  elapsed: number;
  error: string | null;
  cancelled: boolean;
  result?: unknown;              // only when the job is done and its result is public
}
/** GET api/jobs */
export interface JobsPayload { jobs: Job[]; active: Job[] }

// ------------------------------------------------------------------ LLM (GET api/llm/state)
export interface LLMProviderInfo {
  id: string; name: string; kind: string; model: string; vision: boolean; audio: boolean;
}
export interface LLMBudget { max_input_tokens: number; [k: string]: unknown }
/** Server part of the assistant's switchboard. */
export interface LLMStatePayload {
  connected: boolean;
  provider: LLMProviderInfo | null;
  features: Record<string, boolean>;   // effective switches (switch on AND connected AND capability)
  switches: Record<string, boolean>;   // what the user switched on in Settings
  budget: LLMBudget;
}
/** Client-side LLM state (core/llm.ts `LLM`). */
export interface LLMState extends LLMStatePayload { loaded: boolean }

// ------------------------------------------------------------------ status / meta (GET api/status, api/meta)
export interface MethodSpec { label: string; category?: string; note?: string; params: Record<string, unknown>; [k: string]: unknown }
export interface FeatureSpec { key: string; label: string; group: string; note: string; text: boolean; cached: boolean; metric?: string }
/** Set when the dataset changed after the cache shown was built (dataviz/state.py `State.stale`). */
export interface DatasetStale { cached: string | null; current: string | null; built: string | null; [k: string]: unknown }

export interface DatasetInfo {
  id: string; name: string; uri: string; kind: string; mode: string | null; index_job: number | null; stale: DatasetStale | null;
  /** embeddings sessions: the preview kinds its rows carry ('image' | 'audio' | 'video' | 'text', or ['vector']) */
  modality?: string[] | null;
}
/** What the page may offer for the open session (server/deps.py `capabilities`): an embeddings session has rows with
 *  metadata instead of boxes in images, so no curation, no models and no image viewer. */
export interface Caps {
  boxes: boolean; images: boolean; curation: boolean; models: boolean; meta: boolean;
  /** what one point is: a labelled box, a whole image (data/views.py ImageView) or a row of an embeddings session */
  unit?: 'box' | 'image' | 'embeddings';
  /** local / S3: 'yolo' (images/ + labels/) or 'flat' (plain images) */
  layout?: 'yolo' | 'flat' | null;
  /** a combined dataset (data/union.py): the Compare tab, origin / diff colours */
  union?: boolean;
  /** the session has a live change feed (GET api/live: labeling sessions, promoted unions) */
  live?: boolean;
}
/** Page defaults from config.yaml `ui` (+ analysis.groups / duplicate_threshold). */
export interface UiDefaults {
  page_size: number; similar_k: number; check_list_size: number; duplicate_threshold: number;
  groups?: unknown; config?: string | null; [k: string]: unknown;
}
export interface Status {
  ready: boolean;
  methods: Record<string, MethodSpec>;
  ui: UiDefaults;
  jobs: Job[];
  active: Job[];
  dataset: DatasetInfo | null;         // null on the Sessions home
  caps?: Caps;
  llm: LLMStatePayload | null;
  // only with an open session:
  default_feature?: string;
  startup?: { feature: string; method: string; cached: boolean };
  features?: FeatureSpec[];
  /** my access to the open session (s/<id>/): a viewer cannot edit boxes, fix curation issues or write tags */
  my_role?: Role;
}
/** Access to a shared session (dataviz/auth/access.py): owner (or an admin) > labeler > viewer. */
export type Role = 'owner' | 'labeler' | 'viewer';
/** GET api/meta */
export interface Meta {
  dataset: string; dataset_uri: string; source: string;
  n_samples: number; n_images: number;
  classes: string[];
  counts: number[];                    // boxes per class
  palette: string[];
  palette_light?: string[];
  knn_k: number;
  index_stats: Record<string, unknown>;
  /** a combined dataset: its members in order (origin = the position); null / absent otherwise */
  members?: UnionMember[] | null;
}
/** One member of a combined dataset (GET api/meta members[]). */
export interface UnionMember { alias: string; name: string; sid: string; n: number; n_images: number; offset: number; unit: string | null }

// ------------------------------------------------------------------ points / projection / groups
/** GET api/points (columnar, one entry per label box = sample id) + the columns added client side. */
export interface Points {
  id: number[];
  cls: number[];                       // class id per box (curation edits it in place)
  image: string[];
  session: string[];
  area: number[];
  aspect: number[];
  brightness: number[];
  blur: number[];
  // scores of the current feature (GET api/scores, set by ensureScores)
  suspicion?: number[];
  suggested?: number[];
  outlier?: number[];
  // curation: 1 = box removed by a fix
  deleted?: Uint8Array;
  // Model tab: outcome of each label box (-1 = n/a, 0 found, 1 wrong class, 2 badly placed, 3 missed)
  m_status?: number[];
  m_pred?: number[];
  m_score?: number[];
  // cleanlab label quality of the current feature (GET api/label-quality, set by loadLabelQuality); null = not scored
  cl_quality?: Array<number | null>;
  cl_given_prob?: Array<number | null>;
  cl_ood_feat?: Array<number | null>;
  cl_ood_prob?: Array<number | null>;
  cl_issue?: number[];                 // 1 = cleanlab says the label is wrong
  cl_rank?: number[];                  // 0 = most likely wrong, -1 = no issue
  cl_suggested?: number[];             // class with the highest out-of-fold probability
  // cleanlab object detection on the Model tab's predictions (GET api/models/{id}/cleanlab); 0-1, low = bad
  cl_swap?: Array<number | null>;
  cl_badloc?: Array<number | null>;
  cl_image_quality?: Array<number | null>;
  // embeddings sessions (rows, not boxes): the row's own id, which previews it has, its text and metadata columns
  row_id?: string[];
  preview?: string[][];
  text?: string[] | null;
  meta?: Record<string, Array<number | string | null>>;
  meta_types?: Record<string, 'num' | 'cat' | 'text'>;
  // combined datasets: member index of each box, and the same-content image group (-1 = none)
  origin?: ArrayLike<number>;
  hgroup?: number[];
  // live feed: 1 = the box moved since the index (its dot is at the old place)
  moved?: Uint8Array;
}
/** Names of the numeric Points columns (colour-by / sort / filter fields). */
export type PointsColumn = 'area' | 'aspect' | 'brightness' | 'blur' | 'suspicion' | 'outlier' | 'm_score' |
  'cl_quality' | 'cl_given_prob' | 'cl_ood_feat' | 'cl_ood_prob' | 'cl_swap' | 'cl_badloc' | 'cl_image_quality';

/** GET api/models/{id}/cleanlab */
export interface DetectionChecks {
  cl_swap: Array<number | null>; cl_badloc: Array<number | null>; cl_image_quality: Array<number | null>;
  images: Array<{ image: string; quality: number }>; overlooked: Array<{ image: string; pid: number; quality: number }>;
}

/** One class of the cleanlab summary (rank_classes_by_label_quality). */
export interface LqClass { cls: number; name: string; issues: number; inverse_issues: number; noise_rate: number; quality: number }
/** Dataset-level cleanlab results of one feature space. */
export interface LqSummary {
  n: number; classes: number; class_counts: number[]; settings: Record<string, unknown>; note?: string;
  class_ids?: number[]; confident_joint?: number[][]; health_score?: number; n_issues?: number;
  class_ranking?: LqClass[];
  overlapping_classes?: Array<{ a: number; b: number; a_name: string; b_name: string; n: number; joint: number }>;
  imbalance?: number; oof_accuracy?: number | null; oof_log_loss?: number | null; folds_used?: number;
  left_out_classes?: number[]; feature?: string;
}
/** GET / POST api/label-quality (ready). */
export interface LabelQualityReady {
  ready: true; feature: string; quality: Array<number | null>; issue: number[]; rank: number[]; suggested: number[];
  given_prob: Array<number | null>; ood_feat: Array<number | null>; ood_prob: Array<number | null>; summary: LqSummary;
}
/** Not computed (GET) or computing (POST: job_id). */
export interface LabelQualityPending { ready: false; feature: string; job_id?: number }
export type LabelQualityResult = LabelQualityReady | LabelQualityPending;

/** GET api/scores?feature=… (ready) */
export interface ScoresReady { ready: true; feature: string; suspicion: number[]; suggested: number[]; outlier: number[] }
/** Not computed yet: wait for the job, ask again. */
export interface NotReady { ready: false; job_id: number; key?: string }
export type ScoresResult = ScoresReady | NotReady;

/** POST api/projection body (dataviz/server/models.py ProjectionRequest). */
export interface ProjectionRequest {
  feature: string; method: string; params: Record<string, number>; classes: number[] | null;
}
/** `quality`: see dataviz/ml/analysis.py `projection_quality` (absent on projections saved before it existed). */
export interface ProjectionQuality { trustworthiness: number; continuity: number; knn_preserved: number; k: number; n: number }
export interface ProjectionInfo { explained_variance?: number[]; quality?: ProjectionQuality; [k: string]: unknown }
/** POST api/projection (ready): a 2D layout of the boxes `id`. */
export interface ProjectionReady { ready: true; key: string; id: number[]; x: number[]; y: number[]; info: ProjectionInfo | null }
export type ProjectionResult = ProjectionReady | NotReady;
/** GET api/projections → views[] (saved projections). */
export interface SavedView {
  key?: string; feature: string; method: string; params: Record<string, number>; classes?: number[] | null;
  label: string; created?: string; last_used?: string; [k: string]: unknown;
}

/** POST api/groups response. */
export interface GroupsPayload {
  key: string;
  group: number[];                     // group index per projected point, -1 = outlier, -2 = removed box
  groups: Array<Record<string, unknown>>;
  n_outliers: number;
}
/** Client-side density groups (S.groups). */
export interface Groups { k: string; group: number[]; list: GroupsPayload['groups']; nOut: number; of: Map<number, number> }

/** GET api/tags: tag name -> box ids. */
export type Tags = Record<string, number[]>;
/** POST api/distances → { d, metric }. */
export interface DistancesPayload { d: number[][]; metric: string }
export type DistState = { key: string | null; pending: string } | { key: string; d: number[][]; metric: string; pending?: undefined };

// ------------------------------------------------------------------ samples / images / annotations
/** One label box as sent by GET api/sample/{id}. bbox is pixels [x1, y1, x2, y2]. */
export interface Sample {
  id?: number; image: string; session?: string; cls?: number; class_name?: string;
  bbox: [number, number, number, number]; img_w?: number; img_h?: number; area_px?: number;
  same_image: number[];
  // with scores:
  feature?: string; suspicion?: number; suggested?: number; suggested_name?: string; outlier?: number;
  label_quality?: {
    quality: number; issue: boolean; rank: number; given_prob: number; suggested: number; suggested_name: string;
    ood_feat: number | null; ood_prob: number | null; top: Array<{ cls: number; name: string; p: number }>;
  };
  [k: string]: unknown;
}
/** GET api/similar/{id} and POST api/text-search items. */
export interface SimilarItem { id: number; sim: number; [k: string]: unknown }
/** GET api/duplicates → pairs[]. */
export interface DuplicatePair { a: number; b: number; sim: number }
/** GET api/images. */
export interface ImagesPayload { total: number; bg: string[]; pending: Record<string, unknown> }

/** A label-page annotation (normalised 0..1 geometry). */
export type AnnotationKind = 'bbox' | 'polygon' | 'keypoints' | 'classify';
export interface Annotation {
  id: string;
  task_id?: string;
  kind?: AnnotationKind | string;
  cls?: number;
  class_id?: number;
  bbox?: [number, number, number, number];
  points?: Array<[number, number]>;
  score?: number | null;
  source?: string;
  [k: string]: unknown;
}
/** A class of a labeling task. */
export interface LabelClass { id?: number | string; name: string; color?: string | null; hotkey?: string | null }

// ------------------------------------------------------------------ sessions / datasets / models
export interface DatasetEntry {
  id: string; name: string; uri?: string; kind?: string; [k: string]: unknown;
}
export interface ModelEntry {
  id: string; name: string; predicted?: boolean; [k: string]: unknown;
}
/** GET api/models/{id}/eval (scored model); more fields are read by views/model.ts. */
export interface ModelEval {
  split: string;
  gt: { status: number[]; pred_cls: number[]; pred_score: number[] };
  [k: string]: unknown;
}

// ------------------------------------------------------------------ filter (core/filter.ts)
export type NameMode = 'substr' | 'glob' | 'regex' | 'fuzzy';
export type ClsMode = 'any' | 'all' | 'none';
/** The filter spec F (persisted in ui_settings as `filter`). */
export interface FilterSpec {
  scope: 'image' | 'box';
  name: { mode: NameMode; q: string; neg: boolean };
  cls: { mode: ClsMode; ids: number[] };
  nbox: Range; area: Range; aspect: Range; brightness: Range; blur: Range;
  session: string[]; tags: string[]; m_status: number[];
  suspicion_min: number | null; outlier_min: number | null;
  cl_quality_max: number | null; cl_issue: boolean;      // cleanlab: label quality at most, only label issues
  issues: string[];
  clip: { q: string; k: number };
}

// ------------------------------------------------------------------ app state (core/state.ts `S`)
export interface ModelState {
  id: string | null; conf: number; iou: number; split: string; list: ModelEntry[]; ev: ModelEval | null; showPreds: boolean;
}
/** One entry of the gallery's back stack (S.selStack). */
export interface SelEntry { ids: number[]; title: string; kind: 'samples' | 'pairs'; pairs: DuplicatePair[]; view: string | null | undefined }
export interface AppState {
  status: Status | null;
  meta: Meta | null;
  P: Points | null;                    // columnar points + current feature scores
  view: ProjectionReady | null;        // current projection
  viewPos?: Map<number, number>;       // point id -> index in view
  viewReq: ProjectionRequest | null;   // request that produced `view`
  scoresFeature: string | null;
  lq?: { feature: string; summary: LqSummary } | null;   // cleanlab label quality loaded into P.cl_* (null = none)
  hidden: Set<number>;                 // hidden class ids (All mode)
  sel: number[];
  selKind: 'samples' | 'pairs';
  selStack?: SelEntry[];
  activeView?: string | null;
  pairs: DuplicatePair[];
  selTitle: string;
  page: number;
  highlight: number[];
  picked: number[];                    // gallery cards clicked (ctrl+click): pinned as yellow dots on the plot
  sessionIdx: Map<string, number> | null;
  groups: Groups | null;
  tags: Tags;
  lasso: { x: number[]; y: number[] } | null;
  dimSet: Set<number> | null;          // ids inside the lasso (the rest is faded)
  dist: DistState | null;
  filterMask?: Uint8Array | null;      // filter bar result: 1 = shown
  edited: Set<string>;                 // images saved from the dialog this session
  model: ModelState;
  /** combined datasets only (views/compare.ts) */
  cmp?: CmpState | null;
  /** live feed: new boxes drawn elsewhere, placed in projection `proj` (core/live.ts; the 'live-new' trace) */
  liveNew?: LiveNew | null;
}

// ------------------------------------------------------------------ compare (routes/compare.py, views/compare.ts)
/** Matching parameters of a compare run. */
export interface CmpParams { iou_match?: number; iou_same?: number; name_key?: 'rel' | 'stem' | 'basename' }
export interface CmpImageCounts { only_a: number; only_b: number; same: number; labels_changed: number; pixels_changed: number; dup_a: number; dup_b: number }
export interface CmpBoxCounts { same: number; moved: number; reclass: number; removed: number; added: number; image_only_a: number; image_only_b: number }
export interface CmpClassRow { cls: number; a: number; b: number; added: number; removed: number; reclass_in: number; reclass_out: number }
/** POST api/compare/run → summary. a / b = member indices. confusion: row = class in A, column = class in B. */
export interface CmpSummary {
  a: number; b: number; a_alias: string; b_alias: string; params: CmpParams;
  images: CmpImageCounts; boxes: CmpBoxCounts; classes: CmpClassRow[]; confusion: number[][];
}
export interface CmpRunResult { ready: true; key: string; summary: CmpSummary }
export type CmpListKey = 'only_a' | 'only_b' | 'same' | 'labels_changed' | 'pixels_changed' | 'dup';
/** GET api/compare/result. */
export interface CmpResult { key: string; summary: CmpSummary; image_lists: Record<CmpListKey, string[]>; truncated: Partial<Record<CmpListKey, number>> }
/** One box of GET api/compare/image (xyxy normalised 0..1). */
export interface CmpImgBox { sid: number; cls: number; xyxy: [number, number, number, number]; status: number; pair: number; iou: number }
export interface CmpImgSide { name: string; origin: number; img_w: number | null; img_h: number | null; boxes: CmpImgBox[] }
/** GET api/compare/image. status: 1 only A, 2 only B, 3 same, 4 labels changed, 5 pixels changed. */
export interface CmpImage { status: number; how: number; dup: boolean; a: CmpImgSide | null; b: CmpImgSide | null }
/** POST api/compare/quickmap (ready): a projection + the similarity of each box to its nearest box of `ref`. */
export interface QuickMap extends ProjectionReady { novel: number[] }
export type QuickMapResult = QuickMap | NotReady;
/** GET api/union/status. */
export interface UnionStatus { union: boolean; stale?: boolean; members?: Array<{ alias: string; name: string; sid: string; n: number; stale: boolean }> }
export type OriginShow = 'a' | 'b' | 'both' | 'diff';
/** One compare run in the page: its key, summary, image lists and the per-box columns (sample id order). */
export interface CmpRun {
  key: string; summary: CmpSummary; lists: CmpResult['image_lists'] | null;
  status: Uint8Array; pair: Int32Array; iou: Float32Array;
}
/** The density-difference layer under the map (compare-core densityDiff). */
export interface CmpDensity { on: boolean; G: number; blur: number; novelty: number | null; nA: number; nB: number }
/** One provisional box of the live feed: a box drawn after the index, placed at (x, y) of projection `proj`. */
export interface LivePlaced { ann: string; proj: string; x: number; y: number; sim: number; cls: number; image: string }
export interface LiveNew { byAnn: Map<string, LivePlaced> }
/** S.cmp: a combined dataset's compare state (views/compare.ts owns it, plot/render.ts reads it). */
export interface CmpState {
  origin: Uint8Array;                  // member index per box
  a: number; b: number;                // the two members compared (indices)
  show: OriginShow;
  mask: Uint8Array | null;             // originMask(show): 1 = drawn; null = no origin filter
  run: CmpRun | null;
  density: CmpDensity;
}
