// Browser globals that are not modules: window.Plotly (plotly.min.js, loaded on demand by main.ts) and the
// debugging handle window.dataviz (set by main.ts on the visualizer, extended by label/app.ts). Ambient only.
// Plotly as a *value* in a module: `declare const Plotly: typeof import('plotly.js')` at the top of that module
// (the UMD global is type-only there); use `import type * as P from 'plotly.js'` for its types.
import type * as PlotlyNS from 'plotly.js';
import type { AppState, SavedView, UiDefaults } from './types.js';
import type { Theme } from './core/theme.js';

// S.views: the saved projections, set by views/controls.ts (refreshSavedViews), read by main.ts
declare module './types.js' {
  interface AppState { views?: SavedView[] }
}

declare global {
  interface DatavizHandle {
    S?: AppState;
    UI?: UiDefaults;
    plotAxes?: () => unknown;
    readonly THEME?: Theme;
    label?: object;
  }
  interface Window {
    Plotly?: typeof PlotlyNS;
    dataviz: DatavizHandle;
  }
}
export {};
