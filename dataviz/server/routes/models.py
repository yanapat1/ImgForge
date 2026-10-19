"""
Settings › Models and the Model tab: saved models (local files or HTTP APIs), uploads, a one-image test,
the prediction job over the open dataset, and the evaluation against its labels.
Model loading / inference runs off the event loop (run_io) or as a job with progress.

Saved models belong to a user (deps.owned passes owner_id to the registry when it is per-user). Managing them
needs only a sign-in; the routes that read the open dataset need viewer on its /s/<id>/ session, running a
prediction over it labeler.
"""

import io
import asyncio
import shutil
from pathlib import Path

import orjson
from PIL import Image, ImageDraw
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response, PlainTextResponse

from ...auth import deps
from ...core import logging as vlog
from ...data import sources
from ...models import insights, registry as mreg, schema
from ...paths import PROJECT_ROOT
from ..deps import guards, json_bytes, owned
from .samples import draw_preds

log = vlog.get_logger('models')


def eval_overview(ctx, split='val', conf_mode='best', conf=0.25, iou=None, focus=None):
    """The Evaluation tab's data (also the AI assistant's evaluation context): every model that ran on the open
    dataset compared, one (focus) model explained with the dataset, and the findings. Blocking."""
    reg, svc = ctx.models, ctx.model_service
    st = ctx.current()
    lb = insights.leaderboard(svc, st, owned(reg.models), split, conf_mode, conf, iou)
    fid = focus if focus in lb['per_class']['models'] else lb['best']
    ins = insights.dataset_insights(svc, st, owned(reg.model, fid), split, conf_mode, conf, iou) if fid else None
    _, src = svc.splits(st)
    return {'leaderboard': lb, 'focus': fid, 'insights': ins, 'split_source': src,
            'findings': insights.findings(lb, ins, st.classes, src, ctx.curation(st)),
            'dataset': {'name': st.source.name, 'uri': st.source.uri, 'images': len(st.by_image),
                        'labels': len(st.samples), 'classes': st.classes}}


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.current_user)])
    viewer = [Depends(deps.require_bound('viewer'))]
    g = guards(state)
    reg, svc = ctx.models, ctx.model_service

    def need():
        if reg is None:
            raise HTTPException(501, 'models are not enabled in this server')

    def model_or_404(mid):
        need()
        try:
            return owned(reg.model, mid)
        except KeyError as e:
            raise HTTPException(404, str(e))

    def run_io(fn, *args):
        return asyncio.to_thread(fn, *args)          # carries the contextvars (user, bound session) along

    def public(m):
        out = reg.public(m)
        st = ctx.state                          # None on the Sessions home: nothing predicted
        try:
            out['predicted'] = st is not None and svc.is_predicted(st, m)
        except Exception:  # noqa: BLE001 - a missing file must not break the list
            out['predicted'] = False
        if m.get('path'):
            out['exists'] = Path(m['path']).exists()
        return out

    @r.get('/api/models')
    def list_models():
        need()
        return {'models': [public(m) for m in owned(reg.models)]}

    @r.get('/api/models/discover')
    def discover():
        need()
        known = {m.get('path') for m in owned(reg.models)}
        return {'found': [d for d in mreg.discover(PROJECT_ROOT) if d['path'] not in known][:60]}

    @r.get('/api/models/spec')
    def spec():
        return {'custom': schema.CUSTOM_SPEC, 'matrix': mreg.MATRIX, 'families': mreg.FAMILIES,
                'formats': mreg.FORMATS, 'rfdetr_variants': mreg.RFDETR_VARIANTS,
                'defaults': {'conf': svc.cfg.get('default_conf', 0.25), 'match_iou': svc.cfg.get('match_iou', 0.5)}}

    @r.get('/api/models/adapter_template.py')
    def adapter_template():
        return PlainTextResponse(schema.ADAPTER_TEMPLATE, media_type='text/x-python',
                                 headers={'Content-Disposition': 'attachment; filename="adapter_template.py"'})

    @r.post('/api/models')
    def save_model(body: dict):
        need()
        try:
            m = owned(reg.upsert, body)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
        svc.forget(m['id'])
        log.info(f'saved model [bold]{vlog.esc(m["name"])}[/] ({m["family"]} · {m["format"]})')
        return public(m)

    @r.delete('/api/models/{mid}')
    def delete_model(mid: str):
        m = model_or_404(mid)
        svc.forget(mid)
        owned(reg.delete, mid)
        svc.remove_upload(m)
        log.info(f'removed model {vlog.esc(m["name"])}')
        return {'ok': True}

    WEIGHT_EXT = {'.pt', '.pth', '.pkl', '.pickle', '.safetensors', '.onnx', '.torchscript', '.yaml', '.yml', '.py'}

    def browse(path):
        res = sources.browse_local(path)
        p = Path(res['path'])
        files = []
        for f in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if f.is_file() and f.suffix.lower() in WEIGHT_EXT and not f.name.startswith('.'):
                files.append({'name': f.name, 'size': f.stat().st_size, 'format': mreg.guess_format(f)})
        for d in res['folders']:
            d['openvino'] = mreg.guess_format(p / d['name']) == 'openvino'
        res['files'] = files
        res['openvino'] = mreg.guess_format(p) == 'openvino'
        return res

    @r.get('/api/models/browse')
    async def browse_files(path: str = ''):
        """Folders + model files (weights, arch yaml, adapter .py) under `path`, for the path picker."""
        need()
        try:
            return await run_io(browse, path or str(PROJECT_ROOT))
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))

    @r.get('/api/models/serving-list')
    async def serving_list(url: str):
        """Models offered by a serving/ app (GET {url}/): routes + class names, for the API form."""
        import httpx

        def get():
            r = httpx.get(url.rstrip('/') + '/', timeout=10, verify=False)
            r.raise_for_status()
            return r.json()
        try:
            data = await run_io(get)
        except Exception as e:  # noqa: BLE001 - shown in the form
            raise HTTPException(400, f'{type(e).__name__}: {e}')
        if not isinstance(data, list):
            raise HTTPException(400, 'not a serving app: GET / did not return a list of models')
        out = []
        for d in data:
            parts = str(d.get('route', '')).strip('/').split('/')
            if len(parts) >= 3:
                out.append({'provider': parts[-3], 'model_type': parts[-2], 'model_name': parts[-1],
                            'versions': d.get('versions') or ['v1'], 'imgsz': d.get('imgsz'), 'backend': d.get('backend'),
                            'classes': len(d.get('classes') or {})})
        return {'models': out}

    @r.post('/api/models/upload')
    async def upload(files: list[UploadFile] = File(...)):
        """Store uploaded weights under <workspace_root>/models/<id>/; returns the path to put in the form.
        OpenVINO: send the .xml and .bin together (the folder is returned)."""
        need()
        folder = reg.upload_dir()
        saved = []
        try:
            for f in files:
                name = Path(f.filename or 'model.bin').name
                dest = folder / name
                with dest.open('wb') as out:
                    while chunk := await f.read(1 << 22):
                        out.write(chunk)
                saved.append(dest)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        weights = [p for p in saved if p.suffix.lower() != '.py']
        adapters = [p for p in saved if p.suffix.lower() == '.py']
        xml = [p for p in saved if p.suffix.lower() == '.xml']
        path = folder if xml else (weights[0] if weights else None)
        log.info(f'uploaded {", ".join(p.name for p in saved)} → {vlog.esc(folder)}')
        return {'path': str(path) if path else None, 'adapter': str(adapters[0]) if adapters else None,
                'format': mreg.guess_format(path) if path else None,
                'bytes': sum(p.stat().st_size for p in saved)}

    @r.post('/api/models/{mid}/test')
    async def test(mid: str):
        m = model_or_404(mid)
        if ctx.state is None:                   # the Sessions home / label page: load it (or reach it) and list its classes
            from ...models import hub
            try:
                info = await run_io(hub.info, reg, m, svc.workdir)
            except Exception as e:  # noqa: BLE001 - shown in the form
                raise HTTPException(400, f'{type(e).__name__}: {e}')
            return {'ok': True, 'no_session': True, 'names': info.get('classes') or [], 'task_type': info.get('task_type'),
                    'detections': [], 'n': 0}
        g.need_index()
        try:
            return await run_io(svc.test, ctx.current(), m)
        except Exception as e:  # noqa: BLE001 - shown in the form
            log.warning(f'test {vlog.esc(m["name"])} failed: {vlog.esc(e)}')
            raise HTTPException(400, f'{type(e).__name__}: {e}')

    @r.post('/api/models/{mid}/predict', dependencies=[Depends(deps.require_bound('labeler'))])
    def run_predict(mid: str):
        m = model_or_404(mid)
        st = ctx.current()
        if st.ready and svc.is_predicted(st, m):
            return {'ready': True}
        job = svc.submit_predict(st, m)
        return {'ready': False, 'job_id': job.id}

    @r.get('/api/models/{mid}/eval', dependencies=viewer)
    async def evaluate(mid: str, conf: float | None = None, iou: float | None = None, split: str = 'val'):
        m = model_or_404(mid)
        g.need_index()
        res = await run_io(svc.evaluate, ctx.current(), m, conf, iou, split)
        if res is None:
            raise HTTPException(409, f'{m["name"]}: run predictions first')
        return json_bytes(orjson.dumps(res, option=orjson.OPT_SERIALIZE_NUMPY))

    @r.get('/api/eval/overview', dependencies=viewer)
    async def overview(split: str = 'val', conf_mode: str = 'best', conf: float = 0.25, iou: float | None = None,
                            focus: str | None = None):
        """Evaluation tab: every model that ran on this dataset compared, one (focus) model explained with the
        dataset, and the findings. conf_mode='best' scores each model at its own best-F1 confidence."""
        need()
        g.need_index()
        res = await run_io(lambda: eval_overview(ctx, split, conf_mode, conf, iou, focus))
        return json_bytes(orjson.dumps(res, option=orjson.OPT_SERIALIZE_NUMPY))

    @r.get('/api/eval/compare', dependencies=viewer)
    async def eval_compare(a: str, b: str, split: str = 'val', conf_mode: str = 'best', conf: float = 0.25,
                           iou: float | None = None):
        """Evaluation tab: model a vs model b, label by label (insights.compare)."""
        need()
        g.need_index()
        ma, mb = model_or_404(a), model_or_404(b)
        res = await run_io(insights.compare, svc, ctx.current(), ma, mb, split, conf_mode, conf, iou)
        if res is None:
            raise HTTPException(409, f'run predictions with both {ma["name"]} and {mb["name"]} first')
        return json_bytes(orjson.dumps(res, option=orjson.OPT_SERIALIZE_NUMPY))

    def _draw(img, boxes_gt, preds, focus=None):
        draw = ImageDraw.Draw(img)
        lw = max(2, img.width // 500)
        pal = state.palette
        for b, c in boxes_gt:
            draw.rectangle(b, outline=pal[c % len(pal)], width=lw)
        for b, score, c, pid in preds:
            if pid == focus:
                draw.rectangle([b[0] - lw * 2, b[1] - lw * 2, b[2] + lw * 2, b[3] + lw * 2], outline='white', width=lw * 2)
        draw_preds(draw, preds, pal, lw)
        return img

    def _jpeg(img, max_w):
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=88)
        return Response(buf.getvalue(), media_type='image/jpeg')

    @r.get('/api/models/{mid}/pred/{pid}', dependencies=viewer)
    def pred_info(mid: str, pid: int):
        m = model_or_404(mid)
        g.need_index()
        try:
            p = svc.pred(ctx.current(), m, pid)
        except KeyError as e:
            raise HTTPException(404, str(e))
        p['cls_name'] = state.classes[p['cls']] if p['cls'] >= 0 else None
        p['labels'] = state.by_image.get(p['image'], [])
        return p

    @r.get('/api/models/{mid}/image-preds', dependencies=viewer)
    def image_preds(mid: str, name: str, conf: float = 0.25):
        """One image's predictions as data (the viewer draws them, dashed, and the editor can accept one as a label):
        {preds: [{box: [x1, y1, x2, y2] px, score, cls (dataset id, -1 = unknown class), pid}]}."""
        m = model_or_404(mid)
        g.need_index()
        rows = svc.image_preds(ctx.current(), m, name, max(0.0, min(float(conf), 1.0)))
        return {'preds': [{'box': [round(v, 2) for v in b], 'score': round(sc, 4), 'cls': c, 'pid': j}
                          for b, sc, c, j in rows]}

    @r.get('/api/models/{mid}/pred/{pid}/image', dependencies=viewer)
    def pred_image(mid: str, pid: int, conf: float = 0.25, max_w: int = 1280):
        """The whole image: labels solid, predictions dashed, this prediction ringed."""
        m = model_or_404(mid)
        g.need_index()
        try:
            p = svc.pred(ctx.current(), m, pid)
        except KeyError as e:
            raise HTTPException(404, str(e))
        img = sources.cached_rgb(state.source, p['image']).copy()       # cached image is shared: _draw mutates
        gt = [(state.samples[i]['bbox'], state.samples[i]['cls']) for i in state.by_image.get(p['image'], [])]
        preds = svc.image_preds(ctx.current(), m, p['image'], min(conf, p['score']))
        return _jpeg(_draw(img, gt, preds, focus=pid), max_w)

    @r.get('/api/models/{mid}/pred/{pid}/crop', dependencies=viewer)
    def pred_crop(mid: str, pid: int, size: int = 224):
        """Square crop around one prediction (same framing as the label crops)."""
        m = model_or_404(mid)
        g.need_index()
        try:
            p = svc.pred(ctx.current(), m, pid)
        except KeyError as e:
            raise HTTPException(404, str(e))
        from ...data.indexer import crop_outside_transparent, square_crop_box
        img = sources.cached_rgb(state.source, p['image'])             # shared, read-only: crop() copies
        x1, y1, x2, y2 = p['box']
        pad = float(state.cfg['index']['crop_pad'])
        crop = img.crop(square_crop_box(x1, y1, x2, y2, pad))
        crop = crop.resize((size, size), Image.BICUBIC)
        cut = crop_outside_transparent(crop, (x1, y1, x2, y2), img.width, img.height, pad)
        buf = io.BytesIO()
        if cut is None:
            crop.save(buf, 'JPEG', quality=88)
            return Response(buf.getvalue(), media_type='image/jpeg', headers={'Cache-Control': 'max-age=3600'})
        cut.save(buf, 'WEBP', quality=88)
        return Response(buf.getvalue(), media_type='image/webp', headers={'Cache-Control': 'max-age=3600'})

    return r
