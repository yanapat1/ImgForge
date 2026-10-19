"""Alembic environment: always run online on the connection db/migrate.py hands over."""

from alembic import context

connection = context.config.attributes['connection']
context.configure(connection=connection, version_table_schema='public', transaction_per_migration=True)
with context.begin_transaction():
    context.run_migrations()
