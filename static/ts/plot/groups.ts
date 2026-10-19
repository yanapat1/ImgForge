// Density groups (bubbles / outliers) fetched from the server, and selecting a group.

import type { GroupsPayload, ProjectionRequest } from '../types.js';
import { postJSON } from '../core/api.js';
import { $req } from '../core/dom.js';
import { pts, S } from '../core/state.js';
import { clsName } from '../core/theme.js';
import { curGroups, curView, groupList } from './access.js';
import { invalidateHover } from './render.js';
import { setSelection } from '../views/gallery.js';

function groupsWanted(): boolean {
  return $req<HTMLInputElement>('g-bubbles').checked || $req<HTMLInputElement>('g-outliers').checked ||
    $req<HTMLInputElement>('g-dim').checked || $req<HTMLSelectElement>('color-by').value === 'group';
}

export async function refreshGroups(): Promise<void> {
  if (!groupsWanted() || !S.viewReq) { $req('g-info').textContent = ''; return; }
  const body: ProjectionRequest & { min_cluster_size: number; per_class: boolean } = { ...S.viewReq,
    min_cluster_size: Math.max(3, +$req<HTMLInputElement>('g-min').value || 15), per_class: $req<HTMLSelectElement>('g-mode').value === 'class' };
  const k = JSON.stringify(body);
  if (S.groups && S.groups.k === k) return;
  const r = await postJSON<GroupsPayload>('api/groups', body);
  const of = new Map<number, number>();
  curView().id.forEach((id, i) => of.set(id, r.group[i] ?? -1));
  S.groups = { k, group: r.group, list: r.groups, nOut: r.n_outliers, of };
  invalidateHover();
  $req('g-info').textContent = `${r.groups.length} groups · ${r.n_outliers} outliers (${(100 * r.n_outliers / Math.max(1, r.group.length)).toFixed(1)}%)`;
}

export const isOutlier = (id: number): boolean => !!S.groups && S.groups.of.get(id) === -1;

export const showOutlierMarks = (): boolean => !!S.groups && $req<HTMLInputElement>('g-outliers').checked;

export function selectGroupOf(id: number): boolean {
  if (!S.groups) return false;
  const gid = S.groups.of.get(id);
  if (gid === undefined) return false;
  if (gid === -1) { $req<HTMLSelectElement>('sort').value = 'outlier'; setSelection(curView().id.filter(isOutlier), 'Outliers (dot is not in any group)'); return true; }
  selectGroup(gid);
  return true;
}

export function selectGroup(gid: number): void {
  const groups = curGroups();
  const ids = curView().id.filter((id) => groups.of.get(id) === gid);
  const g = groupList(groups).find((x) => x.id === gid);
  const first = ids[0];
  const cls = g ? g.cls : first === undefined ? undefined : pts().cls[first];
  setSelection(ids, `Group ${gid} · ${clsName(cls ?? -1)}${g && !g.pure ? ' (mixed)' : ''}`);
}
