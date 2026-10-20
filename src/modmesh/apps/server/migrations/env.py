from alembic import context
from sqlalchemy import Connection

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise RuntimeError("Use modmesh-server --migrate to run migrations")
context.configure(connection=connection, target_metadata=None, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
