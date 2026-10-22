// Settings › Models: the saved models (list + models found in the project) and the add / edit form:
// 1 name + family · 2 file format · 3 where it comes from (server path, upload, or an HTTP API) · 4 options,
// plus, for Custom models and generic APIs, the exact output structure the model has to produce, and 5 the plot:
// any file model (or a Hugging Face backbone) can embed the boxes as a feature space, before the projection.

import { api, errText, getJSON, postJSON } from '../core/api.js';
import { $, $req, esc, icon } from '../core/dom.js';
import { setBar, waitJob } from '../core/jobs.js';
import type { CustomSpec, FoundModel, ModelEmbed, LabelModelEntry, ModelApi, ModelBrowse, ModelDraft, ModelFamily, ModelForm, ModelFormat, ModelItem, ModelSpec,
  ServingEntry, SetAction, SetChange } from '../types-settings.js';
import { refreshStatus } from '../views/controls.js';
import { modelsChanged } from '../views/model.js';
import { SET, renderSettings } from './dialog.js';
import { dd, fmtBytes, formOf, rowAttr, setDisabled, statusHTML, tagc } from './forms.js';

const FAMILIES: Record<ModelFamily, [string, string]> = {
  yolo: ['YOLO', 'ultralytics detect models (YOLOv5u / v8 / 11 / 26 …). Class names come from the weights.'],
  rfdetr: ['RF-DETR', 'Roboflow RF-DETR nano … 2xlarge: rfdetr checkpoints, or this repo’s train_rfdetr.py runs.'],
  custom: ['Custom', 'Any other detector. It must give boxes in the structure shown below, or use a small Python adapter.'],
  hf: ['Hugging Face', 'An image backbone (e.g. facebook/dinov2-base, or your fine-tuned folder): embeds boxes for the plot, does not detect.'],
};
const FORMATS: Record<ModelFormat, [string, string]> = {
  pt: ['.pt', 'PyTorch checkpoint / TorchScript'], pth: ['.pth', 'PyTorch weights / state dict'],
  pkl: ['.pkl', 'pickled model (trusted files only)'], safetensors: ['.safetensors', 'weights only: needs the architecture'],
  onnx: ['ONNX', '.onnx graph (onnxruntime, GPU if available)'], openvino: ['OpenVINO', 'folder with .xml + .bin (CPU)'],
  api: ['API', 'a model served over HTTP'],
  hf: ['Hugging Face', 'hub id (owner/name) or a model folder'],
};
const POOLS: Array<[string, string]> = [['avg', 'average (feature map / tokens)'], ['cls', 'first token (CLS)'], ['flatten', 'flatten everything']];
const blankEmbed = (): ModelDraft['embed'] => ({ enabled: false, layer: '', pool: 'avg', output: '', imgsz: '', batch_size: '' });
const VARIANT_HELP = 'auto = read from the checkpoint (this repo’s runs store it) or the file name';

const isFormat = (x: string): x is ModelFormat => x in FORMATS;

const ago = (t: number): string => { const s = Date.now() / 1000 - t; return s < 5400 ? `${Math.max(1, Math.round(s / 60))} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`; };

export async function loadModelsData(): Promise<void> {
  const [m, f] = await Promise.all([getJSON<{ models: ModelItem[] }>('api/models'), getJSON<{ found: FoundModel[] }>('api/models/discover')]);
  SET.models = m.models;
  SET.found = f.found;
  if (!SET.spec) SET.spec = await getJSON<ModelSpec>('api/models/spec');
}

// ---------------------------------------------------------------- list
// Settings › Models is global (home, label page, visualizer): dataset-only parts (Run, "run on this dataset") need one
const onDataset = (): boolean => document.documentElement.dataset.screen === 'viz';

