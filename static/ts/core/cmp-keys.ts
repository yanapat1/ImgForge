// The colour key per box of colour-by 'origin' / 'diff' on a combined dataset (compare-core originKeys / diffKeys):
// one Uint8Array built once per (colouring, run, A, B, shared) and reused, so plot/render.ts reads one byte per dot.

import { diffKeys, originKeys } from './compare-core.js';
import { S } from './state.js';

/** The cached key column of `by` (null: not a combined dataset, or 'diff' before a run). */
export function cmpKeys(by: 'origin' | 'diff'): Uint8Array | null {
  const C = S.cmp;
  if (!C) return null;
  const r = C.run;
  if (by === 'diff' && !r) return null;
  const tag = `${by}|${r?.key ?? ''}|${C.a}|${C.b}|${by === 'origin' && C.shared ? 1 : 0}`;
  if (C.keys && C.keysFor === tag && C.keys.length === C.origin.length) return C.keys;
  const out = C.keys ?? undefined;
  C.keys = by === 'diff' && r ? diffKeys(C.origin, r.status, C.a, C.b, out) : originKeys(C.origin, r ? r.status : null, C.a, C.b, C.shared, out);
  C.keysFor = tag;
  return C.keys;
}

/** The statuses changed in place (a live patch): rebuild on the next read. */
export function staleKeys(): void { if (S.cmp) S.cmp.keysFor = ''; }
