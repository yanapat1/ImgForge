// Shared types of the labeling page (static/ts/label/*): the server payloads (dataviz/labeling/README.md "API contract"),
// the in-memory annotation (discriminated by geometry_type), the page state L, the settings and the tool interface.
// Pure types: no runtime code.

// ------------------------------------------------------------------ server payloads
export type TaskType = 'object_detection' | 'segmentation' | 'keypoints' | 'classification';
export type GeometryType = 'bbox' | 'polygon' | 'keypoints' | 'tag';
export type MediaStatus = 'todo' | 'in_progress' | 'done' | 'skipped';
export type AnnSource = 'manual' | 'model' | 'import';

/** A model linked to a task (task.config.model): a Settings › Models model (registry_id) or a model server connection. */
export interface ModelLink {
  registry_id?: string | null;
  connection_id?: string | null;
  model_id: string;
  name?: string | null;
  version?: string | null;
  conf?: number;
  auto_on_open?: boolean;
  /** model class name -> task class id | 'new' | 'ignore' */
  class_map?: Record<string, string>;
}

export interface TaskConfig {
  keypoints?: string[];
  skeleton?: Array<[number, number]>;
  flip_idx?: number[];
  multi_label?: boolean;
  model?: ModelLink;
  [k: string]: unknown;
}

export interface TaskClass {
  id: string;
  task_id: string;
  name: string;
  color: string;
  position: number;
  hotkey: string | null;
}

export interface LabelTask {
  id: string;
  name: string;
  task_type: TaskType;
  position: number;
  config: TaskConfig;
  classes: TaskClass[];
}

export type LabelSource =
  | { kind: 'upload' }
  | { kind: 'local'; path: string; recursive?: boolean }
  | { kind: 's3'; connection_id: string; bucket: string; prefix?: string }
  | { kind: 'ftp' | 'sftp'; connection_id: string; path: string };

/** GET api/label/projects/{pid} */
export interface LabelProject {
  id: string;
  name: string;
  description?: string;
  created?: number;
  updated?: number;
  source?: LabelSource;
  export_default?: unknown;
  n_media?: number;
  review?: boolean;
  tasks: LabelTask[];
  /** ids of the tasks whose linked model is activated in this server session */
  model_active?: string[];
  /** my access to this session; 'viewer' = the page is read-only */
  my_role?: 'owner' | 'labeler' | 'viewer';
  [k: string]: unknown;
}

export type ReviewMode = 'manual' | 'bulk';
export interface Media {
  id: string;
  file_name: string;
  width: number;
  height: number;
  position?: number;
  status: Record<string, MediaStatus>;
  n: Record<string, number>;
  review?: Record<string, ReviewMode>;
  [k: string]: unknown;
}

// ------------------------------------------------------------------ geometry / annotations (normalised 0..1)
export type Pt = [number, number];
/** [x, y, v]: v 0 not placed, 1 occluded, 2 visible. */
export type KpPoint = [number, number, number];
export interface BBoxGeom { x: number; y: number; w: number; h: number }
export interface PolyGeom { points: Pt[]; holes?: Pt[][] }
export interface KpGeom { points: KpPoint[] }
export type TagGeom = Record<string, never>;

export interface AnnBase {
  id: string;
  task_id: string;
  class_id: string;
  attributes: Record<string, unknown>;
  parent_id: string | null;
  z_order: number;
  version?: number;
  score?: number | null;
  source?: AnnSource;
}
export interface BBoxAnn extends AnnBase { geometry_type: 'bbox'; geometry: BBoxGeom }
export interface PolygonAnn extends AnnBase { geometry_type: 'polygon'; geometry: PolyGeom }
export interface KeypointsAnn extends AnnBase { geometry_type: 'keypoints'; geometry: KpGeom }
export interface TagAnn extends AnnBase { geometry_type: 'tag'; geometry: TagGeom }
/** An annotation. Immutable by convention: an edit replaces the object. */
export type Ann = BBoxAnn | PolygonAnn | KeypointsAnn | TagAnn;
/** Annotation of a drawn (non-tag) geometry. */
export type ShapeAnn = BBoxAnn | PolygonAnn | KeypointsAnn;

