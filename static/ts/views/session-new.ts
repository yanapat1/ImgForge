// Create session (the Image page's one New dialog): pick a folder of a bucket, tick what to create (Visualize and
// Label, both by default: one linked session whose Label side owns the labels and whose Visualize side reads them
// live), see what the folder's format allows (GET api/bucket/buckets/{id}/session-check), name it, Create
// (POST api/bucket/session). Sessions are made only from Bucket data: a link opens the Bucket page to add more.
// One innerHTML render per change (the name field keeps its text), one delegated click / change listener.

import { errText, getJSON, postJSON } from '../core/api.js';
import { zoneOn } from '../core/auth.js';
import { esc, icon } from '../core/dom.js';
import { waitJob } from '../core/jobs.js';
import { bucketUrl, labelUrl, vizUrl } from '../core/nav.js';
import { toast } from '../core/toast.js';
import * as api from '../bucket/api.js';
import type { Bucket, Entry } from '../types-bucket.js';
import { openSettings } from '../settings/dialog.js';
import { openDialog } from './dialog.js';

interface Check {
  format: string; label: string | null; reason?: string | null; images: number | null; labels: number | null;
  classes: string[] | number | null; streamed: boolean; can: { visualize: boolean; label: boolean; linked: boolean };
  label_import: 'imported' | 'none'; note: string; convert_to: string[]; default: { visualize: boolean; label: boolean };
}
interface Made {
  name: string; linked: boolean; visualize?: { id: string; url: string };
  label?: { id: string; url: string; job_id?: number | null; import_job_id?: number | null; import_error?: string };
}
export interface CreateOpts { visualize?: boolean; label?: boolean; bucket?: string; path?: string }

const TASKS: [string, string][] = [['detect', 'Detect (boxes)'], ['segment', 'Segment (polygons)'], ['pose', 'Pose (keypoints)'], ['classify', 'Classify (tags)']];
const FMT_TASK: Record<string, string> = { 'yolo-seg': 'segment', 'yolo-pose': 'pose', classification: 'classify' };
const fmtN = (n: number | null | undefined): string => (n == null ? '–' : n.toLocaleString());

