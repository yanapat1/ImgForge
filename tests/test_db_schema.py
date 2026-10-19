"""The migrations and db/schema.py describe the same database (a column added to one only fails here)."""

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext


def test_migrations_match_schema(pg):
    from dataviz import db
    from dataviz.db import migrate, schema
    with db.connect() as c:
        assert migrate.current(c) == migrate.head()
        ctx = MigrationContext.configure(c, opts={'include_schemas': True, 'compare_type': True,
                                                  'include_name': lambda name, type_, parent: (
                                                      type_ != 'schema' or name in schema.SCHEMAS)})
        diff = [d for d in compare_metadata(ctx, schema.metadata)
                if not (isinstance(d, tuple) and d[0] == 'remove_table' and d[1].schema not in schema.SCHEMAS)
                and not _is_vec_set_table(d)]
    assert diff == []


def _is_vec_set_table(d):
    """vec.emb_<id> tables are created at run time (ml/vectors.py), not by the migrations."""
    return isinstance(d, tuple) and d[0] == 'remove_table' and d[1].schema == 'vec' and d[1].name.startswith('emb_')


def test_secrets_round_trip(pg):
    from dataviz.db import crypto
    blob = crypto.encrypt_json({'secret_key': 's3cr3t'})
    assert b's3cr3t' not in blob
    assert crypto.decrypt_json(blob) == {'secret_key': 's3cr3t'}
    assert crypto.encrypt_json({}) is None and crypto.decrypt_json(None) == {}