/** GET api/label/projects/{pid}/annotations/{mid} */
export interface AnnotationsPayload { media: Media; versions: Record<string, number>; annotations: Ann[] }
/** PUT api/label/projects/{pid}/annotations/{mid} */
export interface SaveResult { version: number; status: string; n: number }

// ------------------------------------------------------------------ settings / hotkeys
export type TagsMode = 'all' | 'hover_selected' | 'off';
export type HotkeyMap = Record<string, string[]>;
export interface LabelSettings {
  crosshair: boolean;
  tags: TagsMode;
  tag_content: 'name' | 'name_index';
  auto_advance: boolean;
  advance_to: 'next' | 'unlabeled';
  commit_scope: 'active' | 'all';
  fill_opacity: number;
  stroke_width: number;
  tag_size: number;
  tag_opacity: number;
  stroke_opacity: number;
  line_style: 'solid' | 'dashed';
  handle_size: number;
  color_mode: 'class' | 'single';
  single_color: string;
  selected_style: 'halo' | 'thick';
  dim_other_tasks: boolean;
  bbox_mode: 'drag' | 'two_click';
  hotkeys: HotkeyMap;
}

// ------------------------------------------------------------------ history
/** [id, before | null (added), after | null (deleted)] */
export type Change = [string, Ann | null, Ann | null];
export interface HistoryEntry { changes: Change[]; merge: string | null; t: number }
export interface History { undo: HistoryEntry[]; redo: HistoryEntry[] }

// ------------------------------------------------------------------ page state L
export interface ViewState { scale: number; tx: number; ty: number; fitted?: boolean }
export type SaveState = 'saved' | 'saving' | 'offline' | 'conflict';
export interface LabelState {
  pid: string | null;
  project: LabelProject | null;
  ids: string[];
  idx: number;
  media: Media | null;
  anns: Map<string, Ann>;
  versions: Record<string, number>;
  taskId: string | null;
  classId: string | null;
  selected: Set<string>;
  hoverId: string | null;
  hidden: Set<string>;
  hideAll: boolean;
  view: ViewState;
  settings: LabelSettings;
  dirty: Set<string>;
  saveState: SaveState;
  history: History | null;
  editing: Map<string, Ann>;
  loading: boolean;
  cursor: { nx: number; ny: number } | null;
  /** a viewer of a shared session: browse only (no drawing, no status changes, no saves) */
  readOnly: boolean;
}

// ------------------------------------------------------------------ canvas / tools
/** A pointer position: screen css px (sx, sy) and normalised image coordinates (nx, ny). */
export interface PointerPos { sx: number; sy: number; nx: number; ny: number }
/** Normalised extent of a shape + its area (cached per Ann). */
export interface Bounds { x0: number; y0: number; x1: number; y1: number; area: number }
/** Class as the canvas draws it (CLS in canvas.ts). */
export interface ClassStyle {
  id: string; name: string; color: string; ink: string; stroke: string; fill: string; fill2: string; task_id: string;
}
export interface Suggestion { x1: number; y1: number; x2: number; y2: number; text?: string }
export interface TagRect { x: number; y: number; w: number; h: number; id: string; text: string; color: string; fg: 'dark' | 'light' }

/** A geometry tool; the canvas calls whichever members exist. */
export interface Tool {
  name?: string;
  coalesced?: boolean;
  onDown?(p: PointerPos, e: PointerEvent): void;
  onMove?(p: PointerPos, e: PointerEvent): void;
  onUp?(p: PointerPos, e: PointerEvent): void;
  onDbl?(p: PointerPos, e: MouseEvent): void;
  onKey?(combo: string): boolean;
  drawPreview?(x: CanvasRenderingContext2D): void;
  cursor?(p: PointerPos): string;
  busy?(): boolean;
  cancel?(): boolean;
  hint?(): string;
  finish?(): boolean;
  /** keypoints: toggle visible / occluded */
  visibility?(): boolean;
  reset?(): void;
}
