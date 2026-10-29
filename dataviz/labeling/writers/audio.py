"""
Audio writers (media_type 'audio'): a JSONL manifest (NeMo / Hugging Face audiofolder metadata.jsonl), a Common
Voice style TSV, and a segments CSV. Export-only: there is no audio model labeler yet.
"""

import csv

import orjson

from .base import Writer

AUDIO_TASKS = ('audio_segment', 'transcription')


class _AudioWriter(Writer):
    media = ('audio',)
    tasks = AUDIO_TASKS

    def segments(self, rows):
        out = []
        for r in rows:
            if r.get('_parent_only') or r['geometry_type'] != 'time_segment' or r.get('t_start_ms') is None:
                continue
            lab = self.names[self.index[r['class_id']]] if r.get('class_id') in self.index else ''
            out.append((r['t_start_ms'] / 1000, (r.get('t_end_ms') or r['t_start_ms']) / 1000, lab, r.get('value_text') or ''))
        return sorted(out)

    def transcript(self, rows):
        t = [r.get('value_text') or '' for r in rows if r['geometry_type'] == 'transcription' and not r.get('_parent_only')]
        if t:
            return ' '.join(x.strip() for x in t if x.strip())
        return ' '.join(s[3].strip() for s in self.segments(rows) if s[3].strip())

    def _open(self, name, header=None, delimiter=','):
        self.out_name = name
        self.fh = open(self.tmp(name), 'w', newline='', encoding='utf-8')
        if header:
            self.cw = csv.writer(self.fh, delimiter=delimiter)
            self.cw.writerow(header)

    def finish(self):
        self.fh.close()
        self.copy(self.out_name, self.fh.name)
        return super().finish()

    def abort(self):
        if getattr(self, 'fh', None):
            self.fh.close()
        super().abort()


class AudioJsonl(_AudioWriter):
    key = 'audio_jsonl'
    label = 'JSONL manifest'
    layout = export_layout = '└── metadata.jsonl      {file_name, audio_filepath, duration, text, segments}'
    note = 'NeMo manifest / Hugging Face audiofolder metadata'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self._open('metadata.jsonl')

    def add(self, m, rows):
        f = self.ctx['image_rel'](m)
        d = {'file_name': f, 'audio_filepath': f, 'duration': round((m.get('duration_ms') or 0) / 1000, 3),
             'text': self.transcript(rows)}
        segs = self.segments(rows)
        if segs:
            d['segments'] = [{'start': s, 'end': e, 'label': lab, 'text': t} for s, e, lab, t in segs]
        self.fh.write(orjson.dumps(d).decode() + '\n')
        self.n_media += 1


class CommonVoice(_AudioWriter):
    key = 'commonvoice'
    label = 'Common Voice TSV'
    tasks = ('transcription', 'audio_segment')
    layout = export_layout = '└── commonvoice.tsv     path<TAB>sentence'
    note = 'one row per clip with its transcript'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self._open('commonvoice.tsv', ['path', 'sentence'], '\t')

    def add(self, m, rows):
        self.cw.writerow([self.ctx['image_rel'](m), self.transcript(rows)])
        self.n_media += 1


class SegmentsCsv(_AudioWriter):
    key = 'segments_csv'
    label = 'Segments CSV'
    tasks = ('audio_segment',)
    layout = export_layout = '└── segments.csv        file, start, end, label, text (seconds)'
    note = 'one row per time segment'

    def begin(self, sink, task, names, ctx):
        super().begin(sink, task, names, ctx)
        self._open('segments.csv', ['file', 'start', 'end', 'label', 'text'])

    def add(self, m, rows):
        f = self.ctx['image_rel'](m)
        for s, e, lab, t in self.segments(rows):
            self.cw.writerow([f, s, e, lab, t])
        self.n_media += 1


WRITERS = [AudioJsonl, CommonVoice, SegmentsCsv]
