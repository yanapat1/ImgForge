// What shared code asks of a screen it must not import. An install may leave a zone out (dataviz/zones.py): the
// visualizer's code is then not even on the server, so core, settings and the other screens never import it. The
// visualizer screen (viz/boot.ts) fills these in when it starts; on every other screen they stay unset and do nothing.

type Fn = () => void | Promise<void>;

export const hooks: {
  repaint: Fn | null;          // the theme, look or palette changed: redraw the plot, gallery and the open tab
  refreshStatus: Fn | null;    // a job finished or the models changed: reload the session status (feature list ✓ marks)
  modelsChanged: Fn | null;    // Settings › Models added or removed a model: the Model tab reloads its list
  displayChanged: Fn | null;   // Settings › Display changed a plot mark: redraw the marks and remember the setting
} = { repaint: null, refreshStatus: null, modelsChanged: null, displayChanged: null };
