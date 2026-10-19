"""
The ◆ AI assistant API (/api/llm/*): saved providers + feature switches (Settings › AI assistant), the connection
test, the models list, and the assistant features. Works without an open session except where a feature reads the
dataset (409 then). Every feature route checks its switch (and that the active provider is connected) and answers
403 otherwise, so a feature hidden on the page cannot be called either.

Provider calls block: handlers are plain `def` (FastAPI's thread pool); the chat streams NDJSON from a sync
generator, and a provider error mid-stream becomes a {"t": "error"} line. Auto-tag is a job ('io' lane).

Everything runs on the CALLER's providers and switches (llm/store.py is per user). The dataset features need
the visualizer session the request is bound to (/s/<id>/: viewer; auto-tag writes box tags: labeler); the
label page features need the labeling session (assist: viewer; prelabel / accept: labeler).
"""

import base64
import binascii
import json
import threading
import time
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ...auth import access, deps
from ...auth import context as auth_context
from ...core import logging as vlog
from ...llm import budget, context, providers, tasks
from ...llm.providers import LLMError

_log = vlog.get_logger('llm')
MAX_CHECK = 96
MAX_WAV = 8 << 20               # ~4 min of 16 kHz mono: the page sends <= 30 s

CHAT_SYSTEM = ('You are the ◆ assistant of a dataset visualizer for YOLO object-detection datasets (images, '
               'labelled boxes, classes, curation issues, model evaluations). Answer concisely in Markdown; use '
               'tables for numbers. Base answers on the context given (dataset summary, evaluation, box crops); say '
               'so when it does not contain the answer instead of guessing. Box crops are numbered #k; refer to them '
               'by that number.')
REPORT_TASK = ('Write a dataset report in Markdown: overview, class balance (a table), data quality and curation '
               'issues, model results and findings if given, and a prioritised list of recommended next steps '
               '(what to fix, what data to collect). Use only the context given.')


@contextmanager
def _llm_errors():
    try:
        yield
    except HTTPException:
        raise
    except LLMError as e:
        raise HTTPException(e.status, str(e))


