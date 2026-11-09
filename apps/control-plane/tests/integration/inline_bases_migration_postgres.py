"""Exercise the inline registry migration against a disposable PostgreSQL DB."""

import importlib.util
import os
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    url = make_url(os.environ["COLLABORATION_TEST_DATABASE_URL"])
    if url.host != "127.0.0.1" or url.database != "collaboration_test":
        raise RuntimeError("Only a disposable loopback collaboration_test database is allowed")
    path = (
        Path(__file__).resolve().parents[2]
        / "app/db/migrations/versions/202611090001_inline_base_sources.py"
    )
    spec = importlib.util.spec_from_file_location("inline_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(url)
    with engine.begin() as connection:
        if connection.scalar(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        ):
            raise RuntimeError("Expected an empty disposable database")
        # The predecessor's actual PostgreSQL unique constraint is unnamed in
        # SQLAlchemy, so PostgreSQL supplies this table/column-derived name.
        connection.execute(
            text(
                "CREATE TABLE collaboration_bases "
                "(id text PRIMARY KEY, entry_id text NOT NULL UNIQUE)"
            )
        )
        connection.execute(text("INSERT INTO collaboration_bases VALUES ('saved', 'entry-one')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert (
                connection.scalar(
                    text("SELECT source_key FROM collaboration_bases WHERE id='saved'")
                )
                == "file"
            )
            connection.execute(
                text(
                    "INSERT INTO collaboration_bases (id, entry_id, source_key) "
                    "VALUES ('inline-one','note',:one), ('inline-two','note',:two)"
                ),
                {"one": "a" * 64, "two": "b" * 64},
            )
            try:
                migration.downgrade()
            except RuntimeError as error:
                assert "inline definition" in str(error)
            else:
                raise AssertionError("Downgrade discarded inline identities")
            connection.execute(text("DELETE FROM collaboration_bases WHERE source_key <> 'file'"))
            migration.downgrade()
            assert connection.scalar(text("SELECT count(*) FROM collaboration_bases")) == 1
    engine.dispose()
    print(
        "Inline Bases migration upgrade, preserved standalone identity, "
        "multiple sources, and downgrade guard passed"
    )


if __name__ == "__main__":
    main()
