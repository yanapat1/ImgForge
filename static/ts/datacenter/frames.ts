// "Extract frames…" (double-click a video, the details pane, the folder's Send menu): cut videos into image frames
// (dataviz/datacenter/video.py). One video plays in the dialog, with Set start / Set end from its current time; the
// presets fill the one pattern the server knows ("every E s take T frames from W s, spread or consecutive", plus
// every n-th / all / count / keyframes / scene / smart), and every change asks POST api/dc/frames/estimate (debounced)
// for the frame count, the size and a film strip of the planned frames (file?w=&t=). The output: format, size,
// near-duplicate skip, the folder layout (YOLO, YOLO + train/val/test by time block, per video, flat) and where it
// goes (this bucket, or a new one: the only way for a bucket streamed from S3). POST api/dc/frames runs one job;
// progress shows in the activity strip, and at the end the folder opens or goes on to Label / Visualize.

import { zoneOn } from '../core/auth.js';
import { errText } from '../core/api.js';
import { esc, icon, syncRanges } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { toast } from '../core/toast.js';
import { openDialog } from '../views/dialog.js';
import { watchJobs } from './activity.js';
import * as api from './api.js';
import type { Action } from './app.js';
import { openSend } from './send.js';
import { baseName, bucketOf, D, fmtN, fmtSize, hooks, parentOf, selEntries } from './state.js';
import type { FramesDone, FramesEstimate, FramesFormat, FramesLayout, FramesOpts, VideoInfo } from '../types-datacenter.js';

type Preset = 'fps' | 'first' | 'burst' | 'every' | 'all' | 'count' | 'keyframes' | 'scene' | 'smart';
type After = 'open' | 'label' | 'visualize';
interface FR {
  preset: Preset; fps: number; take: number; everyS: number; windowS: number; pick: 'spread' | 'consecutive';
  n: number; every: number; threshold: number; start: number | null; end: number | null;
  dedupe: number; maxSide: number | null; format: FramesFormat; quality: number; layout: FramesLayout;
  target: 'inplace' | 'copy'; newName: string; out: string; remember: boolean; after: After;
  info: VideoInfo | null; est: FramesEstimate | null; estErr: string; busy: boolean; tok: number;
}

const PRESETS: ReadonlyArray<[Preset, string, string]> = [
  ['fps', 'Per second', 'N frames spread over every second'],
  ['first', 'First N of each second', 'N frames in a row at the start of every second'],
  ['burst', 'Burst every K s', 'N frames from a window, every K seconds'],
  ['every', 'Every Nth frame', 'every Nth decoded frame'],
  ['all', 'All frames', 'every frame of the video'],
  ['count', 'Total count', 'N frames spread over the whole video'],
  ['keyframes', 'Keyframes', 'only the encoder’s key frames: the fastest'],
  ['scene', 'Scene changes', 'a frame each time the picture changes'],
  ['smart', 'Smart', 'the N most varied frames, covering the whole video'],
];
const LAYOUTS: ReadonlyArray<[FramesLayout, string, string]> = [
  ['yolo', 'YOLO', 'images/ + empty labels/ + data.yaml: ready to label'],
  ['yolo-split', 'YOLO + train / val / test', 'split by time blocks, so near-identical frames never sit in two splits'],
  ['per-video', 'One folder per video', '<video>/ with its frames'],
  ['flat', 'Flat', 'every frame in one folder'],
];
const DEDUPE: ReadonlyArray<[number, string]> = [[0, 'Off'], [0.01, 'Light'], [0.03, 'Strong']];
const SIDES: ReadonlyArray<[number | null, string]> = [[null, 'Original'], [1920, '1920 px'], [1280, '1280 px'], [640, '640 px']];

/** m:ss.s */
export const fmtTime = (s: number): string => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;

/** The server's options for the dialog state. */
function opts(F: FR): FramesOpts {
  const o: FramesOpts = { start: F.start ?? 0, end: F.end, dedupe: F.dedupe, max_side: F.maxSide, format: F.format, quality: F.quality, layout: F.layout };
  switch (F.preset) {
    case 'fps': return { ...o, fps: F.fps };
    case 'first': return { ...o, mode: 'pattern', every_s: 1, window_s: 1, take: F.take, pick: 'consecutive' };
    case 'burst': return { ...o, mode: 'pattern', every_s: F.everyS, window_s: Math.min(F.windowS, F.everyS), take: F.take, pick: F.pick };
    case 'every': return { ...o, mode: 'every', n: F.every };
    case 'count': return { ...o, mode: 'count', n: F.n };
    case 'scene': return { ...o, mode: 'scene', threshold: F.threshold };
    case 'smart': return { ...o, mode: 'smart', n: F.n };
    default: return { ...o, mode: F.preset };
  }
}

