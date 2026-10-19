// Non-null accessors for the plot's state (the plot only runs once a projection is shown) and the typed #plot div.

import type { Groups, ProjectionReady } from '../types.js';
import type { GroupInfo, PlotDiv, TraceUpdate } from '../types-plot.js';
import { $q, $req } from '../core/dom.js';
import { S } from '../core/state.js';
import type * as P from 'plotly.js';

declare const Plotly: typeof import('plotly.js');

export const plotDiv = (): PlotDiv => $req<PlotDiv>('plot');

/** The current projection (throws where the untyped code would have hit a null). */
export function curView(): ProjectionReady { if (!S.view) throw new Error('no projection'); return S.view; }

/** Point id -> index in the current projection. */
export function viewPos(): Map<number, number> { if (!S.viewPos) throw new Error('no projection'); return S.viewPos; }

export function curGroups(): Groups { if (!S.groups) throw new Error('no groups'); return S.groups; }

export const groupList = (g: Groups): GroupInfo[] => g.list as GroupInfo[];

/** Plotly.restyle with one value per trace (the typings only know one value for all). */
export function restyle(gd: PlotDiv, update: TraceUpdate, traces: number[]): Promise<unknown> {
  return Plotly.restyle(gd, update as P.Data, traces);
}

/** Plotly.update with one trace value per trace (see restyle). */
export function updateTraces(gd: PlotDiv, traceUpdate: TraceUpdate, layout: Partial<P.Layout>, traces: number[]): Promise<unknown> {
  return Plotly.update(gd, traceUpdate as P.Data, layout, traces);
}

/** Data coordinates of point `id` in the current projection (NaN for an id that is not in it: Plotly skips those). */
export function xyOf(id: number): [number, number] {
  const v = curView(), i = viewPos().get(id);
  return i === undefined ? [NaN, NaN] : [v.x[i] ?? NaN, v.y[i] ?? NaN];
}

/** SVG element by selector (the lasso layer is an <svg>: `$req` only types HTML elements). Throws when absent. */
export function svgReq<T extends SVGElement>(sel: string): T {
  const el = $q<T>(sel);
  if (!el) throw new Error(`missing element ${sel}`);
  return el;
}
