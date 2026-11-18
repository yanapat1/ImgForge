"""The Bucket page's status bar shows only the jobs whose key its filter knows (static/ts/bucket/activity.ts MINE).
After the Data Center -> Bucket rename the backend's keys became bk-<kind>: while the filter still asked for dc-, so
no bucket job (label with a model, video -> frames, import, export…) showed. Every kind the backend submits must match."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _mine():
    ts = (ROOT / 'static/ts/bucket/activity.ts').read_text()
    m = re.search(r'^const MINE = /(.+)/;$', ts, re.MULTILINE)
    assert m, 'MINE not found in activity.ts'
    return re.compile(m.group(1).replace('\\/', '/'))


def test_every_bucket_job_key_shows_in_the_status_bar():
    py = (ROOT / 'dataviz/bucket/service.py').read_text()
    kinds = sorted(set(re.findall(r"jobs\.submit\(\s*f?'(bk-[a-z-]+):", py))       # inline keys
                   | set(re.findall(r"\bkey = f?'(bk-[a-z-]+):", py)))                   # key = f'bk-…' then submit(key, …)
    assert {'bk-autolabel', 'bk-frames', 'bk-import', 'bk-export', 'bk-fetch', 'bk-compare'} <= set(kinds), kinds
    mine = _mine()
    missing = [k for k in kinds if not mine.match(f'{k}:bid123:path')]
    assert not missing, f'activity.ts MINE does not show {missing}'


def test_labeling_jobs_and_others():
    mine = _mine()
    assert mine.match('label-predict:abc:m1') and mine.match('label-import:abc')
    assert not mine.match('features:abc') and not mine.match('dc-autolabel:x')