/** A saved auto-extract setting in a few words (the bucket details). */
export function describeOpts(o: FramesOpts): string {
  const m = o.mode ?? 'pattern';
  const lay = LAYOUTS.find((x) => x[0] === (o.layout ?? 'yolo'))?.[1] ?? '';
  let s: string;
  if (m === 'all') s = 'all frames';
  else if (m === 'every') s = `every ${o.n ?? 1}th frame`;
  else if (m === 'count') s = `${o.n} frames`;
  else if (m === 'keyframes') s = 'keyframes';
  else if (m === 'scene') s = 'scene changes';
  else if (m === 'smart') s = `smart ${o.n ?? 300}`;
  else if ((o.every_s ?? 1) === 1 && (o.window_s ?? 1) === 1) s = `${o.pick === 'consecutive' ? 'first ' : ''}${o.take ?? 1} per second`;
  else s = `${o.take ?? 1} from ${o.window_s} s every ${o.every_s} s`;
  return `${s} · ${(o.format ?? 'jpg').toUpperCase()} · ${lay}`;
}

const num = (key: string, label: string, v: number, min: number, max: number, step: number | 'any' = 1, unit = ''): string =>
  `<label class="fld"><span>${label}</span><span class="dc-fr-num"><input type="number" class="txt" data-f="${key}" value="${v}" min="${min}" max="${max}" step="${step}" inputmode="decimal">${unit ? `<span class="faint">${unit}</span>` : ''}</span></label>`;

function fields(F: FR): string {
  switch (F.preset) {
    case 'fps': return `<div class="f2">${num('fps', 'Frames per second', F.fps, 0.01, 240, 'any')}<span></span></div>`;
    case 'first': return `<div class="f2">${num('take', 'Frames in a row', F.take, 1, 240)}<span></span></div>`;
    case 'burst': return `<div class="dc-fr-burst">${num('take', 'Take', F.take, 1, 1000, 1, 'frames')}${num('windowS', 'from', F.windowS, 0.05, 3600, 'any', 's')}` +
      `${num('everyS', 'every', F.everyS, 0.05, 86400, 'any', 's')}<label class="fld"><span>Pick</span><select data-f="pick">` +
      `<option value="spread"${F.pick === 'spread' ? ' selected' : ''}>spread over the window</option><option value="consecutive"${F.pick === 'consecutive' ? ' selected' : ''}>in a row</option></select></label></div>`;
    case 'every': return `<div class="f2">${num('every', 'Every', F.every, 1, 100000, 1, 'th frame')}<span></span></div>`;
    case 'count': case 'smart': return `<div class="f2">${num('n', 'Frames', F.n, 1, 200000)}<span></span></div>`;
    case 'scene': return `<label class="fld"><span>Change needed <b class="num" id="dc-fr-thv">${F.threshold.toFixed(2)}</b> <span class="faint">· lower catches smaller changes</span></span>` +
      `<input type="range" data-f="threshold" min="0.02" max="0.5" step="0.01" value="${F.threshold}"></label>`;
    default: return '';
  }
}

function treeOf(F: FR, name: string, stem: string): string {
  const ext = F.format, f = `${stem}_000000.${ext}  ${stem}_000030.${ext}  …`;
  if (F.layout === 'yolo') return `${name}/\n  images/       ${f}\n  labels/       (empty: label next)\n  data.yaml\n  frames.csv    file, video, frame, time_s`;
  if (F.layout === 'yolo-split') return `${name}/\n  images/train/  val/  test/\n  labels/train/  val/  test/\n  data.yaml\n  frames.csv    file, video, frame, time_s, split`;
  if (F.layout === 'per-video') return `${name}/\n  ${stem}/       ${f}\n  frames.csv`;
  return `${name}/\n  ${f}\n  frames.csv`;
}

