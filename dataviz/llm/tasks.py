"""
The structured assistant features (JSON answers, validated server side, every request budget-checked):

label_check   box crops in mosaics of 16 -> per box ok / wrong / unsure + a suggested class
nl_filter     a sentence -> a filter spec in the exact shape of static/js/core/filter.js emptyFilter()
auto_tag      a job: 8 whole images per request -> `ai:<tag>` tags in the session's tags.json
label_assist  a label-page image + the task's classes + exemplar sheet -> {answer, boxes} in normalised x1y1x2y2
voice         a WAV clip -> {transcript, action, args}, the action from the screen's whitelist
Prompts live next to the code that parses their answers.
"""

import math
import time

from ..core import logging as vlog
from . import budget, context, providers
from .providers import LLMError

_log = vlog.get_logger('llm')
CHECK_BATCH = 16
TAG_BATCH = 8
TAG_VOCAB = ['night', 'day', 'rain', 'fog', 'snow', 'blur', 'glare', 'crowded', 'occluded', 'indoor', 'outdoor',
             'low_light', 'overexposed', 'aerial', 'close_up']
VOICE_ACTIONS = {'viz': ('ask', 'tab', 'filter', 'search', 'none'),
                 'label': ('next', 'prev', 'skip', 'commit', 'undo', 'class', 'ask', 'none')}
VIZ_TABS = ('ai', 'explore', 'viz', 'stats', 'curation', 'model', 'eval', 'label')      # index.html data-tab


def _add(u, v):
    return {'in': u['in'] + (v or {}).get('in', 0), 'out': u['out'] + (v or {}).get('out', 0)}


def _ask(p, system, parts, limit):
    """complete_json on one user turn, budget-checked."""
    msgs, _ = budget.fit(p['kind'], system, [], [{'role': 'user', 'parts': parts}], limit)
    return providers.complete_json(p, system, msgs)


def _class_by_name(classes):
    return {str(n).strip().casefold(): i for i, n in enumerate(classes)}


# ---------------------------------------------------------------- label check
CHECK_SYSTEM = ('You check object-detection labels. Each numbered crop shows one labelled box (centred, with some '
                'margin). Decide whether the crop really shows the labelled class. Answer JSON only: '
                '{"items": [{"n": <crop number>, "verdict": "ok" | "wrong" | "unsure", "suggest": <a class name from '
                'the list or null>, "why": <short reason>}]} with one item per crop.')


def label_check(p, state, ids, limit):
    ids = [int(i) for i in ids if 0 <= int(i) < len(state.samples)]
    names = _class_by_name(state.classes)
    items, usage = [], {'in': 0, 'out': 0}
    for b in range(0, len(ids), CHECK_BATCH):
        chunk = ids[b:b + CHECK_BATCH]
        jpeg, legend, shown = context.selection_mosaic(state, chunk, max_n=CHECK_BATCH)
        text = (f'Classes: {", ".join(state.classes)}.\n{legend}\n'
                'For each crop: is the labelled class right?')
        ans, u = _ask(p, CHECK_SYSTEM, [{'jpeg': jpeg}, {'text': text}], limit)
        usage = _add(usage, u)
        got = {}
        rows = ans.get('items') if isinstance(ans, dict) else ans if isinstance(ans, list) else []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            try:
                n = int(r.get('n'))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(shown):
                got[shown[n - 1]] = r
        for i in shown:
            r = got.get(i) or {}
            v = str(r.get('verdict') or 'unsure').lower()
            v = v if v in ('ok', 'wrong', 'unsure') else 'unsure'
            sug = r.get('suggest')
            sug = state.classes[names[str(sug).strip().casefold()]] if sug and str(sug).strip().casefold() in names else None
            cls = state.classes[int(state.labels[i])] if 0 <= int(state.labels[i]) < len(state.classes) else str(state.labels[i])
            if sug == cls:
                sug = None
            items.append({'id': i, 'cls': cls, 'verdict': v, 'suggest': sug,
                          'why': str(r.get('why') or ('no answer for this crop' if not r else ''))[:300]})
    return {'items': items, 'usage': usage}


