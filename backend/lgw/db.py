"""Database engine and short-lived sessions."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

BUSY_TIMEOUT_MS = 5000


def create_engine_from_url(url):
    """Create an engine for ``url``.

    SQLite connections enable WAL mode and a busy timeout so several
    gunicorn workers can write at the same time.

    Parameters
    ----------
    url : str
        SQLAlchemy database URL.

    Returns
    -------
    sqlalchemy.engine.Engine
        Engine that opens a connection only when it is used.
    """
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def create_session_factory(engine):
    """Return a session factory bound to ``engine``.

    Each call of the factory opens a new session. Callers should use
    ``with factory() as session`` and must not share that session
    across coroutines.

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        Engine returned by :func:`create_engine_from_url`.

    Returns
    -------
    sqlalchemy.orm.sessionmaker
        Session factory.
    """
    return sessionmaker(bind=engine)


def _set_sqlite_pragmas(dbapi_connection, connection_record):
    del connection_record
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