function estLine(F: FR): string {
  if (F.estErr) return `<div class="status-line err" role="status">${icon('alert')}<span>${esc(F.estErr)}</span></div>`;
  const e = F.est;
  if (!e) return `<div class="status-line busy" role="status"><span class="spin"></span><span>Counting the frames…</span></div>`;
  const n = e.frames == null ? 'Unknown until it runs' : `${e.approx ? '≈ ' : ''}${fmtN(e.frames)} frame${e.frames === 1 ? '' : 's'}`;
  const size = e.bytes ? ` · ~${fmtSize(e.bytes)}` : '';
  const many = e.videos.length > 1 ? ` · ${e.videos.length} videos` : '';
  const warn = e.frames != null && e.frames >= e.limit ? ` · capped at ${fmtN(e.limit)}` : '';
  return `<div class="status-line ${warn ? 'err' : 'ok'}" role="status">${icon(warn ? 'alert' : 'film')}<span><b>${n}</b>${size}${many}${warn} <span class="faint">· ${esc(e.describe)}</span></span></div>`;
}

function strip(F: FR, bid: string, path: string | null): string {
  if (!path || !F.est?.times.length) return '';
  return `<div class="dc-fr-strip" aria-label="Planned frames">${F.est.times.map((t) => `<figure><img src="${esc(api.fileUrl(bid, path, 160, t))}" alt="" decoding="async" loading="lazy"><figcaption class="num">${fmtTime(t)}</figcaption></figure>`).join('')}</div>`;
}

function form(F: FR, bid: string, one: string | null, canInplace: boolean, name: string, stem: string): string {
  const card = (on: boolean, attr: string, title: string, txt: string, dis = false): string =>
    `<button type="button" class="dest-o${on ? ' on' : ''}" ${attr} role="radio" aria-checked="${on}"${dis ? ' disabled' : ''}><span class="ri" aria-hidden="true"></span><span class="dt"><b>${title}</b><span>${esc(txt)}</span></span></button>`;
  const chip = (attr: string, on: boolean, label: string, title = ''): string =>
    `<button type="button" class="chip${on ? ' on' : ''}" ${attr} aria-pressed="${on}"${title ? ` title="${esc(title)}"` : ''}>${on ? icon('check') : ''}${label}</button>`;
  const range = F.start != null || F.end != null
    ? `<span class="num">${fmtTime(F.start ?? 0)} – ${F.end != null ? fmtTime(F.end) : 'end'}</span><button type="button" class="linkish" data-fr="clear-range">whole video</button>` : '<span class="faint">the whole video</span>';
  return `<div class="dc-fr-form">` +
    (one ? `<div class="dc-fr-range"><button type="button" class="sm" data-fr="set-start">${icon('chev-r')}Set start</button><button type="button" class="sm" data-fr="set-end">${icon('stop')}Set end</button><span class="grow"></span>${range}</div>` : '') +
    `<div class="m-lbl">Which frames</div><div class="chips" role="radiogroup" aria-label="Which frames">${PRESETS.map(([k, l, t]) => chip(`data-preset="${k}" role="radio"`, F.preset === k, l, t)).join('')}</div>` +
    `<p class="hint">${esc(PRESETS.find((p) => p[0] === F.preset)?.[2] ?? '')}</p>${fields(F)}` +
    `<div id="dc-fr-est">${estLine(F)}${strip(F, bid, one)}</div>` +
    `<div class="m-lbl">Images</div><div class="dc-fr-out"><div class="chips" role="radiogroup" aria-label="Image format">${(['jpg', 'png', 'webp'] as const).map((k) => chip(`data-fmt="${k}" role="radio"`, F.format === k, k.toUpperCase())).join('')}</div>` +
    `<label class="fld"><span>Size</span><select data-f="maxSide">${SIDES.map(([v, l]) => `<option value="${v ?? ''}"${v === F.maxSide ? ' selected' : ''}>${l}</option>`).join('')}</select></label>` +
    `<label class="fld"><span>Skip near-duplicates</span><select data-f="dedupe">${DEDUPE.map(([v, l]) => `<option value="${v}"${v === F.dedupe ? ' selected' : ''}>${l}</option>`).join('')}</select></label></div>` +
    (F.format !== 'png' ? `<label class="fld"><span>Quality <b class="num" id="dc-fr-qv">${F.quality}</b></span><input type="range" data-f="quality" min="50" max="100" step="1" value="${F.quality}"></label>` : '') +
    `<div class="m-lbl">Folder layout</div><div class="dest dc-fr-lay" role="radiogroup" aria-label="Folder layout">${LAYOUTS.map(([k, l, t]) => card(F.layout === k, `data-layout="${k}"`, l, t)).join('')}</div>` +
    `<pre class="dc-al-tree">${esc(treeOf(F, F.out.trim() || name, stem))}</pre>` +
    `<div class="m-lbl">Where</div><div class="dest" role="radiogroup" aria-label="Where">` +
    card(F.target === 'inplace', 'data-target="inplace"', 'Next to the video', canInplace ? 'A new folder in this bucket.' : 'This bucket is read-only (streamed from S3).', !canInplace) +
    card(F.target === 'copy', 'data-target="copy"', 'In a new bucket', 'A new managed bucket holds the frames.') + '</div>' +
    `<div class="f2"><label class="fld"><span>Folder name</span><input type="text" class="txt" data-f="out" value="${esc(F.out)}" placeholder="${esc(name)}" autocomplete="off" spellcheck="false"></label>` +
    (F.target === 'copy' ? `<label class="fld"><span>New bucket name</span><input type="text" class="txt" data-f="newName" value="${esc(F.newName)}" autocomplete="off" spellcheck="false"></label>` : '<span></span>') + '</div>' +
    `<label class="fld"><span>When done</span><select data-f="after"><option value="open"${F.after === 'open' ? ' selected' : ''}>Open the folder</option>` +
    (zoneOn('labeling') ? `<option value="label"${F.after === 'label' ? ' selected' : ''}>Send to Label</option>` : '') + (zoneOn('visualizer') ? `<option value="visualize"${F.after === 'visualize' ? ' selected' : ''}>Send to Visualize</option>` : '') + `</select></label>` +
    (canInplace ? `<label class="check"><input type="checkbox" data-f="remember"${F.remember ? ' checked' : ''}> Use these settings for every new video in this bucket <span class="faint">(auto-extract on upload and import)</span></label>` : '') +
    '</div>';
}

