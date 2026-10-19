// Types of the embedding plot (plot/*.ts, main.ts): the Plotly graph div with its private layout geometry, the
// density groups sent by POST api/groups, and the shapes handed to Plotly.
import type * as P from 'plotly.js';

/** Plotly's private axis geometry (`gd._fullLayout.xaxis`): pixel <-> data maths. Offsets are relative to the plot div. */
export interface PlotAxis {
  _offset: number;
  _length: number;
  p2l(px: number): number;
  l2p(v: number): number;
}

/** The `#plot` div once Plotly drew it (`data` / `_fullLayout` are absent before the first draw). */
export interface PlotDiv extends Omit<P.PlotlyHTMLElement, 'data'> {
  data?: P.Data[];
  _fullLayout?: { xaxis?: PlotAxis; yaxis?: PlotAxis };
}

/** Axes + client rect of the drawn plot (plotAxes()). */
export interface PlotGeom { gd: PlotDiv; xa: PlotAxis; ya: PlotAxis; r: DOMRect }

/** One density group (api/groups → groups[]): bubble polygon x / y in data coordinates. */
export type GroupInfo = { id: number; n: number; cls: number; pure: boolean; x: number[]; y: number[] };

/** Per-trace restyle / update payload (`{x: [xs of trace 0, xs of trace 1], marker: [m0, m1]}`): Plotly accepts an array per trace, its typings do not. */
export type TraceUpdate = Record<string, unknown>;

// ------------------------------------------------------------------ main.ts payloads
/** GET api/workspace */
export interface WorkspacePayload {
  name: string; path: string; created: string | null; size_mb: number; n_features: number; n_projections: number;
  others: Array<{ name: string; [k: string]: unknown }>;
}
/** GET api/similar/{id} */
export interface SimilarPayload { id: number[]; sim: number[] }
/** POST api/text-search: a job id until the text encoder is ready, then the nearest boxes. */
export type TextSearchResult = { ready: false; job_id: number } | { ready: true; id: number[]; score: number[] };
/** GET api/duplicates */
export interface DuplicatesPayload { pairs: Array<{ a: number; b: number; sim: number }> }
