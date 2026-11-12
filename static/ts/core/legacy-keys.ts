// This browser's settings saved under the names the pages had before Bucket, Image and Data (Data Center, Sessions,
// Analysis): moved once to the new key, so a layout or upload chunk size is not lost. Synchronous and tiny: runs at
// start, before any page reads its keys. Server-side prefs were renamed by migration 0016.

const MOVES: ReadonlyArray<readonly [string, string]> = [
  ['dc.upload.chunk', 'bucket.upload.chunk'],
  ['dataviz.analysis.layout', 'dataviz.data.layout'],
  ['dataviz.analysis.explorer', 'dataviz.data.explorer'],
  ['dataviz.analysis.ends', 'dataviz.data.ends'],
  ['dataviz.analysis.export', 'dataviz.data.export'],
];

export function moveLocalKeys(): void {
  try {
    for (const [from, to] of MOVES) {
      const v = localStorage.getItem(from);
      if (v === null) continue;
      if (localStorage.getItem(to) === null) localStorage.setItem(to, v);
      localStorage.removeItem(from);
    }
  } catch (e) { /* storage blocked (private window): nothing to move */ }
}