function meta(i: VideoInfo | null, n: number): string {
  if (n > 1) return `<div class="dc-fr-meta">${icon('film')}<span>${fmtN(n)} videos</span></div>`;
  if (!i) return `<div class="dc-fr-meta"><span class="spin sm"></span><span>Reading the video…</span></div>`;
  if (i.streamed) return `<div class="dc-fr-meta">${icon('cloud')}<span>${fmtSize(i.size)} · streamed from S3: it is downloaded when the job runs</span></div>`;
  return `<div class="dc-fr-meta">${icon('film')}<span class="num">${i.w}×${i.h} · ${i.fps} fps · ${fmtTime(i.duration ?? 0)} · ${fmtN(i.frames ?? 0)} frames · ${esc(i.codec ?? '')} · ${fmtSize(i.size)}</span>` +
    (i.rotation ? `<span class="faint">rotated ${i.rotation}°: frames come out upright</span>` : '') + '</div>';
}

/** Follow the job: toast at the end, then open the folder or go on to Label / Visualize. */
async function track(jobId: number, after: After): Promise<void> {
  watchJobs();
  try {
    const j = await waitJob(jobId);
    const r = (j as { result?: FramesDone }).result;
    if (!r || r.cancelled) { toast('Frame extraction cancelled'); return; }
    const dup = r.videos.reduce((a, v) => a + v.skipped_dupes, 0);
    toast(`${fmtN(r.frames)} frames in ${r.path}${dup ? ` · ${fmtN(dup)} near-duplicates skipped` : ''}${r.capped ? ' · capped' : ''}`, 'ok');
    await hooks.reload();
    if (after === 'label' || after === 'visualize') void openSend(r.bucket, [r.path], after);
    else hooks.navigate(r.bucket, r.path);
  } catch (e) {
    toast(`Frame extraction failed: ${errText(e)}`, 'err');
  }
}

