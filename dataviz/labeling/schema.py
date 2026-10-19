"""
The global annotation schema: one set of tables for every modality (boxes, polygons, masks, keypoints /
landmarks, image tags, 3D cuboids, audio / voice segments, transcriptions, text spans, relations).

The same definition is used three ways:
    SQLite      the per-project local store (labeling/<pid>/annotations.db, see store.py)
    PostgreSQL  export target (JSON columns become JSONB)
    MSSQL       export target (JSON columns become NVARCHAR(MAX) + ISJSON check)
so a database export is a row copy, never a translation.

Coordinates in `geometry` are normalised to 0..1 of the (EXIF-oriented) image width / height. The shape of
`geometry` per geometry_type is GEOMETRY below; `bbox_*` always hold the normalised extent of any 2D shape
so common queries never parse JSON.

Only first-level children (tasks, classes, media) cascade from their parent: MSSQL rejects "multiple cascade
paths" (annotations -> tasks and -> media both lead to projects), so annotations / statuses are deleted by
the store explicitly.
"""

import orjson
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer,
                        MetaData, String, Table, Text, Unicode, UnicodeText)
from sqlalchemy.dialects import mssql, postgresql
from sqlalchemy.types import TypeDecorator

SCHEMA_VERSION = 1

# task types the page can draw today, and the ones only the schema knows (future editors)
IMAGE_TASK_TYPES = ('object_detection', 'segmentation', 'keypoints', 'classification')
TASK_TYPES = IMAGE_TASK_TYPES + ('polyline', 'point', 'cuboid_3d', 'pointcloud_seg', 'audio_segment', 'transcription',
                                 'text_span', 'text_class', 'relation')

# task_type -> geometry_type its annotations use
TASK_GEOMETRY = {
    'object_detection': 'bbox', 'segmentation': 'polygon', 'keypoints': 'keypoints', 'classification': 'tag',
    'polyline': 'polyline', 'point': 'point', 'cuboid_3d': 'cuboid_3d', 'pointcloud_seg': 'mask_rle',
    'audio_segment': 'time_segment', 'transcription': 'transcription', 'text_span': 'text_span', 'text_class': 'tag',
    'relation': 'tag',
}

# geometry_type -> documented JSON shape (validators in store.py follow this)
GEOMETRY = {
    'bbox': '{"x": left, "y": top, "w": width, "h": height}',
    'polygon': '{"points": [[x, y], ...] (>= 3), "holes": [[[x, y], ...], ...] (optional)}',
    'mask_rle': '{"size": [h, w], "counts": "<COCO RLE>"}',
    'keypoints': '{"points": [[x, y, v], ...]}  v: 0 not labeled, 1 occluded, 2 visible; names in task.config.keypoints',
    'polyline': '{"points": [[x, y], ...] (>= 2)}',
    'point': '{"x": x, "y": y}',
    'tag': '{}  (the value is class_id)',
    'cuboid_3d': '{"center": [x, y, z], "size": [l, w, h], "rotation": [rx, ry, rz]}',
    'time_segment': '{}  + t_start_ms / t_end_ms (+ value_text)',
    'transcription': '{}  + value_text',
    'text_span': '{}  + char_start / char_end',
}

MEDIA_TYPES = ('image', 'video', 'audio', 'text', 'pointcloud', 'document')
STATUSES = ('todo', 'in_progress', 'done', 'skipped')
REVIEWS = ('manual', 'bulk')      # the optional review of a done image (local-only column, not exported)
SOURCES = ('manual', 'model', 'import')


# NVARCHAR(MAX) on MSSQL (UnicodeText would be the deprecated NTEXT)
LongText = UnicodeText().with_variant(mssql.NVARCHAR(None), 'mssql')


class JSONDoc(TypeDecorator):
    """JSON stored as text (SQLite / MSSQL) or JSONB (PostgreSQL); always (de)serialised with orjson."""
    impl = UnicodeText
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(postgresql.JSONB())
        if dialect.name == 'mssql':
            return dialect.type_descriptor(mssql.NVARCHAR(None))      # NVARCHAR(MAX)
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == 'postgresql':
            return value                                               # psycopg adapts dicts to JSONB itself
        return value if isinstance(value, str) else orjson.dumps(value).decode()

    def process_result_value(self, value, dialect):
        if value is None or not isinstance(value, (str, bytes)):
            return value
        return orjson.loads(value)


def _ts(name, **kw):
    return Column(name, DateTime(timezone=True), **kw)


def _json_check(table, col, dialect_only='mssql'):
    return CheckConstraint(f'ISJSON({col}) = 1', name=f'ck_{table}_{col}_json').ddl_if(dialect=dialect_only)


