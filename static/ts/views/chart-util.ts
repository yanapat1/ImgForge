// Plotly layout pieces shared by the Model and Evaluation tabs, and small typed accessors.

import { plotBase, THEME } from '../core/theme.js';

export const ax = (): Partial<Plotly.LayoutAxis> => ({ gridcolor: THEME.grid, linecolor: THEME.border, zerolinecolor: THEME.border,
  tickfont: { color: THEME.muted }, title: { font: { color: THEME.muted } } });
export const cfg: Partial<Plotly.Config> = { displaylogo: false, responsive: true };
export const base = (): Partial<Plotly.Layout> => ({ ...plotBase(THEME.surface, 12), xaxis: ax(), yaxis: ax(), legend: { font: { color: THEME.text } } });

/** Element `i` of an array that is long enough by construction (throws instead of returning undefined). */
export function at<T>(a: ArrayLike<T>, i: number): T {
  const v = a[i];
  if (v === undefined) throw new RangeError(`index ${i} out of range (${a.length})`);
  return v;
}

/** The customdata of a clicked point as an array (Plotly types it as a single Datum). */
export function customOf(p: Plotly.PlotDatum | undefined): unknown[] {
  const cd: unknown = p?.customdata;
  return Array.isArray(cd) ? cd : [];
}

/** A table's first <tbody> (throws when the markup lacks one). */
export function tbody(t: HTMLTableElement): HTMLTableSectionElement {
  const tb = t.tBodies[0];
  if (!tb) throw new Error(`table #${t.id} has no <tbody>`);
  return tb;
}

/**
 * Add nested-array fields to a trace (heatmap cells carrying [id, class] as customdata, a 2-D `text` grid). Plotly's
 * typings only allow flat arrays there, but the library takes any depth. The result is still the trace's own type,
 * so it goes into Plotly.react() unchanged.
 */
export function withNested<T extends object>(trace: T, fields: Record<string, unknown>): T {
  return Object.assign(trace, fields);
}