export async function openFrames(bid: string, paths: string[]): Promise<void> {
  const b = bucketOf(bid);
  if (!b || !paths.length) return;
  const streamed = b.kind === 's3' && !b.local_ready;
  const canInplace = b.writable && !streamed;
  const videoish = paths.length === 1 && /\.(mp4|mov|avi|mkv|webm|m4v|wmv|mpe?g)$/i.test(paths[0] ?? '');
  const one = videoish ? paths[0] ?? null : null;
  const stem = one ? baseName(one).replace(/\.[^.]+$/, '').replace(/[^\w.-]+/g, '_') || 'video' : 'video';
  const folder = paths.length === 1 && !one ? paths[0] ?? '' : parentOf(paths[0] ?? '');
  const name = one ? `${stem}-frames` : `${baseName(folder) || b.name}-frames`;
  const F: FR = {
    preset: 'fps', fps: 1, take: 15, everyS: 2, windowS: 1, pick: 'spread', n: 300, every: 10, threshold: 0.1, start: null, end: null,
    dedupe: 0, maxSide: null, format: 'jpg', quality: 90, layout: 'yolo', target: canInplace ? 'inplace' : 'copy', newName: `${b.name}-frames`,
    out: '', remember: false, after: 'open', info: null, est: null, estErr: '', busy: false, tok: 0,
  };
  let root: HTMLElement | null = null;
  let timer = 0;
  const video = (): HTMLVideoElement | null => root?.querySelector<HTMLVideoElement>('video.dc-fr-player') ?? null;
  const paintEst = (): void => { const el = root?.querySelector('#dc-fr-est'); if (el) el.innerHTML = estLine(F) + strip(F, bid, one); };
  const paint = (): void => {
    const host = root?.querySelector('.dc-fr-form');
    if (host) host.outerHTML = form(F, bid, one, canInplace, name, stem);
    syncRanges();                                   // the sliders paint their filled part from --fill
  };
  const estimate = (): void => {
    clearTimeout(timer);
    timer = window.setTimeout(() => {
      const tok = (F.tok += 1);
      F.est = null; F.estErr = '';
      paintEst();
      api.framesEstimate(bid, paths, opts(F)).then((e) => { if (tok === F.tok) { F.est = e; paintEst(); } })
        .catch((e: unknown) => { if (tok === F.tok) { F.estErr = errText(e); paintEst(); } });
    }, 250);
  };
  const player = one && !streamed
    ? `<video class="dc-fr-player" controls preload="metadata" playsinline src="${esc(api.fileUrl(bid, one))}"></video>` : '';
  const body = `<div class="dc-send dc-fr">${player}<div id="dc-fr-meta">${meta(null, one ? 1 : paths.length)}</div>${form(F, bid, one, canInplace, name, stem)}</div>`;

  await openDialog({
    title: one ? `Extract frames · ${baseName(one)}` : 'Extract frames from videos', wide: true, ok: 'Extract frames', body,
    bind: (r) => {
      root = r;
      syncRanges();
      if (one) {
        api.videoInfo(bid, one).then((i) => { F.info = i; const m = r.querySelector('#dc-fr-meta'); if (m) m.innerHTML = meta(i, 1); })
          .catch((e: unknown) => { const m = r.querySelector('#dc-fr-meta'); if (m) m.innerHTML = `<div class="status-line err">${icon('alert')}<span>${esc(errText(e))}</span></div>`; });
      } else {
        const m = r.querySelector('#dc-fr-meta');
        if (m) m.innerHTML = meta(null, paths.length > 1 ? paths.length : 2);
      }
      estimate();
      r.addEventListener('click', (e) => {
        const t = e.target instanceof Element ? e.target : null;
        const el = t?.closest<HTMLElement>('[data-preset], [data-fmt], [data-layout], [data-target], [data-fr]');
        if (!el || el.hasAttribute('disabled')) return;
        const d = el.dataset;
        if (d['preset']) F.preset = d['preset'] as Preset;
        else if (d['fmt']) F.format = d['fmt'] as FramesFormat;
        else if (d['layout']) F.layout = d['layout'] as FramesLayout;
        else if (d['target']) F.target = d['target'] === 'copy' ? 'copy' : 'inplace';
        else if (d['fr'] === 'clear-range') { F.start = null; F.end = null; }
        else if (d['fr'] === 'set-start' || d['fr'] === 'set-end') {
          const v = video();
          if (!v) return;
          const now = Math.round(v.currentTime * 100) / 100;
          if (d['fr'] === 'set-start') { F.start = now; if (F.end != null && F.end <= now) F.end = null; }
          else if (now > (F.start ?? 0)) F.end = now;
          else { toast('The end must come after the start', 'err'); return; }
        } else return;
        paint();
        estimate();
      });
      const read = (el: HTMLInputElement | HTMLSelectElement): boolean => {
        const k = el.dataset['f'];
        if (!k) return false;
        const v = el.value;
        const n = Number(v);
        switch (k) {
          case 'out': F.out = v; return false;
          case 'newName': F.newName = v; return false;
          case 'remember': F.remember = (el as HTMLInputElement).checked; return false;
          case 'after': F.after = v as After; return false;
          case 'pick': F.pick = v === 'consecutive' ? 'consecutive' : 'spread'; return true;
          case 'maxSide': F.maxSide = v ? Number(v) : null; return true;
          case 'dedupe': F.dedupe = n; return true;
          case 'quality': {
            F.quality = n;
            const q = r.querySelector('#dc-fr-qv');
            if (q) q.textContent = String(n);
            return true;
          }
          case 'threshold': {
            F.threshold = n;
            const q = r.querySelector('#dc-fr-thv');
            if (q) q.textContent = n.toFixed(2);
            return true;
          }
          default:
            if (v === '' || !Number.isFinite(n) || n <= 0) return false;
            (F as unknown as Record<string, number>)[k] = n;
            return true;
        }
      };
      r.addEventListener('input', (e) => {
        const t = e.target;
        if ((t instanceof HTMLInputElement || t instanceof HTMLSelectElement) && read(t)) estimate();
        if (t instanceof HTMLInputElement && t.type === 'range') syncRanges();
        if (t instanceof HTMLInputElement && t.dataset['f'] === 'out') {
          const pre = r.querySelector('.dc-al-tree');
          if (pre) pre.textContent = treeOf(F, F.out.trim() || name, stem);
        }
      });
      r.addEventListener('change', (e) => {
        const t = e.target;
        if (t instanceof HTMLSelectElement || (t instanceof HTMLInputElement && t.type === 'checkbox')) read(t);
      });
    },
    onOk: async () => {
      if (F.target === 'copy' && !F.newName.trim()) return 'Name the new bucket';
      if (F.out.trim().startsWith('.') || /[/\\]/.test(F.out)) return 'The folder name cannot start with a dot or hold / or \\';
      if (F.est && F.est.frames === 0) return 'No frame matches these settings: check the range and the numbers';
      if (F.busy) return null;
      F.busy = true;
      let res;
      try {
        res = await api.frames({
          bucket: bid, paths, opts: opts(F), target: F.target, remember: F.remember && canInplace,
          ...(F.out.trim() ? { out: F.out.trim() } : {}), ...(F.target === 'copy' ? { new_name: F.newName.trim() } : {}),
        });
      } catch (e) {
        F.busy = false;
        return `Could not start: ${errText(e)}`;
      }
      video()?.pause();
      toast(`Cutting ${one ? baseName(one) : `${paths.length === 1 ? baseName(folder) || b.name : `${paths.length} videos`}`} into ${res.path}…`);
      if (F.target === 'copy' || F.remember) void hooks.reload();
      if (res.job_id != null) void track(res.job_id, F.after);
      else if (res.result) { await hooks.reload(); hooks.navigate(res.bucket, res.path); }
      return null;
    },
  });
}

/** The videos an action is about: data-paths, else the selected videos, else the folder shown. */
function targetPaths(el: HTMLElement, bid: string): string[] {
  const p = el.dataset['paths'];
  if (p !== undefined) return p ? p.split('\n') : [''];
  if (D.bid === bid) {
    const vids = selEntries().filter((e) => e.kind === 'video').map((e) => e.path);
    if (vids.length) return vids;
    return [D.path];
  }
  return [''];
}

export const FRAMES_ACTIONS: Record<string, Action> = {
  'extract-frames': (el) => {
    const bid = el.dataset['bid'] || D.bid || '';
    if (bid) void openFrames(bid, targetPaths(el, bid));
  },
  'video-auto-off': (el) => {
    const bid = el.dataset['bid'] || D.bid || '';
    if (!bid) return;
    api.setVideoAuto(bid, null).then(() => { toast('Auto-extract is off'); void hooks.reload(); })
      .catch((e: unknown) => toast(`Could not turn it off: ${errText(e)}`, 'err'));
  },
};