# ---------------------------------------------------------------- natural-language filter
EMPTY_FILTER = {
    'scope': 'image', 'name': {'mode': 'substr', 'q': '', 'neg': False}, 'cls': {'mode': 'any', 'ids': []},
    'nbox': [None, None], 'area': [None, None], 'aspect': [None, None], 'brightness': [None, None],
    'blur': [None, None], 'session': [], 'tags': [], 'm_status': [], 'suspicion_min': None, 'outlier_min': None,
    'issues': [], 'clip': {'q': '', 'k': 500},
}

FILTER_SYSTEM = '''You turn a request into a filter for an object-detection dataset viewer. Answer JSON only:
{"spec": <filter>, "explain": <one short sentence of what the filter keeps>}
<filter> has exactly this shape (the empty filter keeps everything):
{"scope": "image", "name": {"mode": "substr", "q": "", "neg": false}, "cls": {"mode": "any", "ids": []},
 "nbox": [null, null], "area": [null, null], "aspect": [null, null], "brightness": [null, null], "blur": [null, null],
 "session": [], "tags": [], "m_status": [], "suspicion_min": null, "outlier_min": null, "issues": [],
 "clip": {"q": "", "k": 500}}
Fields:
- scope: "image" keeps whole images that have a matching box, "box" keeps only the matching boxes.
- name: image file name match; mode substr | glob | regex | fuzzy; neg true = does NOT match.
- cls: class ids (numbers from the class list); mode any (has one of) | all (has all) | none (has none of).
- nbox: [min, max] boxes per image. area: [min, max] box area in % of the image (0-100).
- aspect: [min, max] box width / height. brightness: [min, max] mean box brightness 0-255 (dark < 60).
- blur: [min, max] laplacian variance of the box (low = blurry, < 50 is blurry). Ranges use null for "no limit".
- tags: tag names from the list (a box must have one). issues: curation issue types from the list.
- suspicion_min / outlier_min: 0-1 scores (likely mislabel / outlier), null = off.
- clip: text search by image content ("q": a short visual description, "k": how many hits) - only when the request
  describes what is visible and no other field can express it.
- Leave session and m_status empty. Use only class ids, tags and issue types that exist below.'''


def _range(v):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return [None, None]
    out = []
    for x in v:
        try:
            f = float(x)
            out.append(f if math.isfinite(f) else None)
        except (TypeError, ValueError):
            out.append(None)
    return out


def clean_filter(spec, classes, tags=(), issues=()):
    """A model's filter spec -> the emptyFilter() shape with only known class ids / tags / issue types
    (class names are mapped to ids, case-insensitively). Never raises."""
    f = spec if isinstance(spec, dict) else {}
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in EMPTY_FILTER.items()}
    out['scope'] = 'box' if f.get('scope') == 'box' else 'image'
    nm = f.get('name') if isinstance(f.get('name'), dict) else {}
    out['name'] = {'mode': nm.get('mode') if nm.get('mode') in ('substr', 'glob', 'regex', 'fuzzy') else 'substr',
                   'q': str(nm.get('q') or ''), 'neg': bool(nm.get('neg'))}
    cl = f.get('cls') if isinstance(f.get('cls'), dict) else {}
    by_name = _class_by_name(classes)
    ids = []
    for x in list(cl.get('ids') or []) + list(cl.get('names') or []):
        if isinstance(x, bool):
            continue
        if isinstance(x, (int, float)) and 0 <= int(x) < len(classes):
            ids.append(int(x))
        elif isinstance(x, str):
            if x.strip().isdigit() and int(x) < len(classes):
                ids.append(int(x))
            elif x.strip().casefold() in by_name:
                ids.append(by_name[x.strip().casefold()])
    out['cls'] = {'mode': cl.get('mode') if cl.get('mode') in ('any', 'all', 'none') else 'any',
                  'ids': sorted(set(ids))}
    for k in ('nbox', 'area', 'aspect', 'brightness', 'blur'):
        out[k] = _range(f.get(k))
    tset = {str(t) for t in tags}
    out['tags'] = [str(t) for t in f.get('tags') or [] if str(t) in tset] if isinstance(f.get('tags'), list) else []
    iset = {str(t) for t in issues}
    out['issues'] = [str(t) for t in f.get('issues') or [] if str(t) in iset] if isinstance(f.get('issues'), list) else []
    for k in ('suspicion_min', 'outlier_min'):
        try:
            v = float(f.get(k)) if f.get(k) is not None else None
            out[k] = v if v is not None and math.isfinite(v) else None
        except (TypeError, ValueError):
            out[k] = None
    cq = f.get('clip') if isinstance(f.get('clip'), dict) else {}
    try:
        k = int(cq.get('k') or 500)
    except (TypeError, ValueError):
        k = 500
    out['clip'] = {'q': str(cq.get('q') or ''), 'k': max(1, min(20000, k))}
    return out


