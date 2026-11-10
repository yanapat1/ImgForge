// A line diff (Myers' O((N+M)·D) shortest edit script). Common leading and trailing lines are cut first, so a small
// edit to a long text costs almost nothing. Gives up (null) past `maxD` edits: the caller shows the text instead.

export type DiffOp = { t: ' ' | '+' | '-'; s: string };

export function diffLines(a: string[], b: string[], maxD = 2000): DiffOp[] | null {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let q = 0;
  while (q < a.length - p && q < b.length - p && a[a.length - 1 - q] === b[b.length - 1 - q]) q++;
  const A = a.slice(p, a.length - q), B = b.slice(p, b.length - q);
  const N = A.length, M = B.length, max = N + M, o = max + 1;
  const head: DiffOp[] = a.slice(0, p).map((s) => ({ t: ' ', s }));
  const tail: DiffOp[] = a.slice(a.length - q).map((s) => ({ t: ' ', s }));
  if (!N || !M) return [...head, ...A.map((s): DiffOp => ({ t: '-', s })), ...B.map((s): DiffOp => ({ t: '+', s })), ...tail];
  const V = new Int32Array(2 * max + 3);
  const trace: Int32Array[] = [];                      // V before step d, the window k = -d-1 .. d+1
  let found = -1;
  for (let d = 0; d <= Math.min(max, maxD) && found < 0; d++) {
    trace.push(V.slice(o - d - 1, o + d + 2));
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && (V[o + k - 1] as number) < (V[o + k + 1] as number)) ? (V[o + k + 1] as number) : (V[o + k - 1] as number) + 1;
      let y = x - k;
      while (x < N && y < M && A[x] === B[y]) { x++; y++; }
      V[o + k] = x;
      if (x >= N && y >= M) { found = d; break; }
    }
  }
  if (found < 0) return null;
  const mid: DiffOp[] = [];
  let x = N, y = M;
  for (let d = found; d >= 0; d--) {
    const v = trace[d] as Int32Array, at = (k: number): number => v[k + d + 1] as number;
    const k = x - y;
    const pk = k === -d || (k !== d && at(k - 1) < at(k + 1)) ? k + 1 : k - 1;
    const px = d === 0 ? 0 : at(pk), py = d === 0 ? 0 : px - pk;
    while (x > px && y > py) { mid.push({ t: ' ', s: A[x - 1] as string }); x--; y--; }
    if (d > 0) {
      if (x === px) mid.push({ t: '+', s: B[y - 1] as string }); else mid.push({ t: '-', s: A[x - 1] as string });
    }
    x = px; y = py;
  }
  mid.reverse();
  return [...head, ...mid, ...tail];
}