def _wav(b64):
    try:
        data = base64.b64decode(b64 or '', validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(400, 'the voice clip is not valid base64')
    if not data:
        raise HTTPException(400, 'empty voice clip')
    if len(data) > MAX_WAV:
        raise HTTPException(400, 'the voice clip is too long')
    return data


_KW = {'param': 'pid', 'kind': 'label', 'what': 'label session'}
LABEL_VIEWER = deps.require_session('viewer', **_KW)
LABEL_LABELER = deps.require_session('labeler', **_KW)


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.current_user)])
    sources = {}                    # label pid -> (source json, ImageSource, ImageCache)
    sources_lock = threading.Lock()

    def store():
        if ctx.llm is None:
            raise HTTPException(501, 'the AI assistant is not enabled in this server')
        return ctx.llm

    def need(feature):
        """The caller's connected provider for a feature they switched on, else 403."""
        s = store()
        if not s.on(feature):
            raise HTTPException(403, f'AI feature "{feature}" is off (Settings › AI assistant)')
        return s.active()

    def limit():
        return store().load()['budget']['max_input_tokens']

    def session(need_role='viewer'):
        """The State of the visualizer session this request is bound to (/s/<id>/), the caller's role high enough."""
        b = auth_context.bound()
        if b is None:
            raise HTTPException(409, 'no session open')
        if not access.allows(b.role, need_role):
            raise HTTPException(403, 'you can only view this session')
        st = b.state
        if st.index is None:
            raise HTTPException(503, 'the dataset is still being indexed')
        return st

    def admin():
        u = auth_context.user()
        return bool(u and u.get('is_admin'))

    def form_provider(body, need_model=True):
        """{id} -> the saved provider; {form} -> an unsaved one (form.id reuses that provider's stored key)."""
        s = store()
        if body.get('id') and not body.get('form'):
            p = s.provider(body['id'])                  # the caller's own only
            if p is None:
                raise HTTPException(404, f'no provider {body["id"]!r}')
            return p
        form = body.get('form')
        if not isinstance(form, dict):
            raise HTTPException(400, 'give a provider id or a form')
        old = s.provider(form['id']) if form.get('id') else None
        f = dict(form)
        if not need_model and not str(f.get('model') or (old or {}).get('model') or '').strip():
            f['model'] = '-'                    # listing models comes before picking one
        try:
            return s.merged(f, old, admin=admin())     # an env: key typed by a non-admin: 400, never resolved
        except ValueError as e:
            raise HTTPException(400, str(e))

    @r.get('/api/llm/state')
    def llm_state():
        return store().state()

    @r.get('/api/llm/config')
    def llm_config():
        return store().config()

    @r.put('/api/llm/config')
    def put_config(body: dict):
        store().set_config(body)
        return store().config()

    @r.post('/api/llm/providers', dependencies=[Depends(deps.can_add_connections)])
    def save_provider(body: dict):
        try:
            p = store().upsert(body)
        except KeyError:
            raise HTTPException(404, f'no provider {body.get("id")!r}') from None
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {'provider': store().public(p), **store().config()}

    @r.delete('/api/llm/providers/{pid}')
    def delete_provider(pid: str):
        try:
            store().delete(pid)
        except KeyError:
            raise HTTPException(404, f'no provider {pid!r}') from None
        return store().config()

    @r.post('/api/llm/test', dependencies=[Depends(deps.can_add_connections)])
    def test_provider(body: dict):
        p = form_provider(body)
        res = providers.test(p)
        if body.get('id') and not body.get('form'):
            store().record_test(p['id'], res)
        return res

    @r.post('/api/llm/models', dependencies=[Depends(deps.can_add_connections)])
    def provider_models(body: dict):
        p = form_provider(body, need_model=False)
        with _llm_errors():
            return {'models': providers.list_models(p)}

    # ------------------------------------------------------------ chat (NDJSON stream)
    def selection_parts(st, ids):
        jpeg, legend, _ = context.selection_mosaic(st, ids)
        return [{'jpeg': jpeg}, {'text': legend}] if jpeg else []

    @r.post('/api/llm/chat')
    def chat(body: dict):
        p = need('assistant')
        msgs_in = body.get('messages') or []
        c = body.get('ctx') or {}
        task = body.get('task')
        if not msgs_in or not isinstance(msgs_in, list) or msgs_in[-1].get('role') != 'user':
            raise HTTPException(400, 'the last message must be the user\'s')
        if c.get('dataset'):
            need('dataset_qa')
        if c.get('eval'):
            need('explain_eval')
        sel = [int(i) for i in c.get('selection') or [] if isinstance(i, (int, float, str)) and str(i).lstrip('-').isdigit()]
        if sel or any(m.get('image_ids') for m in msgs_in):
            need('ask_selection')
        if task == 'report':
            need('report')
        if any(m.get('wav_b64') for m in msgs_in):
            need('voice')
        needs_session = bool(c.get('dataset') or c.get('eval') or sel or task == 'report'
                             or any(m.get('image_ids') for m in msgs_in))
        st = session() if needs_session else None
        ctx_parts = []
        if c.get('dataset') or task == 'report':
            ctx_parts.append({'text': 'DATASET SUMMARY\n' + context.dataset_summary(st, ctx.curation(st))})
        if c.get('eval') or task == 'report':
            ev = context.eval_summary(ctx)
            ctx_parts.append({'text': 'EVALUATION\n' + (ev or '(no model has been evaluated on this dataset yet)')})
        if sel:
            ctx_parts += selection_parts(st, sel)
        history = []
        last = len(msgs_in) - 1
        for k, m in enumerate(msgs_in[-40:], start=max(0, len(msgs_in) - 40)):
            parts = []
            if m.get('image_ids') and k == last:     # older turns' images would be omitted by fit() anyway
                parts += selection_parts(st, m['image_ids'])
            if m.get('wav_b64') and k == last:
                parts.append({'wav': _wav(m['wav_b64'])})
            if str(m.get('text') or '').strip():
                parts.append({'text': str(m['text'])})
            elif m.get('role') == 'user' and k == last and task == 'report':
                parts.append({'text': 'Write the report.'})
            history.append({'role': 'assistant' if m.get('role') == 'assistant' else 'user', 'parts': parts})
        system = CHAT_SYSTEM + ('\n\n' + REPORT_TASK if task == 'report' else '')
        with _llm_errors():
            msgs, est = budget.fit(p['kind'], system, ctx_parts, history, limit())

        def gen():
            t0 = time.perf_counter()
            yield json.dumps({'t': 'meta', 'in_est': est}) + '\n'
            try:
                usage, n_chars = None, 0
                for kind, val in providers.chat_stream(p, system, msgs):
                    if kind == 'delta':
                        n_chars += len(val)
                        yield json.dumps({'t': 'delta', 'text': val}) + '\n'
                    else:
                        usage = val
                if not usage or not usage.get('in'):        # servers that report no usage: our estimates
                    usage = {'in': est, 'out': (usage or {}).get('out') or budget.text_tokens('x' * n_chars)}
                yield json.dumps({'t': 'done', 'usage': usage,
                                  'ms': round((time.perf_counter() - t0) * 1000)}) + '\n'
            except Exception as e:  # noqa: BLE001 - the page shows the message in the transcript
                msg = str(e) if isinstance(e, LLMError) else f'{type(e).__name__}: {e}'
                _log.warning(f'chat: {vlog.esc(msg)}')
                yield json.dumps({'t': 'error', 'error': msg}) + '\n'
        return StreamingResponse(gen(), media_type='application/x-ndjson', headers={'Cache-Control': 'no-cache'})

    # ------------------------------------------------------------ structured features
    @r.post('/api/llm/label-check')
    def label_check(body: dict):
        p = need('label_check')
        st = session()
        ids = body.get('ids') or []
        if not ids:
            raise HTTPException(400, 'select some boxes first')
        if len(ids) > MAX_CHECK:
            raise HTTPException(400, f'check at most {MAX_CHECK} boxes at a time ({len(ids)} selected)')
        with _llm_errors():
            return tasks.label_check(p, st, ids, limit())

    def issue_types(st):
        cur = ctx.curation(st)
        try:
            return list(cur.summary()) if cur is not None else []
        except Exception:  # noqa: BLE001
            return []

    @r.post('/api/llm/filter')
    def nl_filter(body: dict):
        p = need('nl_filter')
        st = session()
        text = str(body.get('text') or '').strip()
        if not text:
            raise HTTPException(400, 'describe what to show')
        tags = sorted(context._tags(st))
        with _llm_errors():
            res = tasks.nl_filter(p, text[:2000], st.classes, tags, issue_types(st), limit())
        return {'spec': res['spec'], 'explain': res['explain'], 'usage': res['usage']}

    @r.post('/api/llm/tag')
    def auto_tag(body: dict):
        p = need('auto_tag')
        st = session('labeler')                         # it writes box tags
        scope = body.get('scope') or 'all'
        if scope == 'selection':
            ids = [int(i) for i in body.get('ids') or [] if 0 <= int(i) < len(st.samples)]
            if not ids:
                raise HTTPException(400, 'select some boxes first')
            names = list(dict.fromkeys(st.samples[i]['image'] for i in ids))
        elif scope == 'all':
            names = sorted(st.by_image)
        else:
            raise HTTPException(400, "scope is 'all' or 'selection'")
        vocab = body.get('vocab') if isinstance(body.get('vocab'), list) else None
        fn = tasks.auto_tag_job(p, st, names, vocab, limit())    # p, limit: resolved now (jobs have no user)
        job = ctx.jobs.submit(f'llm-tag:{st.tag}', f'◆ auto-tag {len(names)} images', fn, lane='io', public_result=True)
        return {'job_id': job.id}

    @r.post('/api/llm/voice')
    def voice(body: dict):
        p = need('voice')
        wav = _wav(body.get('wav_b64'))
        screen = body.get('screen') if body.get('screen') in ('viz', 'label') else 'viz'
        classes = body.get('classes') if isinstance(body.get('classes'), list) else []
        with _llm_errors():
            res = tasks.voice(p, wav, screen, classes, limit())
        return {'transcript': res['transcript'], 'action': res['action'], 'args': res['args'], 'usage': res['usage']}

    # ------------------------------------------------------------ label page
    def label_store(pid):
        reg = getattr(ctx, 'labeling', None)
        if reg is None:
            raise HTTPException(501, 'labeling sessions are not enabled in this server')
        try:
            return reg, reg.store(pid)
        except KeyError as e:
            raise HTTPException(404, e.args[0] if e.args else 'no such labeling session')

    def label_cache(pid):
        """The project's ImageCache, built once per source setting (same as routes/labeling.py source_for)."""
        from ...labeling import media
        reg = ctx.labeling
        entry = reg.get(pid)
        key = json.dumps([entry['source'], entry['owner_id']], sort_keys=True)
        with sources_lock:
            hit = sources.get(pid)
            if hit and hit[0] == key:
                return hit[2]
            # the project owner's connections, like routes/labeling.py source_for
            src = media.make_source(entry['source'], reg.project_dir(pid), ctx.datasets.scoped(entry['owner_id']),
                                    (ctx.cfg or {}).get('s3', {}))
            cache = media.ImageCache(reg.project_dir(pid), src)
            sources[pid] = (key, src, cache)
        if hit:
            try:
                hit[1].close()
            except Exception:  # noqa: BLE001 - closing a stale pool is best effort
                pass
        return cache

    def media_bytes(pid, m):
        try:
            return label_cache(pid).path(m['uri']).read_bytes()
        except (OSError, ValueError, KeyError) as e:
            _log.warning(f'assist: image {vlog.esc(m.get("uri"))}: {vlog.esc(e)}')
            raise HTTPException(404, 'this image is missing from its source')

    def box_task(st, tid):
        from ...labeling import schema
        try:
            task = st.task(tid)
        except KeyError as e:
            raise HTTPException(404, e.args[0])
        if schema.TASK_GEOMETRY.get(task['task_type']) != 'bbox':
            raise HTTPException(400, f'the AI assistant draws boxes: task {task["name"]!r} is a '
                                     f'{task["task_type"].replace("_", " ")} task')
        return task

    def assist_run(pid, body, prelabel):
        p = need('prelabel' if prelabel else 'label_assist')
        _, st = label_store(pid)
        task = box_task(st, body.get('task_id'))
        try:
            m = st.media_row(body.get('mid'))
        except KeyError as e:
            raise HTTPException(404, e.args[0])
        question = str(body.get('question') or '').strip()
        if not prelabel and not question:
            raise HTTPException(400, 'ask a question')
        image = context.prepare_image(media_bytes(pid, m))
        sheets = context.task_exemplars(pid, st, task, lambda row: media_bytes(pid, row))
        names = {c['id']: c['name'] for c in task['classes']}
        existing = [names.get(a['class_id'], '?') for a in st.live_annotations(m['id'], task['id'])
                    if a['geometry_type'] == 'bbox']
        with _llm_errors():
            return tasks.label_assist(p, task, image, sheets, question[:2000], limit(), prelabel=prelabel,
                                      existing=existing)

    @r.post('/api/llm/label/{pid}/assist', dependencies=[Depends(LABEL_VIEWER)])
    def label_assist(pid: str, body: dict):
        return assist_run(pid, body, False)

    @r.post('/api/llm/label/{pid}/prelabel', dependencies=[Depends(LABEL_LABELER)])
    def label_prelabel(pid: str, body: dict):
        return assist_run(pid, body, True)

    @r.post('/api/llm/label/{pid}/accept', dependencies=[Depends(LABEL_LABELER)])
    def label_accept(pid: str, body: dict):
        s = store()
        if not (s.on('label_assist') or s.on('prelabel')):
            raise HTTPException(403, 'AI features "label_assist" and "prelabel" are off (Settings › AI assistant)')
        _, st = label_store(pid)
        task = box_task(st, body.get('task_id'))
        live = {c['id'] for c in task['classes']}
        preds = []
        for b in body.get('boxes') or []:
            if not isinstance(b, dict) or b.get('class_id') not in live:
                continue
            try:
                x1, y1, x2, y2 = (float(b[k]) for k in ('x1', 'y1', 'x2', 'y2'))
            except (KeyError, TypeError, ValueError):
                continue
            preds.append({'class_id': b['class_id'], 'score': 1.0, 'geometry_type': 'bbox',
                          'geometry': {'x': min(x1, x2), 'y': min(y1, y2), 'w': abs(x2 - x1), 'h': abs(y2 - y1)}})
        if not preds:
            raise HTTPException(400, 'no box with a class of this task to accept')
        try:
            return st.apply_predictions(body.get('mid'), task['id'], preds, replace_model=False)
        except KeyError as e:
            raise HTTPException(404, e.args[0] if e.args else 'not found')
        except ValueError as e:
            raise HTTPException(400, str(e))

    return r