def nl_filter(p, text, classes, tags, issues, limit):
    cls = '\n'.join(f'{i}: {n}' for i, n in enumerate(classes))
    msg = (f'Classes (id: name):\n{cls}\nTags: {", ".join(tags) or "(none)"}\n'
           f'Issue types: {", ".join(issues) or "(none: no curation scan)"}\n\nRequest: {text}')
    ans, usage = _ask(p, FILTER_SYSTEM, [{'text': msg}], limit)
    spec = ans.get('spec', ans) if isinstance(ans, dict) else {}
    explain = str(ans.get('explain') or '') if isinstance(ans, dict) else ''
    return {'spec': clean_filter(spec, classes, tags, issues), 'explain': explain[:400], 'usage': usage}


# ---------------------------------------------------------------- auto-tag (job)
TAG_SYSTEM = ('You tag images of a dataset by their conditions. Each numbered cell is one image. Use only tags from '
              'the given list, none when nothing applies. Answer JSON only: {"images": [{"n": <cell number>, '
              '"tags": [<tag>, ...]}]} with one item per cell.')


def clean_vocab(vocab):
    out = []
    for v in vocab or TAG_VOCAB:
        t = ''.join(ch if ch.isalnum() or ch in '_-' else '_' for ch in str(v).strip().lower())[:32].strip('_')
        if t and t not in out:
            out.append(t)
    return out[:40] or list(TAG_VOCAB)


def auto_tag_job(p, state, names, vocab, limit):
    """fn(job) for JobManager: tags `names` in batches; writes ai:<tag> -> box ids (every live box of a tagged
    image) after each batch, replacing the earlier ai: tags of the images it looked at. Stops on cancel."""
    vocab = clean_vocab(vocab)
    allowed = set(vocab)

    def fn(job):
        total = len(names)
        job.progress(0, total, stage='tagging images')
        counts, done = {}, 0
        dm = state.deleted_mask()
        for b in range(0, total, TAG_BATCH):
            if job.cancelled.is_set():
                break
            chunk = names[b:b + TAG_BATCH]
            jpeg, _ = context.image_mosaic(state, chunk)
            text = f'Tags: {", ".join(vocab)}.\n' + '\n'.join(f'#{k + 1}: {n}' for k, n in enumerate(chunk))
            try:
                ans, _ = _ask(p, TAG_SYSTEM, [{'jpeg': jpeg}, {'text': text}], limit)
            except LLMError as e:
                _log.warning(f'auto-tag batch {b // TAG_BATCH + 1}: {vlog.esc(e)}')
                if b == 0:
                    raise                                   # the first batch failing: say so instead of a silent 0
                done += len(chunk)
                job.progress(done, total)
                continue
            got = {}
            for r in (ans.get('images') if isinstance(ans, dict) else ans) or []:
                try:
                    n = int(r.get('n'))
                except (AttributeError, TypeError, ValueError):
                    continue
                if 1 <= n <= len(chunk):
                    got[chunk[n - 1]] = {str(t).strip().lower() for t in r.get('tags') or []} & allowed
            boxes = {n: [i for i in state.by_image.get(n, ()) if dm is None or not dm[i]] for n in chunk}
            with state.tags_lock:
                tags = state.load_tags()
                for n in chunk:                             # re-tagging replaces the old ai: tags of these images
                    mine = set(boxes[n])
                    for t in vocab:
                        k = f'ai:{t}'
                        if k in tags:
                            tags[k] = sorted(set(tags[k]) - mine)
                    for t in got.get(n, ()):
                        k = f'ai:{t}'
                        tags[k] = sorted(set(tags.get(k, [])) | mine)
                        counts[k] = counts.get(k, 0) + 1
                for k in [k for k, v in tags.items() if k.startswith('ai:') and not v]:
                    tags.pop(k)
                state.save_tags(tags)
            done += len(chunk)
            job.progress(done, total)
        return {'images': done, 'tags': counts}
    return fn


