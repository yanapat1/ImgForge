// Types of the Settings dialog (static/ts/settings/*): the SET state, the forms, and the API payloads it reads.
// Pure types, no runtime code. Forms are `type` aliases (not interfaces) so the dialog's data-f="a.b.c" writer can
// view them as plain records. Each form carries a `form` tag, so `SET.form` narrows like a discriminated union.

// ------------------------------------------------------------------ shared
export type StatusKind = 'busy' | 'ok' | 'err' | '';
/** One status line (statusHTML). */
export interface StatusLine { kind: StatusKind; text: string }

export type SectionId = 'datasets' | 'connections' | 'labeling' | 'models' | 'llm' | 'appearance' | 'plot' | 'workspace' | 'account' | 'users';
export type SettingsView = 'list' | 'add' | 's3' | 'local' | 'labeling' | 'embeddings' | 'conn' | 'model' | 'llm-form' | 'llm-add';

/** openSettings options (the Sessions home opens the dialog in solo mode). */
export interface OpenSettingsOpts {
  solo?: boolean | undefined;
  view?: 'add' | 'labeling' | undefined;
  edit?: string | undefined;
  project?: string | undefined;
}

// ------------------------------------------------------------------ browsing
export interface FolderEntry { name: string; yolo?: boolean; embeddings?: boolean }
export interface YoloInfo { images: number; labels: number; classes: boolean; bytes?: number }
/** GET api/fs/browse, POST api/connections/browse */
export interface BrowseResult {
  path: string;
  parent?: string | null;
  folders: FolderEntry[];
  yolo?: boolean;
  info?: YoloInfo | null;
  embeddings?: boolean;
}
/** GET api/fs/embeddings: what an embeddings session on that folder would hold (data/embedding_source.py check). */
export interface EmbeddingsSummary {
  path: string; n: number; label: boolean; classes: number;
  features: Array<{ key: string; name: string; dim: number }>;
  previews: Array<'image' | 'audio' | 'video' | 'text'>;
  columns: Array<{ name: string; type: 'num' | 'cat' | 'text' }>;
}

// ------------------------------------------------------------------ connections
export type ConnKind = 's3' | 'ftp' | 'sftp' | 'postgres' | 'mssql' | 'model';
/** The editable copy of a connection (also a blank form). Secrets are write-only: blank on save = keep the stored one. */
export type ConnDraft = {
  id?: string | undefined; kind?: ConnKind | undefined; name?: string | undefined;
  endpoint?: string | undefined; region?: string | undefined; verify_ssl?: boolean | undefined;
  access_key?: string | undefined; secret_key?: string | undefined;
  host?: string | undefined; port?: number | string | null | undefined; username?: string | undefined;
  tls?: boolean | undefined; passive?: boolean | undefined; base_path?: string | undefined; private_key?: string | undefined;
  url?: string | undefined; api_key?: string | undefined; timeout_s?: number | string | null | undefined;
  database?: string | undefined; schema?: string | undefined; table_prefix?: string | undefined; encrypt?: boolean | undefined;
  // only on saved connections (the server never sends the secrets back)
  has_secret?: boolean | undefined; has_private_key?: boolean | undefined; has_api_key?: boolean | undefined;
};
/** A saved connection as the server lists it (GET api/connections → connections[]). */
export type ConnItem = ConnDraft & { id: string };
/** Answer of POST api/connections/test (fields depend on the kind). */
export interface ConnTestResult {
  ms?: number;
  protocol?: string;
  models?: Array<{ id?: string; name?: string; task_type?: string; classes?: unknown[] }>;
  buckets?: Array<string | { name: string }>;
  folders?: Array<string | { name: string }> | number;
  path?: string;
  images?: number;
  server_version?: string;
  tables_exist?: boolean;
  schema_version?: string | number;
}

// ------------------------------------------------------------------ datasets (sessions)
export interface DatasetItem {
  id: string; name: string;
  kind: 's3' | 'local' | 'labeling' | 'embeddings';
  uri?: string;
  modality?: string[];
  mode?: 'stream' | 'cache' | undefined;
  connection_id?: string; connection?: string | null;
  bucket?: string; prefix?: string; path?: string;
  project_id?: string; task_id?: string; only_done?: boolean;
  error?: string;
  exists?: boolean;
  mirror?: { path: string; files: number; bytes: number; synced: number };
  labeling?: { project?: string; task?: string; task_type?: string };
}
/** GET api/datasets */
export interface DatasetsPayload { datasets: DatasetItem[]; active: string | null }
/** GET api/connections */
export interface ConnectionsPayload { connections: ConnItem[] }

