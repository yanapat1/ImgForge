// The labeling page's single mutable state (L), its settings defaults and the hotkey action ids.
// Shared contract for every module in js/label/ and for Settings › Labeling (js/settings/): modules read and
// write L.x and never keep private copies of shared data (the same rule as core/state.js S).

import type { HotkeyMap, LabelSettings, LabelState, TaskType } from '../types-label.js';

// settings: label_settings.json on the server, mirrored to localStorage 'label-settings' for the first paint
export const SETTINGS_DEFAULTS: LabelSettings = {
  crosshair: true,
  tags: 'all',                 // 'all' | 'hover_selected' | 'off'
  tag_content: 'name',         // 'name' | 'name_index'
  auto_advance: true,
  advance_to: 'next',          // 'next' | 'unlabeled'
  commit_scope: 'active',      // 'active' (the task on screen) | 'all' (every task of the image)
  fill_opacity: 0.15,
  stroke_width: 1.5,
  // appearance (Settings › Labeling › Appearance)
  tag_size: 10,                // name tag font px (the tag height follows)
  tag_opacity: 0.85,           // tag background alpha
  stroke_opacity: 1,           // line alpha ("faintness")
  line_style: 'solid',         // 'solid' | 'dashed' (model labels are always dashed)
  handle_size: 7,              // resize handle px
  color_mode: 'class',         // 'class' (each class its colour) | 'single' (single_color for every shape)
  single_color: '#ff3b30',
  selected_style: 'halo',      // 'halo' (thicker + halo) | 'thick' (thicker only)
  dim_other_tasks: true,       // shapes of the other tasks are drawn faded (not hidden)
  bbox_mode: 'drag',           // 'drag' | 'two_click'
  hotkeys: {},                 // {action_id: [combo, combo]}: only the ones changed from DEFAULT_HOTKEYS
};

// combo strings: modifiers in the order ctrl+alt+shift+, then the key: a KeyboardEvent.code-derived name for
// digits / arrows / named keys / punctuation ('1', 'arrowright', 'enter', 'pagedown', ';', '`'), else the lower-case
// letter. meta (Cmd) counts as ctrl. Punctuation is named by its physical key, so shift+; stays 'shift+;' (not ':').

// class hotkeys: the base keys give classes 1–44 (digits 1–9, 0 = class 10, then the letter / punctuation rows),
// shift + the same keys give classes 45–88. Class 1–9 also keep ctrl+digit (listed first: the panel shows it).
const BASE_KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0',
  'q', 'w', 'e', 'r', 't', 'a', 's', 'd', 'f', 'g', 'z', 'x', 'c', 'v', 'b', 'y', 'u', 'i', 'o', 'p',
  'h', 'j', 'k', 'l', ';', 'n', 'm', ',', '.', '/', '[', ']', "'", '-'];
export const CLASS_KEYS: string[] = [...BASE_KEYS, ...BASE_KEYS.map((k) => `shift+${k}`)];
export const N_TASK_KEYS = 9;

export const DEFAULT_HOTKEYS: HotkeyMap = {
  commit: ['ctrl+enter'],
  next: ['ctrl+arrowright', 'pagedown'],
  prev: ['ctrl+arrowleft', 'pageup'],
  next_unlabeled: ['ctrl+shift+arrowright'],
  skip: ['ctrl+shift+s'],
  review: ['alt+r'],                 // approve this done image (only when the session tracks review)
  undo: ['ctrl+z'],
  redo: ['ctrl+shift+z', 'ctrl+y'],
  delete: ['delete', 'backspace'],
  cancel: ['escape'],
  crosshair: ['alt+h'],
  tags: ['alt+t'],
  fit: ['alt+f', 'home'],
  hide_all: ['ctrl+h'],
  copy_prev: ['ctrl+shift+v'],
  duplicate: ['ctrl+d'],
  cycle_overlap: ['tab'],
  finish_shape: ['enter'],
  kp_visibility: ['alt+v'],
  panel_left: ['alt+['],
  panel_right: ['alt+]'],
  theme: ['alt+l'],                  // light / dark
  model_this: ['alt+m'],             // run the task's model on this image
  model_remove: ['ctrl+shift+m'],    // remove model labels from this image
  model_activate: ['ctrl+m'],        // let the linked model run in this session (nothing runs until it is on)
  area_select: ['`'],                // HOLD + left-drag: rectangle select (a hold key, like Space for panning)
  copy: ['ctrl+c'],
  paste: ['ctrl+v'],
  ...Object.fromEntries(CLASS_KEYS.map((k, i) => [`class_${i + 1}`, i < 9 ? [`ctrl+${k}`, k] : [k]])),
  ...Object.fromEntries(Array.from({ length: N_TASK_KEYS }, (_, i) => [`task_${i + 1}`, [`alt+${i + 1}`]])),
};