export function renderModelsSec(): string {
  if (SET.view === 'model') return renderModelForm();
  const viz = onDataset();
  const cards = (SET.models || []).map((m) => {
    const isApi = m.format === 'api';
    const a = m.api;
    const tags = [tagc(FAMILIES[m.family][0], 'on'), tagc(FORMATS[m.format][0])];
    if (isApi && a?.preset === 'label') tags.push(tagc(`labeling API · ${(m.task_type || 'object_detection').replace('_', ' ')}`));
    if (viz && m.family !== 'hf') tags.push(m.predicted ? tagc('run on this dataset', 'ok', 'check') : tagc('not run on this dataset'));
    if (m.exists === false) tags.push(tagc('file not found', 'bad', 'alert'));
    const embeds = m.family === 'hf' || !!m.embed?.enabled;
    if (embeds) tags.push(tagc('feature space in the plot', 'ok', 'scatter'));
    if (isApi && a?.has_key) tags.push(tagc('API key stored', '', 'check'));
    const where = isApi ? (a?.preset === 'serving' ? `${a.url} · ${a.provider}/${a.model_type}/${a.model_name}` : a?.url) : m.path;
    return `<div class="ds-card" data-mid="${esc(m.id)}"><div class="ds-icon${isApi ? ' s3' : ''}">${icon(isApi ? 'cloud' : 'model')}</div>` +
      `<div class="ds-main"><div class="ds-title">${esc(m.name)}</div><div class="ds-uri" title="${esc(where)}">${esc(where)}</div>` +
      `<div class="ds-meta">${tags.join('')}</div></div><div class="ds-actions">` +
      (m.family === 'hf' ? '' : `<button data-act="test-model" title="Load it and predict one image">${icon('plug')}Test</button>`) +
      (embeds && viz ? `<button data-act="test-embed" title="Embed four boxes of this dataset: the embedding size and speed">${icon('scatter')}Test embeddings</button>` : '') +
      (m.predicted || !viz || m.family === 'hf' ? '' : `<button class="primary" data-act="run-model" title="Predict every image of the open dataset">${icon('play')}Run</button>`) +
      `<button class="icon-btn ghost" data-act="edit-model" aria-label="Edit" title="Edit">${icon('edit')}</button>` +
      `<button class="icon-btn ghost" data-act="remove-model" aria-label="Remove" title="Remove (uploaded files are deleted, files elsewhere are not)">${icon('trash')}</button>` +
      `</div><div class="ds-progress" id="mst-${esc(m.id)}"></div></div>`;
  }).join('') || '<p class="sec-note">No models yet. Add one, or pick a trained run found in this project below.</p>';
  const found = (SET.found || []).slice(0, 24).map((f, i) =>
    `<div class="ds-card"><div class="ds-icon">${icon('model')}</div><div class="ds-main"><div class="ds-title">${esc(f.name)}</div>` +
    `<div class="ds-uri" title="${esc(f.path)}">${esc(f.path)}</div><div class="ds-meta">${tagc(FAMILIES[f.family][0], 'on')}${tagc(FORMATS[f.format][0])}` +
    `${f.size ? tagc(fmtBytes(f.size)) : ''}${tagc(ago(f.mtime))}</div></div>` +
    `<div class="ds-actions"><button data-act="add-found" data-i="${i}">${icon('plus')}Add</button></div></div>`).join('');
  return `<section><div class="sec-actions"><button class="primary" data-act="new-model">${icon('plus')}Add model</button>` +
    '<span class="sec-note">Models are shared by every dataset and by the labeling page (link one to a task, then activate it there); ' +
    'predictions are cached per dataset. Use them in the <b>Model</b> tab, and (with “Use in the plot”) as a feature space under <b>Features › My models</b>.</span></div>' +
    cards + (found ? `<h4 class="m-found-h">Found in this project</h4>${found}` : '') + '</section>';
}

// ---------------------------------------------------------------- form
function blankModel(): ModelDraft {
  return { name: '', family: 'yolo', format: 'pt', path: '', variant: '', arch: '', adapter: '', imgsz: '', conf: '', iou: '',
    task_type: 'object_detection', embed: blankEmbed(),
    api: { preset: 'serving', url: '', version: 'v1', provider: '', model_type: '', model_name: '', headers: '', api_key: '', label_model: '',
      auth_header: 'Authorization', send: 'multipart', field: 'file',
      mapping: { detections: 'detections', box: 'box', score: 'score', class_id: 'class_id', class_name: 'class_name', box_format: 'xyxy', normalized: false } } };
}

function openForm(m: ModelItem | null | undefined): void {
  SET.view = 'model';
  SET.sec = 'models';
  const base = blankModel();
  const ma = m?.api;
  const model: ModelDraft = m ? { ...base, ...m, imgsz: m.imgsz ?? '', conf: m.conf ?? '', iou: m.iou ?? '',
    embed: { ...base.embed, ...(m.embed ?? {}), imgsz: m.embed?.imgsz ?? '', batch_size: m.embed?.batch_size ?? '' },
    api: { ...base.api, ...(ma ?? {}), api_key: '', mapping: { ...base.api.mapping, ...(ma?.mapping ?? {}) } } } : base;
  SET.form = { form: 'model', model, source: 'path', hasKey: !!(ma && ma.has_key) };
  renderSettings();
  if (model.format !== 'api' && model.format !== 'hf') void browse(model.path ? model.path.replace(/\/[^/]*$/, '') : '');
}

async function browse(path: string | null | undefined): Promise<void> {
  const f = formOf('model');
  try {
    f.browse = await getJSON<ModelBrowse>(`api/models/browse?path=${encodeURIComponent(path || '')}`);
    f.status = null;
  } catch (e) { f.status = { kind: 'err', text: errText(e) }; }
  renderSettings();
}

const step = (n: number | string, title: string, inner: string, done = false): string =>
  `<div class="form-card${done ? ' done' : ''}"><div class="step-head"><span class="step-n">${done ? '✓' : n}</span>${title}</div>${inner}</div>`;

interface FldOpts { ph?: string; help?: string; mono?: boolean; type?: string; extra?: string }
const fld = (label: string, path: string, val: unknown, { ph = '', help = '', mono = false, type = 'text', extra = '' }: FldOpts = {}): string =>
  `<label class="fld${mono ? ' mono' : ''}">${label}<input type="${type}" data-f="${path}" value="${esc(val == null ? '' : val)}" placeholder="${esc(ph)}" ${extra}>` +
  `${help ? `<span class="help">${help}</span>` : ''}</label>`;

