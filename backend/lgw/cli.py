"""Flask CLI commands for the gateway database."""

from datetime import timedelta

import click
from flask import current_app
from sqlalchemy import select, update

from lgw.keys import generate_key, hash_key, key_hint, validate_user_sub
from lgw.models import (
    ApiKey,
    Base,
    KeyStatus,
    Request,
    RequestStatus,
    UsageSource,
    User,
    ensure_utc,
    utcnow,
)


def _mark_stale(session, stale_ids, now):
    """Mark rows that are still in progress.

    The status check is part of the UPDATE, so a request that finished
    after it was selected is left unchanged.

    Parameters
    ----------
    session : sqlalchemy.orm.Session
        Open session.
    stale_ids : list of int
        Request ids whose start time is past the cutoff.
    now : datetime.datetime
        Time stored in ``ended_at``.

    Returns
    -------
    int
        Number of rows updated.
    """
    if not stale_ids:
        return 0
    result = session.execute(
        update(Request)
        .where(
            Request.id.in_(stale_ids),
            Request.status == RequestStatus.IN_PROGRESS,
        )
        .values(
            status=RequestStatus.GATEWAY_ERROR,
            usage_source=UsageSource.UNKNOWN,
            input_tokens=0,
            output_tokens=0,
            cache_hit_tokens=0,
            ended_at=now,
        )
    )
    session.commit()
    return result.rowcount


def register_cli(app):
    """Register database commands on ``app``.

    Parameters
    ----------
    app : flask.Flask
        Proxy application.
    """

    @app.cli.command("init-db")
    def init_db():
        """Create database tables."""
        engine = current_app.extensions["lgw_engine"]
        Base.metadata.create_all(engine)
        click.echo("initialized database")

    @app.cli.command("create-key")
    @click.option("--user-sub", required=True)
    @click.option("--username", required=True)
    @click.option("--name", "key_name", required=True)
    def create_key(user_sub, username, key_name):
        """Create a user when missing and print a new API key once."""
        message = validate_user_sub(user_sub)
        if message is not None:
            raise click.ClickException(message)
        raw_key = generate_key()
        sessions = current_app.extensions["lgw_sessions"]
        with sessions() as session:
            user = session.get(User, user_sub)
            if user is None:
                session.add(User(sub=user_sub, username=username, email=None))
                session.flush()
            session.add(
                ApiKey(
                    user_sub=user_sub,
                    name=key_name,
                    key_hash=hash_key(raw_key),
                    key_hint=key_hint(raw_key),
                    created_at=utcnow(),
                    last_used_at=None,
                    status=KeyStatus.ACTIVE,
                )
            )
            session.commit()
        click.echo(raw_key)

    @app.cli.command("sweep")
    def sweep():
        """Mark stale in-progress requests as gateway errors."""
        settings = current_app.extensions["lgw_proxy"]
        cutoff = utcnow() - timedelta(seconds=settings.max_request_seconds)
        sessions = current_app.extensions["lgw_sessions"]
        with sessions() as session:
            statement = select(Request).where(
                Request.status == RequestStatus.IN_PROGRESS
            )
            stale_ids = [
                row.id
                for row in session.scalars(statement)
                if ensure_utc(row.started_at) < cutoff
            ]
            swept = _mark_stale(session, stale_ids, utcnow())
        click.echo(f"swept {swept} requests")
