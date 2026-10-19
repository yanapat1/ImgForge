// The single mutable page state (S) and the page defaults from config.yaml (UI).

import type { AppState, Meta, Points, Status, UiDefaults } from '../types.js';

// page defaults; config.yaml ui.* is merged in at startup (applyUiDefaults)
export const UI: UiDefaults = { page_size: 100, similar_k: 48, check_list_size: 300, duplicate_threshold: 0.97 };

export const S: AppState = {
  status: null,
  meta: null,
  P: null,                 // columnar static points + current feature scores
  view: null,              // {id, x, y, key, info} current projection
  viewReq: null,           // request that produced S.view
  scoresFeature: null,
  hidden: new Set(),       // hidden class ids (All mode)
  sel: [],
  selKind: 'samples',      // 'samples' | 'pairs'
  pairs: [],
  selTitle: 'No selection',
  page: 0,
  highlight: [],
  picked: [],              // gallery cards clicked (ctrl+click stacks): pinned as yellow dots on the plot
  sessionIdx: null,
  groups: null,            // {k, group[], list[], nOut, of: Map(id -> group)}
  tags: {},
  lasso: null,             // {x[], y[]} lasso polygon in data coordinates
  dimSet: null,            // ids inside the lasso (the rest is faded)
  dist: null,              // {key, d[][]} feature distances between the picked cards
  // Model tab: the chosen model, how it is scored, and the last evaluation (views/model.js). The per-label
  // outcome is also copied into P.m_status / m_pred / m_score so Explore can colour and sort by it.
  edited: new Set(),   // images saved from the dialog this session (the model tab greens their cards)
  model: { id: null, conf: 0.25, iou: 0.5, split: 'val', list: [], ev: null, showPreds: false },
};

// Non-null accessors for code that only runs after boot (main.ts has loaded status / meta / points): they throw
// where the untyped code would have hit a TypeError on null.
export function pts(): Points { if (!S.P) throw new Error('points not loaded'); return S.P; }
export function meta(): Meta { if (!S.meta) throw new Error('meta not loaded'); return S.meta; }
export function status(): Status { if (!S.status) throw new Error('status not loaded'); return S.status; }

export function featureLabel(key: string | null): string {
  const f = S.status && S.status.features && S.status.features.find((x) => x.key === key);
  return f ? f.label : String(key);
}
