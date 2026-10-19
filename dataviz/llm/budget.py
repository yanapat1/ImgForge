"""
Token estimates and the per-request budget: `fit()` trims a request (old turns, old images, then the context
images) until its estimate is under min(the Settings budget, 100k), or refuses it. Estimates, not tokenizers: text
is ~3.5 characters per token, images follow each provider's published formula, so the page can show "≈ N in".
"""

import io
import math

from .providers import LLMError
from .store import MAX_BUDGET

CHARS_PER_TOKEN = 3.5
AUDIO_TOKENS_PER_S = 32                 # gemini's published rate; openai's audio is in the same range
OMITTED = '[image omitted]'
MIN_SIDE = 128                          # context images are never downscaled below this: dropped instead


def text_tokens(s):
    return math.ceil(len(s or '') / CHARS_PER_TOKEN)


def image_size(jpeg):
    from PIL import Image
    try:
        return Image.open(io.BytesIO(jpeg)).size              # header only
    except Exception:  # noqa: BLE001 - an unreadable image is estimated as a big one
        return 1024, 1024


def image_tokens(kind, w, h):
    if kind == 'anthropic':
        s = min(1.0, 1568 / max(w, h))                         # claude downsizes to 1568 px long side
        return math.ceil(w * s * h * s / 750)
    if kind == 'gemini':
        if w <= 384 and h <= 384:
            return 258
        return 258 * math.ceil(w / 768) * math.ceil(h / 768)
    # openai: fit 2048 x 2048, then the short side to 768, 85 + 170 per 512 px tile
    s = min(1.0, 2048 / max(w, h))
    w, h = w * s, h * s
    s = min(1.0, 768 / min(w, h))
    w, h = w * s, h * s
    return 85 + 170 * math.ceil(w / 512) * math.ceil(h / 512)


def part_tokens(kind, part):
    if 'text' in part:
        return text_tokens(part['text'])
    if 'jpeg' in part:
        return image_tokens(kind, *image_size(part['jpeg']))
    if 'wav' in part:
        return math.ceil(max(0, len(part['wav']) - 44) / 32000 * AUDIO_TOKENS_PER_S) + 1     # 16 kHz 16-bit mono
    return 0


def msgs_tokens(kind, msgs):
    return sum(4 + sum(part_tokens(kind, x) for x in m.get('parts') or []) for m in msgs)


def estimate(kind, system, msgs):
    return text_tokens(system) + msgs_tokens(kind, msgs)


def cap(budget):
    try:
        b = int(budget)
    except (TypeError, ValueError):
        b = MAX_BUDGET
    return max(1000, min(b, MAX_BUDGET))


def _strip_media(m):
    """An older turn: images become '[image omitted]', voice clips '[voice omitted]'."""
    parts = []
    for x in m.get('parts') or []:
        if 'jpeg' in x:
            parts.append({'text': OMITTED})
        elif 'wav' in x:
            parts.append({'text': '[voice omitted]'})
        else:
            parts.append(x)
    return {'role': m['role'], 'parts': parts}


def _downscale(jpeg):
    """Half the size, or None when that would go below MIN_SIDE."""
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg))
    w, h = img.size
    if min(w, h) // 2 < MIN_SIDE:
        return None
    img = img.convert('RGB').resize((w // 2, h // 2), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def fit(kind, system, context, history, budget):
    """(msgs, estimate) for a request under the token cap.
    context: parts (summaries, mosaics) put in front of the latest user turn; history: the conversation, the
    latest user turn last. Order of cuts: media of older turns -> oldest turns -> context images downscaled, then
    dropped. LLMError(400) if the system prompt + context text + the latest turn alone are still too big."""
    limit = cap(budget)
    if not history:
        raise LLMError('nothing to send')
    older = [_strip_media(m) for m in history[:-1]]
    last = history[-1]
    ctx = [dict(x) for x in context or []]

    def build():
        return older + [{'role': last['role'], 'parts': ctx + list(last.get('parts') or [])}]

    est = estimate(kind, system, build())
    while est > limit and older:
        older.pop(0)
        while older and older[0]['role'] != 'user':           # a conversation starts with a user turn
            older.pop(0)
        est = estimate(kind, system, build())
    while est > limit:
        imgs = [i for i, x in enumerate(ctx) if 'jpeg' in x]
        if not imgs:
            break
        i = max(imgs, key=lambda k: part_tokens(kind, ctx[k]))   # the most expensive one first
        small = _downscale(ctx[i]['jpeg'])
        ctx[i] = {**ctx[i], 'jpeg': small} if small else {'text': OMITTED + ' (over the token budget)'}
        est = estimate(kind, system, build())
    if est > limit:
        raise LLMError('request too large for the token budget', 400)
    return build(), est