export const HOTKEY_LABELS: Record<string, string> = {
  commit: 'Commit image', next: 'Next image', prev: 'Previous image', next_unlabeled: 'Next unfinished image',
  skip: 'Skip image', review: 'Mark image reviewed', undo: 'Undo', redo: 'Redo', delete: 'Delete selected', cancel: 'Cancel / deselect',
  crosshair: 'Toggle crosshair', tags: 'Cycle name tags', fit: 'Fit image', hide_all: 'Hide / show all shapes',
  copy_prev: 'Copy previous image’s shapes', duplicate: 'Duplicate selected', cycle_overlap: 'Cycle overlapping shapes',
  finish_shape: 'Finish polygon', kp_visibility: 'Keypoint visibility',
  panel_left: 'Show / hide the classes panel', panel_right: 'Show / hide the objects panel', theme: 'Light / dark theme',
  model_this: 'Label this image with the model', model_remove: 'Remove model labels from this image',
  model_activate: 'Activate / deactivate the model for this session',
  area_select: 'Hold + drag: select an area', copy: 'Copy selected shapes', paste: 'Paste shapes',
  ...Object.fromEntries(CLASS_KEYS.map((_, i) => [`class_${i + 1}`, `Class ${i + 1} of the task`])),
  ...Object.fromEntries(Array.from({ length: N_TASK_KEYS }, (_, i) => [`task_${i + 1}`, `Switch to task ${i + 1}`])),
};

// combos a browser keeps for itself on some systems (the editor warns; plain-digit aliases still work)
export const BROWSER_RESERVED = ['ctrl+1', 'ctrl+2', 'ctrl+3', 'ctrl+4', 'ctrl+5', 'ctrl+6', 'ctrl+7', 'ctrl+8', 'ctrl+9',
  'alt+1', 'alt+2', 'alt+3', 'alt+4', 'alt+5', 'alt+6', 'alt+7', 'alt+8', 'alt+9',
  'ctrl+w', 'ctrl+t', 'ctrl+n', 'ctrl+tab', 'ctrl+shift+t', 'ctrl+shift+n', 'ctrl+shift+w'];

export interface TaskTypeInfo { label: string; icon: string; geometry: string }
export const TASK_TYPES: Record<TaskType, TaskTypeInfo> = {
  object_detection: { label: 'Object detection', icon: 'box', geometry: 'bbox' },
  segmentation: { label: 'Segmentation', icon: 'polygon', geometry: 'polygon' },
  keypoints: { label: 'Keypoints / landmarks', icon: 'keypoint', geometry: 'keypoints' },
  classification: { label: 'Classification', icon: 'tag', geometry: 'tag' },
};

export const L: LabelState = {
  pid: null,
  project: null,            // Project from GET /api/label/projects/{pid} (tasks with their classes)
  ids: [],                  // media ids in position order (GET media-ids)
  idx: -1,                  // index into ids of the image on screen
  media: null,              // Media of the image on screen
  anns: new Map(),          // id -> Ann, every task of the image on screen
  versions: {},             // task_id -> version the page last saw (PUT base_version)
  taskId: null,             // active task: only its classes are listed, its tool is the one drawing
  classId: null,            // class new shapes get
  selected: new Set(),      // selected annotation ids
  hoverId: null,
  hidden: new Set(),        // annotation ids hidden with the eye
  hideAll: false,
  view: { scale: 1, tx: 0, ty: 0 },  // image px -> canvas css px: x * scale + tx
  settings: { ...SETTINGS_DEFAULTS },
  dirty: new Set(),         // task ids with unsaved changes on the image on screen
  saveState: 'saved',       // 'saved' | 'saving' | 'offline' | 'conflict'
  // added by the label page (js/label/app.js owns them)
  history: null,            // {undo:[], redo:[]} of the image on screen (kept per image in the LRU)
  editing: new Map(),       // id -> live Ann while a shape is dragged (drawn on the interaction layer only)
  loading: false,           // the image on screen is still loading: tools ignore input
  cursor: null,             // {nx, ny} pointer in normalised image coordinates, null when off the canvas
  readOnly: false,          // my_role 'viewer' on this session: browse only (app.js sets it from the project)
};

// hold keys: active while pressed (bare keys only); the hotkey editor labels them "hold"
export const HOLD_ACTIONS: ReadonlySet<string> = new Set(['area_select']);

export const settingsHotkeys = (s: LabelSettings = L.settings): HotkeyMap => ({ ...DEFAULT_HOTKEYS, ...(s.hotkeys || {}) });