def make_metadata(prefix='', schema=None):
    """All tables, named <prefix><table> (remote DBs default to prefix 'lbl_'), optionally in a DB schema."""
    md = MetaData(schema=schema)
    p = prefix
    fk = (lambda t: f'{schema}.{p}{t}.id') if schema else (lambda t: f'{p}{t}.id')

    Table(f'{p}projects', md,
          Column('id', String(36), primary_key=True),
          Column('name', Unicode(200), nullable=False),
          Column('description', LongText),
          _ts('created_at'), _ts('updated_at'),
          Column('meta', JSONDoc),
          _json_check(f'{p}projects', 'meta'))

    Table(f'{p}tasks', md,
          Column('id', String(36), primary_key=True),
          Column('project_id', String(36), ForeignKey(fk('projects'), ondelete='CASCADE'), nullable=False),
          Column('name', Unicode(200), nullable=False),
          Column('task_type', String(40), nullable=False),
          Column('position', Integer, nullable=False, default=0),
          Column('config', JSONDoc),            # keypoint names + skeleton + flip_idx, multi_label, attributes ...
          _ts('created_at'),
          Column('is_deleted', Boolean, nullable=False, default=False),
          _json_check(f'{p}tasks', 'config'),
          Index(f'ix_{p}tasks_project', 'project_id', 'position'))

    Table(f'{p}label_classes', md,
          Column('id', String(36), primary_key=True),
          Column('task_id', String(36), ForeignKey(fk('tasks'), ondelete='CASCADE'), nullable=False),
          Column('name', Unicode(200), nullable=False),
          Column('color', String(7), nullable=False),
          Column('position', Integer, nullable=False, default=0),
          Column('hotkey', String(20)),
          Column('parent_id', String(36)),
          Column('attributes_schema', JSONDoc),
          Column('is_deleted', Boolean, nullable=False, default=False),
          _json_check(f'{p}label_classes', 'attributes_schema'),
          Index(f'ix_{p}classes_task', 'task_id', 'position'))

    Table(f'{p}media', md,
          Column('id', String(36), primary_key=True),
          Column('project_id', String(36), ForeignKey(fk('projects'), ondelete='CASCADE'), nullable=False),
          Column('media_type', String(20), nullable=False, default='image'),
          Column('uri', Unicode(1000)),
          Column('file_name', Unicode(400), nullable=False),
          Column('mime', String(80)),
          Column('checksum', String(40)),
          Column('size_bytes', BigInteger),
          Column('width', Integer), Column('height', Integer), Column('depth', Integer),
          Column('duration_ms', BigInteger), Column('frame_count', Integer),
          Column('position', Integer, nullable=False, default=0),
          Column('meta', JSONDoc),
          _ts('created_at'),
          _json_check(f'{p}media', 'meta'),
          Index(f'ix_{p}media_pos', 'project_id', 'position'),
          Index(f'ux_{p}media_name', 'project_id', 'file_name', unique=True))

    Table(f'{p}annotations', md,
          Column('id', String(36), primary_key=True),
          Column('project_id', String(36), nullable=False),
          Column('task_id', String(36), ForeignKey(fk('tasks')), nullable=False),
          Column('media_id', String(36), ForeignKey(fk('media')), nullable=False),
          Column('class_id', String(36)),
          Column('geometry_type', String(30), nullable=False),
          Column('geometry', JSONDoc),
          Column('bbox_x', Float), Column('bbox_y', Float), Column('bbox_w', Float), Column('bbox_h', Float),
          Column('frame_index', Integer),
          Column('t_start_ms', BigInteger), Column('t_end_ms', BigInteger),
          Column('char_start', Integer), Column('char_end', Integer),
          Column('value_text', LongText),
          Column('attributes', JSONDoc),
          Column('score', Float),
          Column('source', String(20), nullable=False, default='manual'),
          Column('parent_id', String(36)),
          Column('z_order', Integer, nullable=False, default=0),
          Column('created_by', Unicode(100)),
          _ts('created_at'), _ts('updated_at'),
          Column('version', Integer, nullable=False, default=1),
          Column('is_deleted', Boolean, nullable=False, default=False),
          _json_check(f'{p}annotations', 'geometry'),
          _json_check(f'{p}annotations', 'attributes'),
          Index(f'ix_{p}ann_task_media', 'task_id', 'media_id'),
          Index(f'ix_{p}ann_media', 'media_id'))

    Table(f'{p}annotation_relations', md,
          Column('id', String(36), primary_key=True),
          Column('project_id', String(36), nullable=False),
          Column('from_id', String(36), nullable=False),
          Column('to_id', String(36), nullable=False),
          Column('relation_type', Unicode(100), nullable=False),
          Column('attributes', JSONDoc),
          Column('is_deleted', Boolean, nullable=False, default=False),
          _json_check(f'{p}annotation_relations', 'attributes'))

    Table(f'{p}media_task_status', md,
          Column('media_id', String(36), ForeignKey(fk('media')), primary_key=True),
          Column('task_id', String(36), ForeignKey(fk('tasks')), primary_key=True),
          Column('status', String(12), nullable=False, default='todo'),
          Column('n_annotations', Integer, nullable=False, default=0),
          Column('version', Integer, nullable=False, default=0),     # bumps on every save of (media, task)
          Column('committed_by', Unicode(100)),
          _ts('committed_at'), _ts('updated_at'),
          Index(f'ix_{p}mts_task_status', 'task_id', 'status'))

    Table(f'{p}export_runs', md,
          Column('id', String(36), primary_key=True),
          Column('project_id', String(36), nullable=False),
          Column('task_id', String(36)),
          Column('target', String(20), nullable=False),
          Column('target_uri', Unicode(1000)),
          Column('options', JSONDoc),
          _ts('started_at'), _ts('finished_at'),
          Column('n_media', Integer), Column('n_annotations', Integer),
          Column('status', String(12)),
          Column('error', LongText),
          _json_check(f'{p}export_runs', 'options'))

    Table(f'{p}schema_info', md,
          Column('key', String(40), primary_key=True),
          Column('value', Unicode(200)))
    return md


def tables(md, prefix=''):
    """{short name: Table} for a metadata built by make_metadata(prefix)."""
    n = len(prefix)
    return {t.name[n:]: t for t in md.sorted_tables}
