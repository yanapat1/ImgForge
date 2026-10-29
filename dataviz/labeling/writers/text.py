"""
Text writers (media_type 'text'): JSONL, CSV and CoNLL (BIO tags for text_span). The document text is read from
the media file (ctx.image_file), capped at MAX_TEXT bytes. Export-only: there is no text model labeler yet.
"""

import csv
import re

import orjson

from .base import Writer

MAX_TEXT = 10 << 20
TEXT_TASKS = ('text_span', 'text_class')
_TOKEN = re.compile(r'\S+')


def read_text(ctx, m):
    fn = ctx.get('image_file')
    p = fn(m) if fn else None
    if p is None:
        return None
    try:
        with open(p, 'rb') as f:
            return f.read(MAX_TEXT).decode('utf-8', 'replace')
    except OSError:
        return None


class _TextWriter(Writer):
    media = ('text',)
    tasks = TEXT_TASKS

    def text(self, m):
        t = read_text(self.ctx, m)
        if t is None:
            self.warn('text_missing')
        return t

    def spans(self, rows):
        return sorted(((int(r['char_start']), int(r['char_end']), self.cname(r), r) for r in self.own(rows, ('text_span',))
                       if r.get('char_start') is not None and r.get('char_end') is not None), key=lambda s: s[:2])

    def labels(self, rows):
        return sorted({self.cname(r) for r in self.own(rows, ('tag',))})

    def _open(self, name, header=None):
        self.out_name = name
        self.fh = open(self.tmp(name), 'w', newline='', encoding='utf-8')
        if header:
            self.cw = csv.writer(self.fh)
            self.cw.writerow(header)

    def finish(self):
        self.fh.close()
        self.copy(self.out_name, self.fh.name)
        return super().finish()

    def abort(self):
        if getattr(self, 'fh', None):
            self.fh.close()
        super().abort()


class TextJsonl(_TextWriter):
    key = 'text_jsonl'
    label = 'JSONL'
    layout = export_layout = '└── text.jsonl          {file, text, labels, spans: [[start, end, label]]}'
    note = 'one JSON line per document'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self._open('text.jsonl')

    def add(self, m, rows):
        t = self.text(m)
        d = {'file': self.ctx['image_rel'](m), 'text': t}
        if self.task['task_type'] == 'text_class':
            d['labels'] = self.labels(rows)
        else:
            d['spans'] = [[s, e, lab] for s, e, lab, _ in self.spans(rows)]
        self.fh.write(orjson.dumps(d).decode() + '\n')
        self.n_media += 1


class TextCsv(_TextWriter):
    key = 'text_csv'
    label = 'CSV'
    layout = export_layout = '└── text.csv            file, label (+ start, end, text for spans)'
    note = 'one row per label or span'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self.spans_mode = task['task_type'] == 'text_span'
        self._open('text.csv', ['file', 'start', 'end', 'label', 'text'] if self.spans_mode else ['file', 'label'])

    def add(self, m, rows):
        f = self.ctx['image_rel'](m)
        if self.spans_mode:
            t = self.text(m) or ''
            for s, e, lab, r in self.spans(rows):
                self.cw.writerow([f, s, e, lab, r.get('value_text') or t[s:e]])
        else:
            for lab in self.labels(rows) or ['']:
                self.cw.writerow([f, lab])
        self.n_media += 1


def bio(text, spans):
    """[(token, tag)]: whitespace tokens, tagged B-/I-<label> when they overlap a span, else O."""
    out = []
    for mt in _TOKEN.finditer(text):
        a, b = mt.span()
        tag = 'O'
        for s, e, lab in spans:
            if a < e and b > s:
                tag = ('B-' if a <= s or not out or out[-1][1][2:] != lab else 'I-') + lab
                break
        out.append((mt.group(), tag))
    return out


class Conll(_TextWriter):
    key = 'conll'
    label = 'CoNLL (BIO)'
    tasks = ('text_span',)
    layout = export_layout = '└── conll.txt           token<TAB>B-/I-/O, a blank line between documents'
    note = 'token-level BIO tags for named entities'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self._open('conll.txt')

    def add(self, m, rows):
        t = self.text(m)
        if t is None:
            return
        self.fh.write(f'# file = {self.ctx["image_rel"](m)}\n')
        for tok, tag in bio(t, [(s, e, lab) for s, e, lab, _ in self.spans(rows)]):
            self.fh.write(f'{tok}\t{tag}\n')
        self.fh.write('\n')
        self.n_media += 1


WRITERS = [TextJsonl, TextCsv, Conll]