export async function openCreateSession(o: CreateOpts = {}): Promise<void> {
  const hasViz = zoneOn('visualizer'), hasLabel = zoneOn('labeling');
  const S = {
    buckets: [] as Bucket[], bid: o.bucket ?? '', path: o.path ?? '', dirs: [] as Entry[], loading: true, err: '',
    check: null as Check | null, viz: o.visualize ?? true, label: o.label ?? true, touched: o.visualize != null || o.label != null,
    task: 'detect', busy: '',
  };
  let root: HTMLElement | null = null;
  let seq = 0;

  const yes = (ok: boolean, text: string): string => `<span class="sn-can ${ok ? 'ok' : 'no'}">${icon(ok ? 'check' : 'x')}${text}</span>`;
  const formatHTML = (): string => {
    const c = S.check;
    if (S.loading) return '<div class="sn-fmt"><span class="ct-spin"></span><span class="muted">Reading the folder…</span></div>';
    if (S.err) return `<div class="note warn">${icon('alert')}<span>${esc(S.err)}</span></div>`;
    if (!c) return '';
    const cls = Array.isArray(c.classes) ? c.classes.length : c.classes;
    return `<div class="sn-fmt"><div class="sn-fmt-h"><b>${esc(c.label ?? c.format)}</b><span class="muted num">${fmtN(c.images)} images · ${fmtN(c.labels)} label files${cls ? ` · ${fmtN(cls)} classes` : ''}</span>` +
      (c.streamed ? `<span class="sn-tag" title="Read from S3 on every request: nothing is copied, and nothing is written to S3">${icon('cloud')}streamed from S3</span>` : '') + '</div>' +
      `<div class="sn-cans">${yes(c.can.visualize, 'Visualize')}${yes(c.can.label, 'Label')}${yes(c.can.linked, 'Both, linked')}` +
      `<span class="sn-can ${c.label_import === 'imported' ? 'ok' : 'off'}">${icon(c.label_import === 'imported' ? 'check' : 'circle')}${c.label_import === 'imported' ? 'its labels are imported' : 'no labels to import'}</span></div>` +
      `<p class="muted">${esc(c.note)}</p>` +
      (c.streamed ? '<p class="muted">Streamed data: the images stay in S3 and are read when needed (the first map reads each image once); labels made here are kept in this app, not written to S3.</p>' : '') +
      (c.convert_to.length ? `<div class="sn-other"><span class="muted">Re-format a copy to</span>${c.convert_to.map((x) => `<button type="button" class="ghost sm" data-sn="reformat" data-to="${esc(x)}" title="Writes a new folder in the bucket “… · formats”; this folder is not changed${c.streamed ? ' (labels only: the images stay in S3)' : ''}">${esc(x.toUpperCase())}</button>`).join('')}</div>` : '') + '</div>';
  };
  const crumbs = (): string => {
    const parts = S.path ? S.path.split('/') : [];
    const b = S.buckets.find((x) => x.id === S.bid);
    return `<button type="button" class="ghost sm" data-sn="go" data-path="">${icon('db')}${esc(b?.name ?? 'bucket')}</button>` +
      parts.map((p, i) => `<span class="faint">/</span><button type="button" class="ghost sm" data-sn="go" data-path="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</button>`).join('');
  };
  const whyOff = (): string => {
    const c = S.check;
    if (!c) return '';
    if (S.viz && S.label && !c.can.linked) return 'This folder cannot be a linked Visualize + Label session: untick one.';
    if (S.viz && !S.label && !c.can.visualize) return 'Visualize alone cannot read this folder.';
    if (S.label && !S.viz && !c.can.label) return 'A Label session cannot be made from this folder.';
    return !S.viz && !S.label ? 'Tick Visualize, Label or both.' : '';
  };
  const body = (): string => {
    if (!S.buckets.length && !S.loading) {
      return `<div class="sn"><div class="note info">${icon('alert')}<span>You have no bucket yet. Sessions are made from data in Bucket: add your data there first.</span></div>` +
        `<p><a href="${esc(bucketUrl(null, null))}">Add data in Bucket</a></p></div>`;
    }
    const kind = (on: boolean, k: string, ico: string, title: string, txt: string, ok: boolean): string =>
      `<label class="dest-o sn-kind${on ? ' on' : ''}${ok ? '' : ' off'}"><input type="checkbox" data-sn="kind" value="${k}"${on ? ' checked' : ''}>` +
      `<span class="dt"><b>${title}</b><span>${txt}</span></span>${icon(ico)}</label>`;
    const off = whyOff();
    return '<div class="sn">' +
      `<div class="m-lbl">Data <span class="faint">· a folder of a bucket, read in place</span></div>` +
      `<div class="sn-pick"><select data-sn="bucket" aria-label="Bucket">${S.buckets.map((b) => `<option value="${esc(b.id)}"${b.id === S.bid ? ' selected' : ''}>${esc(b.name)}${b.kind === 's3' ? ' (S3)' : ''}</option>`).join('')}</select>` +
      `<div class="sn-crumbs">${crumbs()}</div></div>` +
      (S.dirs.length ? `<div class="sn-dirs" role="list">${S.dirs.map((d) => `<button type="button" class="ghost sm" role="listitem" data-sn="go" data-path="${esc(d.path)}" title="Open ${esc(d.name)}">${icon('folder')}${esc(d.name)}</button>`).join('')}</div>` : '') +
      `<p class="hint">Not in a bucket yet? <a href="${esc(bucketUrl(null, null))}">Add it in Bucket</a> (upload, link a folder, S3, import from the web), then come back.</p>` +
      `<div class="m-lbl">Format</div>${formatHTML()}` +
      `<div class="m-lbl">Create</div><div class="dest">` +
      (hasViz ? kind(S.viz, 'viz', 'scatter', 'Visualize', 'The map: clusters, outliers, label errors, models', !!S.check && (S.label ? S.check.can.linked : S.check.can.visualize)) : '') +
      (hasLabel ? kind(S.label, 'label', 'edit', 'Label', 'Draw, fix and review annotations', !!S.check && (S.viz ? S.check.can.linked : S.check.can.label)) : '') + '</div>' +
      (S.viz && S.label ? '<p class="hint">Both: one linked session. The Label side keeps the labels and Visualize reads them live, so a box you fix shows on the map.</p>' : '') +
      (off ? `<div class="note warn">${icon('alert')}<span>${esc(off)}</span></div>` : '') +
      `<div class="f2"><label class="fld"><span>Session name</span><input type="text" class="txt" id="sn-name" autocomplete="off" placeholder="from the folder"></label>` +
      (S.label ? `<label class="fld"><span>Label task</span><select data-sn="task">${TASKS.map(([k, t]) => `<option value="${k}"${k === S.task ? ' selected' : ''}>${t}</option>`).join('')}</select></label>` : '') + '</div>' +
      (S.busy ? `<div class="sn-fmt"><span class="ct-spin"></span><span>${esc(S.busy)}</span></div>` : '') +
      (hasViz ? `<p class="hint sn-other">From sessions you already have: <button type="button" class="ghost sm" data-act="new-union">${icon('layers')}Combine sessions…</button>` +
        `<button type="button" class="ghost sm" data-act="new-labeling">${icon('tag')}Visualize a labeling task…</button></p>` : '') + '</div>';
  };
  const paint = (): void => {
    if (!root) return;
    const name = root.querySelector<HTMLInputElement>('#sn-name')?.value ?? '';
    const host = root.querySelector('.sn');
    if (host) host.outerHTML = body();
    const i = root.querySelector<HTMLInputElement>('#sn-name');
    if (i) i.value = name;
    const ok = root.closest('.ui-dlg')?.querySelector<HTMLButtonElement>('[data-dlg="ok"]');
    if (ok) ok.disabled = !!whyOff() || S.loading || !!S.err || !!S.busy || !S.check;
  };

  /** the folder's sub-folders and what a session from it can do */
  const open = async (bid: string, path: string): Promise<void> => {
    const mine = ++seq;
    Object.assign(S, { bid, path, loading: true, err: '', check: null, dirs: [] });
    paint();
    try {
      const [ls, chk] = await Promise.all([
        api.ls(bid, path).then((r) => r.entries.filter((e) => e.type === 'dir').slice(0, 200)).catch(() => [] as Entry[]),
        getJSON<Check>(`api/bucket/buckets/${encodeURIComponent(bid)}/session-check?path=${encodeURIComponent(path)}`),
      ]);
      if (mine !== seq) return;
      S.dirs = ls;
      S.check = chk;
      if (!S.touched) { S.viz = hasViz && chk.default.visualize; S.label = hasLabel && chk.default.label; }
      S.task = FMT_TASK[chk.format] ?? 'detect';
    } catch (e) {
      if (mine !== seq) return;
      S.err = errText(e);
    }
    S.loading = false;
    paint();
  };

  await openDialog({
    title: 'Create session', wide: true, ok: 'Create session', body: body(), focus: '#sn-name', dirty: () => true,
    bind: (r) => {
      root = r;
      r.addEventListener('click', (e) => {
        const go = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-sn="go"]') : null;
        if (go && !S.busy) void open(S.bid, go.dataset['path'] ?? '');
        const rf = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-sn="reformat"]') : null;
        if (rf && !S.busy) {
          postJSON<{ bucket_name: string; path: string }>('api/bucket/reformat', { bucket: S.bid, path: S.path, to: rf.dataset['to'] })
            .then((x) => toast(`Re-format started: it will be in ${x.bucket_name}/${x.path} (the Bucket page shows the job)`, 'ok'))
            .catch((err: unknown) => toast(`Could not re-format: ${errText(err)}`, 'err'));
        }
        // a session made from other sessions: the Settings form of that kind (closing this dialog)
        const other = e.target instanceof Element ? e.target.closest<HTMLElement>('[data-act="new-union"], [data-act="new-labeling"]') : null;
        if (other && !S.busy) {
          const act = other.dataset['act'] ?? '';
          r.closest('.ui-dlg')?.querySelector<HTMLElement>('[data-dlg="cancel"]')?.click();
          void openSettings('datasets', { solo: true, view: 'add' }).then(() => document.querySelector<HTMLElement>(`#settings-body [data-act="${act}"]`)?.click());
        }
      });
      r.addEventListener('change', (e) => {
        const el = e.target;
        if (el instanceof HTMLSelectElement && el.dataset['sn'] === 'bucket') void open(el.value, '');
        else if (el instanceof HTMLSelectElement && el.dataset['sn'] === 'task') S.task = el.value;
        else if (el instanceof HTMLInputElement && el.dataset['sn'] === 'kind') {
          if (el.value === 'viz') S.viz = el.checked; else S.label = el.checked;
          S.touched = true;
          paint();
        }
      });
      void api.listBuckets().then((bs) => {
        S.buckets = bs;
        if (!bs.length) { S.loading = false; paint(); return; }
        void open(bs.some((b) => b.id === S.bid) ? S.bid : (bs[0]?.id ?? ''), bs.some((b) => b.id === S.bid) ? S.path : '');
      }).catch((e: unknown) => { S.loading = false; S.err = errText(e); paint(); });
    },
    onOk: async (r) => {
      if (whyOff() || !S.check) return whyOff() || 'Pick a folder first.';
      const name = r.querySelector<HTMLInputElement>('#sn-name')?.value.trim() ?? '';
      try {
        S.busy = 'Creating the session…';
        paint();
        const made = await postJSON<Made>('api/bucket/session', { bucket: S.bid, path: S.path, visualize: S.viz, label: S.label, name: name || null, task_type: S.task });
        // the images, then the folder's own labels, are listed into the Label side before the map is opened on them
        for (const [jid, what] of [[made.label?.job_id, 'Listing the images'], [made.label?.import_job_id, 'Importing the labels']] as const) {
          if (jid == null) continue;
          S.busy = `${what}…`;
          paint();
          await waitJob(jid, (j) => { S.busy = `${what}… ${j.pct != null ? `${Math.round(j.pct)}%` : ''}`; paint(); });
        }
        location.href = made.visualize ? vizUrl(made.visualize.id) : labelUrl(made.label?.id ?? '');
        return null;
      } catch (e) {
        S.busy = '';
        paint();
        return `Could not create the session: ${errText(e)}`;
      }
    },
  });
}