/** GET api/label/projects → projects[] (only what the form reads). */
export interface LabProjectTask { id: string; name: string; task_type: string; done?: number }
export interface LabProject { id: string; name: string; n_media?: number; tasks?: LabProjectTask[] }

// ------------------------------------------------------------------ forms (SET.form)
export type S3Form = {
  form: 's3';
  id?: string | undefined; connId: string; conn: ConnDraft;
  bucket?: string | undefined; prefix?: string | undefined; mode?: 'stream' | 'cache' | undefined; name?: string | undefined;
  buckets?: string[] | null | undefined; browse?: BrowseResult | null | undefined;
  connStatus?: StatusLine | null | undefined; locStatus?: StatusLine | null | undefined; saveStatus?: StatusLine | null | undefined;
};
export type LocalForm = {
  form: 'local';
  id?: string | undefined; path?: string | undefined; name?: string | undefined;
  browse?: BrowseResult | null | undefined;
  status?: StatusLine | null | undefined; saveStatus?: StatusLine | null | undefined;
};
export type EmbeddingsForm = {
  form: 'embeddings';
  id?: string | undefined; path?: string | undefined; name?: string | undefined;
  browse?: BrowseResult | null | undefined; summary?: EmbeddingsSummary | null | undefined;
  status?: StatusLine | null | undefined; saveStatus?: StatusLine | null | undefined;
};
export type LabelingForm = {
  form: 'labeling';
  id?: string | undefined; project_id: string; task_id: string; only_done: boolean; name: string; direct: boolean;
  projects: LabProject[] | null;
  status?: StatusLine | null | undefined; saveStatus?: StatusLine | null | undefined;
};
export type ConnForm = {
  form: 'conn';
  conn: ConnDraft;
  connStatus?: StatusLine | null | undefined;
};

// ------------------------------------------------------------------ models
export type ModelFamily = 'yolo' | 'rfdetr' | 'custom';
export type ModelFormat = 'pt' | 'pth' | 'pkl' | 'safetensors' | 'onnx' | 'openvino' | 'api';
export type ApiPreset = 'serving' | 'generic' | 'label';
export type BoxFormat = 'xyxy' | 'xywh' | 'cxcywh';

export type ModelMapping = {
  detections: string; box: string; score: string; class_id: string; class_name: string; box_format: string; normalized: boolean;
};
export type ModelApi = {
  preset: ApiPreset; url: string; version: string; provider: string; model_type: string; model_name: string;
  headers: string; api_key: string; label_model: string; auth_header: string; send: string; field: string;
  mapping: ModelMapping;
  timeout?: number | string;
  has_key?: boolean;
};
/** The model being edited (every field present: blankModel + the saved model on top). */
export type ModelDraft = {
  id?: string;
  name: string; family: ModelFamily; format: ModelFormat; path: string; variant: string; arch: string; adapter: string;
  imgsz: number | string; conf: number | string; iou: number | string; task_type: string;
  api: ModelApi;
};
/** A saved model as GET api/models lists it (a draft's fields may be missing). */
export type ModelItem = {
  id: string; name: string; family: ModelFamily; format: ModelFormat;
  path?: string; variant?: string; arch?: string; adapter?: string;
  imgsz?: number | string | null; conf?: number | string | null; iou?: number | string | null; task_type?: string;
  api?: Partial<Omit<ModelApi, 'mapping'>> & { mapping?: Partial<ModelMapping> };
  predicted?: boolean; exists?: boolean;
};
/** GET api/models/discover → found[] */
export interface FoundModel { name: string; family: ModelFamily; format: ModelFormat; path: string; size?: number; mtime: number }
export interface ModelFileEntry { name: string; size: number; format: ModelFormat | null }
/** GET api/models/browse */
export interface ModelBrowse {
  path: string; parent?: string | null;
  folders: Array<{ name: string; openvino?: boolean }>;
  files: ModelFileEntry[];
}
export interface CustomSpec {
  input: Record<string, string>;
  output_a: { title: string; shape: string; columns: string[]; units: string; example: unknown };
  output_b: { title: string; units: string; example: unknown };
  adapter: { title: string; meta: Record<string, unknown>; functions: Record<string, string> };
  classes: string;
  api_generic: { request: string; response_example: unknown; mapping: Record<string, string> };
}
/** GET api/models/spec */
export interface ModelSpec {
  custom: CustomSpec;
  matrix: Record<ModelFamily, ModelFormat[]>;
  families: string[]; formats: string[]; rfdetr_variants: string[];
  defaults: { conf: number; match_iou: number };
}
export interface ServingEntry { provider: string; model_type: string; model_name: string; backend?: string; imgsz: number | string; classes: number | string; versions: string[] }
export interface LabelModelEntry { id: string; name?: string; task_type?: string; classes?: unknown[] }