# ---------------------------------------------------------------- label page: assist / prelabel
def assist_system(fmt):
    coords = {'norm1000_yxyx': '[ymin, xmin, ymax, xmax] normalised to 0-1000',
              'norm1000_xyxy': '[xmin, ymin, xmax, ymax] normalised to 0-1000',
              'pixel_xyxy': '[xmin, ymin, xmax, ymax] in pixels of the image as you see it'}[fmt]
    return ('You are a labeling assistant for an object-detection labeling tool. The user labels images with the '
            'task classes listed below; the example sheet shows real labelled crops of each class (class name on '
            'the left of each row): follow those examples for what counts as each class and how tight boxes are. '
            'Answer JSON only: {"answer": <short helpful text>, "boxes": [{"class": <class name from the list>, '
            f'"box": {coords}, "note": <short note or "">}}]}}. Give boxes only for objects you can see; an '
            'empty list when the question needs none.')


def to_norm(box, fmt, w, h):
    """A model box in the provider's format -> (x1, y1, x2, y2) normalised 0..1, clamped; None when invalid
    or degenerate."""
    try:
        a, b, c, d = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if fmt == 'norm1000_yxyx':
        x1, y1, x2, y2 = b / 1000, a / 1000, d / 1000, c / 1000
    elif fmt == 'pixel_xyxy':
        if not w or not h:
            return None
        x1, y1, x2, y2 = a / w, b / h, c / w, d / h
    else:
        x1, y1, x2, y2 = a / 1000, b / 1000, c / 1000, d / 1000
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
        return None
    cl = lambda v: min(1.0, max(0.0, v))  # noqa: E731
    x1, x2 = sorted((cl(x1), cl(x2)))
    y1, y2 = sorted((cl(y1), cl(y2)))
    if x2 - x1 < 1e-3 or y2 - y1 < 1e-3:
        return None
    return round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5)


def parse_boxes(ans, fmt, size, task_classes):
    """The model's {answer, boxes} -> contract boxes [{class_name, class_id | None, x1, y1, x2, y2, note}]."""
    by_name = {c['name'].strip().casefold(): c for c in task_classes}
    out = []
    rows = ans.get('boxes') if isinstance(ans, dict) else ans if isinstance(ans, list) else []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        nb = to_norm(r.get('box') or r.get('bbox') or r.get('box_2d'), fmt, *size)
        if nb is None:
            continue
        name = str(r.get('class') or r.get('label') or r.get('class_name') or '').strip()
        c = by_name.get(name.casefold())
        out.append({'class_name': c['name'] if c else name, 'class_id': c['id'] if c else None,
                    'x1': nb[0], 'y1': nb[1], 'x2': nb[2], 'y2': nb[3], 'note': str(r.get('note') or '')[:200]})
    return out


