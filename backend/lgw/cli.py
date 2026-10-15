"""Flask CLI commands for the gateway database."""

import click
from flask import current_app

from lgw.keys import generate_key, hash_key, key_hint, validate_user_sub
from lgw.models import ApiKey, Base, KeyStatus, User, utcnow


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