export type ModelForm = {
  form: 'model';
  model: ModelDraft;
  source: 'path' | 'upload';
  hasKey: boolean;
  browse?: ModelBrowse | null | undefined;
  status?: StatusLine | null | undefined; saveStatus?: StatusLine | null | undefined;
  upStatus?: StatusLine | null | undefined; apiStatus?: StatusLine | null | undefined;
  servingList?: ServingEntry[] | null | undefined; labelList?: LabelModelEntry[] | null | undefined;
};

// ------------------------------------------------------------------ AI assistant (llm)
export type PresetKey = 'openai' | 'anthropic' | 'gemini' | 'vllm' | 'ollama' | 'openrouter' | 'custom';
export type LlmProvider = {
  id: string; name: string; kind: string; base_url: string; model: string;
  vision?: boolean; audio?: boolean; box_format?: string; timeout_s?: number; max_output_tokens?: number; temperature?: number;
  verified_at?: number | null; verify?: { ms?: number } | null; last_error?: string | null;
  has_key?: boolean; key_env?: boolean; api_key_masked?: string | null;
};
/** GET api/llm/config (also the answer of every PUT / DELETE on it). */
export interface LlmConfig {
  providers: LlmProvider[];
  active: string | null;
  features: Record<string, boolean>;
  budget?: { max_input_tokens?: number };
}
export type LlmDraft = {
  id?: string;
  name: string; kind: string; base_url: string; model: string; api_key: string; vision: boolean; audio: boolean;
  box_format: string; timeout_s: number | string; max_output_tokens: number | string; temperature: number | string;
};
/** What is POSTed as a provider: trimmed strings, no api_key when blank (= keep the stored key). */
export type LlmPayload = Omit<LlmDraft, 'api_key'> & { api_key?: string };
export interface LlmTestResult { ok: boolean; ms?: number; text_ok?: boolean; vision_ok?: boolean; audio_ok?: boolean; model?: string; error?: string }
export type LlmForm = {
  form: 'llm';
  llm: LlmDraft;
  preset: PresetKey;
  hasKey: boolean; keyMasked: string; capsTouched: boolean; adv: boolean;
  models: string[] | null; modelsStatus: StatusLine | null; testStatus: StatusLine | null;
  lastTest: { ok: boolean; sig: string } | null; saveStatus: StatusLine | null;
};

export type SetForm = S3Form | LocalForm | EmbeddingsForm | LabelingForm | ConnForm | ModelForm | LlmForm;

// ------------------------------------------------------------------ the dialog state
export interface SetState {
  sec: SectionId;
  view: SettingsView;
  solo: boolean;
  datasets: DatasetItem[];
  conns: ConnItem[];
  models: ModelItem[];
  found: FoundModel[];
  spec: ModelSpec | null;
  active: string | null;
  llm: LlmConfig | null;
  llmError: string | null;
  form: SetForm | null;
  error: string | null;
}

/** A data-act handler (the clicked button, and the click event for the delegated listener). */
export type SetAction = (b: HTMLElement, e?: Event) => void | Promise<void>;
/** A data-act-change handler (the changed control). */
export type SetChange = (el: HTMLElement) => void | Promise<void>;

// ------------------------------------------------------------------ account + users (settings/account.ts, settings/users.ts)
/** A user as the admin list shows it (GET api/admin/users → users[]); times are epoch seconds. */
export interface AdminUser {
  id: string; username: string; email: string; name: string; is_admin: boolean; is_active: boolean; avatar_ver: number;
  created?: number | null; last_login?: number | null; n_sessions?: number;
}
/** GET / PUT api/admin/settings */
export interface AdminSettings { signup_enabled: boolean; who_can_add_connections: 'everyone' | 'admins' }
/** One row of GET api/admin/audit */
export interface AuditEvent {
  at: number | string; user: { id: string; username: string } | null; action: string; target?: string | null;
  meta?: unknown; ip?: string | null;
}

// ------------------------------------------------------------------ labeling settings
/** label_settings.json: scalar options + the hotkeys that differ from the defaults. */
export type LabSettings = {
  crosshair: boolean; tags: string; tag_content: string; auto_advance: boolean; advance_to: string; commit_scope: string;
  fill_opacity: number; stroke_width: number; tag_size: number; tag_opacity: number; stroke_opacity: number;
  line_style: string; handle_size: number; color_mode: string; single_color: string; selected_style: string;
  dim_other_tasks: boolean; bbox_mode: string;
  hotkeys: Record<string, string[]>;
};