const sel = (label: string, path: string, val: unknown, opts: Array<[string, string]>, help = ''): string => `<label class="fld">${label}<select data-f="${path}" data-act-change="model-rerender">` +
  opts.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(val) ? 'selected' : ''}>${esc(l)}</option>`).join('') +
  `</select>${help ? `<span class="help">${help}</span>` : ''}</label>`;

function renderModelForm(): string {
  const f = formOf('model'), m = f.model, spec = SET.spec;
  const editing = !!m.id;
  // 1 name + family
  const fam = '<div class="radio-cards">' + Object.entries(FAMILIES).map(([k, [t, d]]) =>
    `<label class="radio-card${m.family === k ? ' sel' : ''}"><input type="radio" name="mfam" value="${k}" data-f="model.family" data-act-change="model-rerender" ${m.family === k ? 'checked' : ''}>` +
    `<div><b>${icon(k === 'custom' ? 'edit' : 'model')}${t}</b><p>${esc(d)}</p></div></label>`).join('') + '</div>';
  const s1 = fld('Name', 'model.name', m.name, { ph: 'e.g. yolo11n-21 val-best' }) + fam;
  // 2 format
  const allowed: readonly string[] = spec ? spec.matrix[m.family] : Object.keys(FORMATS);
  const s2 = '<div class="fmt-grid">' + Object.entries(FORMATS).map(([k, [t, d]]) =>
    `<button class="fmt${m.format === k ? ' sel' : ''}" data-act="pick-format" data-v="${k}" ${allowed.includes(k) ? '' : 'disabled'} aria-pressed="${m.format === k}">` +
    `<b>${t}</b><span>${esc(d)}</span></button>`).join('') + '</div>' +
    (['pkl', 'pt', 'pth'].includes(m.format) ? '<p class="sec-note">.pkl and full PyTorch checkpoints run code when loaded: only add files you trust. ' +
      'Files must be inside the project (or config <code>models.allowed_roots</code>) or uploaded here.</p>' : '');
  // 3 source
  const s3 = m.format === 'api' ? apiHTML(m.api, f) : m.format === 'hf' ? hfHTML(m) : sourceHTML(f);
  // 4 options
  const s4 = optionsHTML(m);
  const needSpec = m.family === 'custom' || (m.format === 'api' && m.api.preset === 'generic');
  return '<section>' + step(1, 'Name and model family', s1, !!m.name) + step(2, 'File format', s2, true) +
    step(3, m.format === 'api' ? 'API endpoint' : m.format === 'hf' ? 'Hugging Face model' : 'Model file', s3, m.format === 'api' ? !!m.api.url : !!m.path) +
    (m.family === 'hf' ? '' : step(4, 'Options', s4)) + (m.format === 'api' ? '' : step(m.family === 'hf' ? 4 : 5, 'Use in the plot (embeddings)', embedHTML(m), m.family === 'hf' || m.embed.enabled)) +
    (needSpec && spec ? step('?', 'Required model output', specHTML(spec.custom, m)) : '') +
    `<div class="form-foot">${statusHTML(f.saveStatus)}<span class="grow"></span><button class="ghost" data-act="back">Cancel</button>` +
    `<button data-act="save-model">${icon('check')}${editing ? 'Save' : 'Add'}</button>` +
    `<button class="primary" data-act="save-model" data-test="1">${icon('plug')}${editing ? 'Save' : 'Add'} &amp; test</button></div></section>`;
}

function sourceHTML(f: ModelForm): string {
  const m = f.model, b = f.browse;
  const seg = `<div class="segmented" role="group"><button class="view-btn${f.source === 'path' ? ' active' : ''}" data-act="model-source" data-v="path">${icon('folder')}On the server</button>` +
    `<button class="view-btn${f.source === 'upload' ? ' active' : ''}" data-act="model-source" data-v="upload">${icon('upload')}Upload</button></div>`;
  if (f.source === 'upload') {
    const ov = m.format === 'openvino';
    return seg + `<div class="sec-actions"><input id="m-upload" type="file" ${ov ? 'multiple accept=".xml,.bin"' : 'accept=".pt,.pth,.pkl,.pickle,.safetensors,.onnx,.torchscript"'}>` +
      `<button data-act="upload-model">${icon('upload')}Upload</button></div>` +
      `<p class="sec-note">${ov ? 'Choose the .xml and the .bin together.' : 'Stored in the workspace (models/&lt;id&gt;/); removing the model deletes it.'}</p>` +
      statusHTML(f.upStatus) + (m.path ? `<div class="sec-note mono">${esc(m.path)}</div>` : '');
  }
  const parts = b ? b.path.split('/').filter(Boolean) : [];
  const files = b ? b.files.filter((x) => x.format || /\.(ya?ml|py)$/.test(x.name)) : [];
  return seg + `<div class="sec-actions"><input class="txt mono" style="flex:1" type="text" data-f="model.path" value="${esc(m.path || '')}" placeholder="runs/train/…/weights/best.pt" data-enter="go-model-path">` +
    `<button data-act="go-model-path">${icon('arrow-r')}Go</button>${b && b.parent ? `<button class="icon-btn" data-act="up-model-path" aria-label="Parent folder" title="Parent folder">${icon('up')}</button>` : ''}</div>` +
    (b ? `<div class="browser"><div class="crumbs"><button data-act="model-crumb" data-to="">/</button>${parts.map((p, i) =>
      `<span class="sep">/</span><button data-act="model-crumb" data-to="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</button>`).join('')}</div>` +
      '<div class="folders">' + b.folders.map((d) => `<button class="folder${d.openvino ? ' yolo' : ''}" data-act="${d.openvino ? 'pick-model-dir' : 'model-folder'}" data-name="${esc(d.name)}">` +
        `${icon('folder')}<span class="name">${esc(d.name)}</span>${d.openvino ? tagc('OpenVINO', 'on', 'check') : ''}${icon('chev-r')}</button>`).join('') +
      files.map((x) => `<button class="folder yolo" data-act="pick-model-file" data-name="${esc(x.name)}">${icon('model')}<span class="name">${esc(x.name)}</span>` +
        `${tagc(fmtBytes(x.size))}${x.format ? tagc(FORMATS[x.format][0], 'on') : ''}</button>`).join('') +
      (!b.folders.length && !files.length ? '<div class="none">nothing here</div>' : '') + '</div></div>' : '') +
    statusHTML(f.status || (m.path ? { kind: 'ok', text: `selected: ${m.path}` } : { kind: '', text: 'Pick the weights file (or an OpenVINO folder).' }));
}

function hfHTML(m: ModelDraft): string {
  return '<div class="form-grid">' + `<label class="fld full mono">Hub id or folder<input type="text" data-f="model.path" value="${esc(m.path || '')}" placeholder="facebook/dinov2-base"></label>` +
    '</div><p class="sec-note">A hub id is downloaded on first use (safetensors only, no remote code), so anyone may add one. A folder on the server ' +
    '(your fine-tuned <code>save_pretrained</code> output) needs an admin. Image size, mean and std come from its preprocessor config.</p>';
}

// 5: the model as a feature space of the plot (projected with UMAP / PaCMAP / t-SNE / PCA, scored by k-NN and cleanlab)
function embedHTML(m: ModelDraft): string {
  const e: ModelEmbed = m.embed, hf = m.family === 'hf';
  const exported = m.format === 'onnx' || m.format === 'openvino';
  const on = hf || e.enabled;
  const g: string[] = [];
  if (!hf) {
    g.push(`<label class="fld full"><span class="check" style="margin:0"><input type="checkbox" data-f="model.embed.enabled" data-act-change="model-rerender" ${e.enabled ? 'checked' : ''}> ` +
      'Offer this model under <b>Features › My models</b>: its embedding of every box is projected, searched and scored like DINOv2 or CLIP</span></label>');
  }
  if (on) {
    if (exported) {
      g.push(fld('Graph output', 'model.embed.output', e.output, { mono: true, ph: '0 or e.g. embeddings',
        help: 'the output (name or number) holding the embedding; export one if the graph only has detections' }));
    } else if (!hf) {
      g.push(fld('Layer', 'model.embed.layer', e.layer, { mono: true, ph: m.family === 'rfdetr' ? 'backbone' : m.family === 'yolo' ? 'empty = YOLO embed()' : 'e.g. backbone or model.9',
        help: m.family === 'custom' ? 'a module path to hook; empty = the model output (or adapter embed())' : 'module path to hook (model.named_modules()); empty = the default' }));
    }
    g.push(sel('Pooling', 'model.embed.pool', e.pool, POOLS, 'how a feature map or a token sequence becomes one vector'));
    g.push(fld('Crop size (px)', 'model.embed.imgsz', e.imgsz, { type: 'number', ph: hf ? 'from the preprocessor' : '224', help: 'box crops are resized to this' }));
    g.push(fld('Batch size', 'model.embed.batch_size', e.batch_size, { type: 'number', ph: 'auto' }));
  }
  return `<div class="form-grid">${g.join('')}</div>` + (on ? '<p class="sec-note">Changing the weights or these settings computes a new feature space; ' +
    'after saving, pick it in the plot’s projection menu. The k-NN checks and cleanlab’s label-quality check then run on your embeddings.</p>' : '');
}

function apiHTML(a: ModelApi, f: ModelForm): string {
  const preset = '<div class="radio-cards">' +
    `<label class="radio-card${a.preset === 'serving' ? ' sel' : ''}"><input type="radio" name="mapi" value="serving" data-f="model.api.preset" data-act-change="model-rerender" ${a.preset === 'serving' ? 'checked' : ''}>` +
    `<div><b>${icon('cloud')}This repo’s serving app</b><p>serving/ (FastAPI): POST /{version}/{provider}/{type}/{name}/predict. Class names are read from the server.</p></div></label>` +
    `<label class="radio-card${a.preset === 'generic' ? ' sel' : ''}"><input type="radio" name="mapi" value="generic" data-f="model.api.preset" data-act-change="model-rerender" ${a.preset === 'generic' ? 'checked' : ''}>` +
    `<div><b>${icon('plug')}Any HTTP endpoint</b><p>Say how the image is sent and where boxes, scores and classes are in the JSON reply.</p></div></label>` +
    `<label class="radio-card${a.preset === 'label' ? ' sel' : ''}"><input type="radio" name="mapi" value="label" data-f="model.api.preset" data-act-change="model-rerender" ${a.preset === 'label' ? 'checked' : ''}>` +
    `<div><b>${icon('robot')}Labeling model server</b><p>The dataviz-label/1 API (GET /info, POST /predict): boxes, polygons, keypoints or tags. Labels images and is scored here.</p></div></label></div>`;
  const auth = '<div class="form-grid">' +
    fld('API key', 'model.api.api_key', a.api_key, { type: 'password', ph: f.hasKey ? 'unchanged (stored on the server)' : 'optional', extra: 'autocomplete="new-password"' }) +
    fld('Key header', 'model.api.auth_header', a.auth_header, { mono: true, help: 'Authorization adds "Bearer "; or e.g. X-API-Key' }) +
    `<label class="fld full mono">Extra headers<textarea data-f="model.api.headers" rows="2" placeholder="Header-Name: value (one per line)">${esc(a.headers || '')}</textarea></label></div>`;
  if (a.preset === 'label') {
    const ms = f.labelList ? `<div class="folders">${f.labelList.map((x, i) => `<button class="folder${x.id === a.label_model ? ' yolo' : ''}" data-act="pick-label" data-i="${i}">` +
      `${icon('model')}<span class="name">${esc(x.name || x.id)}</span>${tagc(x.id)}${tagc((x.task_type || '').replace('_', ' '))}${tagc(`${(x.classes || []).length} classes`)}</button>`).join('') || '<div class="none">the server lists no models</div>'}</div>` : '';
    return preset + '<div class="sec-actions">' + `<input class="txt mono" style="flex:1" type="url" data-f="model.api.url" value="${esc(a.url)}" placeholder="http://gpu-box:8000" data-enter="label-list">` +
      `<button data-act="label-list">${icon('search')}List models</button></div>` + ms + statusHTML(f.apiStatus) +
      '<div class="form-grid">' + fld('Model id on the server', 'model.api.label_model', a.label_model, { mono: true, help: 'empty = the server’s only model' }) +
      sel('Labels', 'model.task_type', f.model.task_type || 'object_detection', [['object_detection', 'boxes (object detection)'], ['segmentation', 'polygons (segmentation)'],
        ['keypoints', 'keypoints (pose)'], ['classification', 'tags (classification)']]) +
      fld('API key', 'model.api.api_key', a.api_key, { type: 'password', ph: f.hasKey ? 'unchanged (stored on the server)' : 'optional, sent as Bearer', extra: 'autocomplete="new-password"' }) +
      fld('Timeout (s)', 'model.api.timeout', a.timeout, { mono: true, ph: '60' }) + '</div>' +
      '<p class="sec-note">The visualizer scores its boxes (and the extent of its polygons). See dataviz/labeling/MODEL_API.md.</p>';
  }
  if (a.preset === 'serving') {
    const list = f.servingList ? `<div class="folders">${f.servingList.map((s, i) => `<button class="folder${s.model_name === a.model_name && s.provider === a.provider ? ' yolo' : ''}" data-act="pick-serving" data-i="${i}">` +
      `${icon('model')}<span class="name">${esc(`${s.provider}/${s.model_type}/${s.model_name}`)}</span>${tagc(s.backend || '')}${tagc(`${s.imgsz}px`)}${tagc(`${s.classes} classes`)}</button>`).join('') || '<div class="none">no models served</div>'}</div>` : '';
    return preset + '<div class="sec-actions">' + `<input class="txt mono" style="flex:1" type="url" data-f="model.api.url" value="${esc(a.url)}" placeholder="http://gpu-server:8000" data-enter="serving-list">` +
      `<button data-act="serving-list">${icon('search')}List models</button></div>` + list + statusHTML(f.apiStatus) +
      '<div class="form-grid">' + fld('Version', 'model.api.version', a.version, { mono: true }) + fld('Provider', 'model.api.provider', a.provider, { mono: true, ph: 'yolo' }) +
      fld('Model type', 'model.api.model_type', a.model_type, { mono: true, ph: 'obd' }) + fld('Model name', 'model.api.model_name', a.model_name, { mono: true, ph: 'yolo11n-640' }) + '</div>' + auth;
  }
  const mp = a.mapping;
  return preset + '<div class="form-grid">' + `<label class="fld full mono">Endpoint URL (POST)<input type="url" data-f="model.api.url" value="${esc(a.url)}" placeholder="https://my-server/detect"></label>` +
    sel('Send the image as', 'model.api.send', a.send, [['multipart', 'multipart form field'], ['raw', 'raw JPEG body'], ['json', 'JSON {field: base64}']]) +
    fld('Field name', 'model.api.field', a.field, { mono: true, help: 'multipart field, or the JSON key for base64' }) + '</div>' +
    '<div class="step-head" style="margin-top:12px">Where the boxes are in the reply</div><div class="form-grid">' +
    fld('List of detections', 'model.api.mapping.detections', mp.detections, { mono: true, help: 'dotted path, e.g. detections or result.0.objects' }) +
    fld('Box key', 'model.api.mapping.box', mp.box, { mono: true }) + fld('Score key', 'model.api.mapping.score', mp.score, { mono: true }) +
    fld('Class id key', 'model.api.mapping.class_id', mp.class_id, { mono: true, help: 'leave empty to use names only' }) +
    fld('Class name key', 'model.api.mapping.class_name', mp.class_name, { mono: true, help: 'matched to dataset classes by name' }) +
    sel('Box format', 'model.api.mapping.box_format', mp.box_format, [['xyxy', 'x1, y1, x2, y2'], ['xywh', 'x, y, w, h (top-left)'], ['cxcywh', 'cx, cy, w, h (centre)']]) +
    `<label class="fld" style="justify-content:flex-end"><span class="check" style="margin:0"><input type="checkbox" data-f="model.api.mapping.normalized" ${mp.normalized ? 'checked' : ''}> Boxes are 0–1 of the image size</span></label>` +
    '</div>' + auth;
}

function optionsHTML(m: ModelDraft): string {
  const g: string[] = [];
  if (m.family === 'rfdetr' && m.format !== 'api') {
    g.push(sel('Size (variant)', 'model.variant', m.variant || '', [['', 'auto'], ...(SET.spec?.rfdetr_variants ?? []).map((v): [string, string] => [v, v])], VARIANT_HELP));
  }
  if (m.format !== 'api' && m.format !== 'hf') {
    g.push(fld('Input size (px)', 'model.imgsz', m.imgsz, { type: 'number', ph: m.family === 'custom' ? '640' : 'auto',
      help: m.family === 'custom' ? 'square letterbox the model expects' : 'auto = training size from the weights' }));
  }
  if (m.family === 'yolo' && m.format !== 'api') g.push(fld('NMS IoU', 'model.iou', m.iou, { type: 'number', ph: '0.7', extra: 'step="0.05" min="0" max="1"' }));
  g.push(fld('Default confidence', 'model.conf', m.conf, { type: 'number', ph: String(SET.spec ? SET.spec.defaults.conf : 0.25),
    extra: 'step="0.05" min="0" max="1"', help: 'where the Model tab starts (change it live there)' }));
  if (['safetensors', 'pth', 'pkl'].includes(m.format) && m.family === 'yolo') {
    g.push(fld('Architecture (for weights-only files)', 'model.arch', m.arch, { mono: true, ph: 'weights/yolo11n.pt or yolo11n.yaml',
      help: 'the yaml or the .pt the weights were trained from' }));
  }
  if (m.family === 'custom' && m.format !== 'api') {
    g.push(fld('Adapter .py (optional)', 'model.adapter', m.adapter, { mono: true, ph: 'scripts/my_adapter.py',
      help: m.format === 'safetensors' || m.format === 'pth' ? 'needed for a state dict: build_model() builds the network' : 'only if the output does not follow the structure below' }));
  }
  return `<div class="form-grid">${g.join('')}</div>`;
}

function specHTML(spec: CustomSpec, m: ModelDraft): string {
  const pre = (o: unknown) => `<pre class="spec-code">${esc(JSON.stringify(o, null, 1).replace(/\n\s*(\d|-)/g, ' $1').replace(/\n\s*\]/g, ']'))}</pre>`;
  if (m.format === 'api') {
    const g = spec.api_generic;
    return `<p class="sec-note">${esc(g.request)}. The reply must be JSON; the paths above say where each value is. Example reply that works with the default paths:</p>` +
      pre(g.response_example) + '<dl class="kv">' + Object.entries(g.mapping).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('') + '</dl>';
  }
  const inp = spec.input;
  return '<dl class="kv">' + Object.entries(inp).map(([k, v]) => `<dt>input ${esc(k)}</dt><dd>${esc(v)}</dd>`).join('') + '</dl>' +
    `<p class="sec-note"><b>Output A — ${esc(spec.output_a.title)}</b>: shape <code>${esc(spec.output_a.shape)}</code>, columns ` +
    `<code>${spec.output_a.columns.join(', ')}</code>, ${esc(spec.output_a.units)}.</p>` + pre(spec.output_a.example) +
    `<p class="sec-note"><b>Output B — ${esc(spec.output_b.title)}</b>: ${esc(spec.output_b.units)}.</p>` + pre(spec.output_b.example) +
    `<p class="sec-note"><b>${esc(spec.adapter.title)}</b>: a .py file with any of these functions (meta: ${Object.keys(spec.adapter.meta).join(', ')}).</p>` +
    '<ul class="spec-fns">' + Object.entries(spec.adapter.functions).map(([k, v]) => `<li><code>${esc(k)}</code><span>${esc(v)}</span></li>`).join('') + '</ul>' +
    `<p class="sec-note">${esc(spec.classes)}</p>` +
    `<div class="sec-actions"><a class="btn-link" href="api/models/adapter_template.py" download>${icon('download')}Download adapter template</a></div>`;
}

// ---------------------------------------------------------------- actions (merged into SET_ACTIONS)
/** POST api/models/{id}/test: no dataset open = the model's classes only, else one predicted image. */
type ModelTest = { no_session: true; names: string[] } | {
  no_session?: false | undefined; unmatched: string[]; n: number; conf: number; image: string; n_labels: number; ms: number; load_ms: number; mapping: string;
};

async function testModel(id: string, el: HTMLElement): Promise<boolean> {
  el.innerHTML = statusHTML({ kind: 'busy', text: 'loading the model and predicting one image…' });
  try {
    const r = await postJSON<ModelTest>(`api/models/${encodeURIComponent(id)}/test`, {});
    if (r.no_session) {                     // no dataset open: it loaded / answered; its classes
      el.innerHTML = statusHTML({ kind: 'ok', text: `works · ${r.names.length} class${r.names.length === 1 ? '' : 'es'}${r.names.length ? `: ${r.names.slice(0, 12).join(', ')}${r.names.length > 12 ? '…' : ''}` : ' (named by its answers)'}` });
      return true;
    }
    const warn = r.unmatched.length ? ` · not in this dataset: ${r.unmatched.join(', ')}` : '';
    el.innerHTML = statusHTML({ kind: r.unmatched.length ? 'err' : 'ok',
      text: `${r.n} boxes ≥ ${r.conf} on ${r.image} (${r.n_labels} labelled) · ${r.ms} ms/img · loaded in ${(r.load_ms / 1000).toFixed(1)} s · classes ${r.mapping}${warn}` });
    return true;
  } catch (e) { el.innerHTML = statusHTML({ kind: 'err', text: errText(e) }); return false; }
}

/** POST api/models/{id}/test-embed: embed 4 boxes of the open dataset. */
async function testEmbed(id: string, el: HTMLElement): Promise<boolean> {
  if (!onDataset()) { el.innerHTML = statusHTML({ kind: 'err', text: 'open a visualize session to test embeddings on its boxes' }); return false; }
  el.innerHTML = statusHTML({ kind: 'busy', text: 'loading the model and embedding four boxes (a hub model downloads first)…' });
  try {
    const r = await postJSON<{ dim: number; n: number; ms: number }>(`api/models/${encodeURIComponent(id)}/test-embed`, {});
    el.innerHTML = statusHTML({ kind: 'ok', text: `works · ${r.dim}-dimensional embeddings · ${r.n} boxes in ${r.ms} ms · pick it under Features › My models` });
    return true;
  } catch (e) { el.innerHTML = statusHTML({ kind: 'err', text: errText(e) }); return false; }
}

/** The model as POSTed: the api block only for HTTP models. */
function payload(): Omit<ModelDraft, 'api'> & { api?: ModelApi } {
  const { api: a, embed, ...rest } = formOf('model').model;
  const num = (v: unknown): number | null => (v === '' || v == null || !Number.isFinite(+v) ? null : +v);
  const e = { ...embed, imgsz: num(embed.imgsz), batch_size: num(embed.batch_size) };
  return rest.format === 'api' ? { ...rest, embed: e, api: a } : { ...rest, embed: e };
}

/** POST api/models/upload */
interface UploadResult { path: string | null; adapter: string | null; format: ModelFormat | null; bytes: number }
/** POST api/models/{id}/predict */
type PredictResult = { ready: true } | { ready: false; job_id: number };

export const MODEL_ACTIONS: Record<string, SetAction> = {
  'new-model': () => openForm(null),
  'edit-model': (b) => openForm(SET.models.find((x) => x.id === rowAttr(b, '[data-mid]', 'mid'))),
  'add-found': (b) => {
    const fd = SET.found[+dd(b, 'i')];
    if (!fd) return;
    openForm(null);
    Object.assign(formOf('model').model, { name: fd.name, family: fd.family, format: fd.format, path: fd.path });
    renderSettings();
    void browse(fd.path.replace(/\/[^/]*$/, ''));
  },
  'pick-format': (b) => {
    const f = formOf('model'), m = f.model;
    const v = dd(b, 'v');
    if (isFormat(v)) m.format = v;
    renderSettings();
    if (m.format !== 'api' && !f.browse) void browse('');
  },
  'model-source': (b) => { formOf('model').source = dd(b, 'v') === 'upload' ? 'upload' : 'path'; renderSettings(); },
  'go-model-path': () => {
    const p = formOf('model').model.path || '';
    void browse(/\.[a-z]+$/i.test(p) ? p.replace(/\/[^/]*$/, '') : p);
  },
  'up-model-path': () => browse(formOf('model').browse?.parent),
  'model-crumb': (b) => browse('/' + dd(b, 'to')),
  'model-folder': (b) => {
    const br = formOf('model').browse;
    if (br) void browse(`${br.path.replace(/\/$/, '')}/${dd(b, 'name')}`);
  },
  'pick-model-dir': (b) => {
    const f = formOf('model'), m = f.model, br = f.browse;
    if (!br) return;
    m.path = `${br.path.replace(/\/$/, '')}/${dd(b, 'name')}`;
    m.format = 'openvino';
    if (!m.name) m.name = dd(b, 'name').replace(/_openvino_model$/, '');
    renderSettings();
  },
  'pick-model-file': (b) => {
    const f = formOf('model'), m = f.model, br = f.browse;
    if (!br) return;
    const name = dd(b, 'name');
    const file = br.files.find((x) => x.name === name);
    const full = `${br.path.replace(/\/$/, '')}/${name}`;
    if (/\.py$/.test(full)) m.adapter = full;
    else if (/\.ya?ml$/.test(full)) m.arch = full;
    else {
      m.path = full;
      if (file && file.format) m.format = file.format;
      if (!m.name) m.name = br.path.split('/').slice(-2)[0] === 'weights' ? (br.path.split('/').slice(-3)[0] ?? '') : name.replace(/\.[^.]+$/, '');
    }
    renderSettings();
  },
  'upload-model': async () => {
    const f = formOf('model');
    const chosen = [...($req<HTMLInputElement>('m-upload').files ?? [])];
    if (!chosen.length) { f.upStatus = { kind: 'err', text: 'choose a file first' }; renderSettings(); return; }
    const fdata = new FormData();
    chosen.forEach((x) => fdata.append('files', x));
    const total = chosen.reduce((a, x) => a + x.size, 0);
    f.upStatus = { kind: 'busy', text: `uploading ${fmtBytes(total)}…` };
    renderSettings();
    try {
      const r: UploadResult = await (await api('api/models/upload', { method: 'POST', body: fdata })).json();
      if (r.path) { f.model.path = r.path; if (r.format) f.model.format = r.format; }
      if (r.adapter) f.model.adapter = r.adapter;
      if (!f.model.name && r.path) f.model.name = (r.path.split('/').pop() ?? '').replace(/\.[^.]+$/, '');
      f.upStatus = { kind: 'ok', text: `uploaded ${fmtBytes(r.bytes)}` };
    } catch (e) { f.upStatus = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  'serving-list': async () => {
    const f = formOf('model');
    f.apiStatus = { kind: 'busy', text: 'asking the server…' };
    renderSettings();
    try {
      const list = (await getJSON<{ models: ServingEntry[] }>(`api/models/serving-list?url=${encodeURIComponent(f.model.api.url)}`)).models;
      f.servingList = list;
      f.apiStatus = { kind: 'ok', text: `${list.length} model${list.length === 1 ? '' : 's'} served` };
    } catch (e) { f.servingList = null; f.apiStatus = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  'label-list': async () => {
    const f = formOf('model'), a = f.model.api;
    f.apiStatus = { kind: 'busy', text: 'asking the server…' };
    renderSettings();
    try {
      // editing a saved model whose key stays on the server (blank here): ask through the saved model
      const body = !a.api_key && f.hasKey && f.model.id ? { registry_id: f.model.id }
        : { connection: { url: a.url.trim(), api_key: a.api_key || undefined, timeout_s: Number(a.timeout) || 60 } };
      const r = await postJSON<{ models?: LabelModelEntry[]; protocol?: string }>('api/label/models/detect', body);
      const list = r.models || [];
      f.labelList = list;
      f.apiStatus = { kind: 'ok', text: `${r.protocol === 'generic' ? 'dataviz-label/1' : r.protocol} · ${list.length} model${list.length === 1 ? '' : 's'}` };
    } catch (e) { f.labelList = null; f.apiStatus = { kind: 'err', text: errText(e) }; }
    renderSettings();
  },
  'pick-label': (b) => {
    const f = formOf('model'), x = f.labelList?.[+dd(b, 'i')], m = f.model;
    if (!x) return;
    m.api.label_model = x.id;
    m.task_type = x.task_type || 'object_detection';
    if (!m.name) m.name = `${x.name || x.id} (API)`;
    renderSettings();
  },
  'pick-serving': (b) => {
    const f = formOf('model'), s = f.servingList?.[+dd(b, 'i')], a = f.model.api;
    if (!s) return;
    Object.assign(a, { provider: s.provider, model_type: s.model_type, model_name: s.model_name, version: s.versions[0] || 'v1' });
    if (!f.model.name) f.model.name = `${s.model_name} (API)`;
    renderSettings();
  },
  'save-model': async (b) => {
    const f = formOf('model');
    f.saveStatus = { kind: 'busy', text: 'saving…' };
    renderSettings();
    let saved: ModelItem;
    try { saved = await postJSON<ModelItem>('api/models', payload()); } catch (e) { f.saveStatus = { kind: 'err', text: errText(e) }; renderSettings(); return; }
    await loadModelsData();
    void modelsChanged();
    if (onDataset()) void refreshStatus();         // the plot's feature list (My models) follows
    SET.view = 'list';
    SET.form = null;
    renderSettings();
    if (b.dataset.test) {
      const el = $(`mst-${saved.id}`);
      if (el) void (saved.family === 'hf' ? testEmbed(saved.id, el) : testModel(saved.id, el));
    }
  },
  'test-embed': (b) => { const id = rowAttr(b, '[data-mid]', 'mid'); void testEmbed(id, $req(`mst-${id}`)); },
  'test-model': (b) => { const id = rowAttr(b, '[data-mid]', 'mid'); void testModel(id, $req(`mst-${id}`)); },
  'run-model': async (b) => {
    const id = rowAttr(b, '[data-mid]', 'mid'), el = $req(`mst-${id}`);
    setDisabled(b, true);
    try {
      const r = await postJSON<PredictResult>(`api/models/${encodeURIComponent(id)}/predict`, {});
      if (!r.ready) {
        el.innerHTML = '<div class="bar"><div></div></div><span class="num"></span>';
        await waitJob(r.job_id, (j) => {
          const box = $(`mst-${id}`);
          const bar = box && box.querySelector<HTMLElement>('.bar > div');
          if (!box || !bar) return;
          setBar(bar, j);
          const num = box.querySelector('.num');
          if (num) num.textContent = j.total ? `${j.done}/${j.total} images ${j.extra || ''}` : j.stage;
        });
      }
      await loadModelsData();
      void modelsChanged();
      renderSettings();
    } catch (e) { el.innerHTML = statusHTML({ kind: 'err', text: errText(e) }); setDisabled(b, false); }
  },
  'remove-model': async (b) => {
    const m = SET.models.find((x) => x.id === rowAttr(b, '[data-mid]', 'mid'));
    if (!m) return;
    if (!confirm(`Remove the model "${m.name}"?\n\nUploaded files are deleted; files elsewhere on the server are not touched.`)) return;
    try { await api(`api/models/${encodeURIComponent(m.id)}`, { method: 'DELETE' }); } catch (e) { alert(errText(e)); }
    await loadModelsData();
    void modelsChanged();
    if (onDataset()) void refreshStatus();
    renderSettings();
  },
};

export const MODEL_CHANGES: Record<string, SetChange> = {
  'model-rerender': () => {
    const m = formOf('model').model;
    const allowed = SET.spec?.matrix[m.family];
    if (allowed && !allowed.includes(m.format) && allowed[0]) m.format = allowed[0];
    renderSettings();
  },
};