def label_assist(p, task, image, sheets, question, limit, prelabel=False, existing=()):
    """image: (jpeg, (w, h)) as prepared for the model; sheets: exemplar sheet JPEGs (cached, sent first so the
    request prefix stays identical between questions)."""
    fmt = p.get('box_format') or 'norm1000_xyxy'
    jpeg, size = image
    names = [c['name'] for c in task['classes']]
    head = [{'text': f'Task "{task["name"]}" classes: {", ".join(names) or "(none yet)"}.\nExample sheet(s):'}]
    head += [{'jpeg': s, 'cache': True} for s in sheets]
    q = (f'Find every object of the task classes in this image ({size[0]}x{size[1]} px) and give its box. Keep the '
         'answer to one short sentence.') if prelabel else question
    tail = [{'text': 'The image to label:'}, {'jpeg': jpeg}]
    if existing:
        tail.append({'text': f'Already labelled here: {", ".join(existing)}.'})
    tail.append({'text': q})
    msgs, _ = budget.fit(p['kind'], assist_system(fmt), head, [{'role': 'user', 'parts': tail}], limit)
    ans, usage = providers.complete_json(p, assist_system(fmt), msgs)
    answer = str(ans.get('answer') or '') if isinstance(ans, dict) else ''
    return {'answer': answer[:2000], 'boxes': parse_boxes(ans, fmt, size, task['classes']), 'usage': usage}


# ---------------------------------------------------------------- voice
def voice_system(screen, classes):
    acts = {'viz': ('ask {"text"}: a question for the assistant; tab {"tab": one of ' + ', '.join(VIZ_TABS) + '}; '
                    'filter {"text"}: a description of which images / boxes to show; search {"text"}: find images '
                    'by what they show; none {}'),
            'label': ('next {}; prev {}; skip {}; commit {}: done with this image; undo {}; class {"name": a class '
                      'from the list}: pick that class; ask {"text"}: a question for the labeling assistant; none {}')}
    return (f'You are a voice command parser. Transcribe the clip, then map it to one action. Actions: {acts[screen]}. '
            + (f'Classes: {", ".join(classes)}. ' if classes else '')
            + 'Answer JSON only: {"transcript": <what was said>, "action": <action>, "args": <object>}.')


def voice(p, wav, screen, classes, limit):
    screen = screen if screen in VOICE_ACTIONS else 'viz'
    classes = [str(c) for c in classes or []][:200]
    system = voice_system(screen, classes)
    msgs, _ = budget.fit(p['kind'], system, [], [{'role': 'user', 'parts': [{'wav': wav}, {'text': 'The command:'}]}],
                         limit)
    t0 = time.perf_counter()
    ans, usage = providers.complete_json(p, system, msgs)
    ans = ans if isinstance(ans, dict) else {}
    transcript = str(ans.get('transcript') or '')[:1000]
    action = str(ans.get('action') or 'none').strip().lower()
    args = ans.get('args') if isinstance(ans.get('args'), dict) else {}
    if action not in VOICE_ACTIONS[screen]:
        action, args = ('ask', {'text': transcript}) if transcript else ('none', {})
    if action == 'class':
        by = {c.casefold(): c for c in classes}
        name = by.get(str(args.get('name') or '').strip().casefold())
        action, args = ('class', {'name': name}) if name else ('none', {})
    elif action == 'tab':
        tab = str(args.get('tab') or '').strip().lower()
        action, args = ('tab', {'tab': tab}) if tab in VIZ_TABS else ('none', {})
    elif action in ('ask', 'filter', 'search'):
        args = {'text': str(args.get('text') or transcript)[:1000]}
    else:
        args = {}
    return {'transcript': transcript, 'action': action, 'args': args, 'usage': usage,
            'ms': round((time.perf_counter() - t0) * 1000)}
