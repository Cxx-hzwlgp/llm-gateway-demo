"""Authenticate a gateway API key on each request."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from flask import current_app, request
from sqlalchemy import select

from lgw.keys import hash_key, is_key_format
from lgw.models import ApiKey, KeyStatus, ensure_utc, utcnow
from lgw.proxy.errors import invalid_api_key

logger = logging.getLogger(__name__)

_TOUCH_AFTER = timedelta(minutes=1)


@dataclass(frozen=True)
class AuthenticatedKey:
    """Identity of a key that is allowed to forward requests."""

    id: int
    user_sub: str


def authenticate():
    """Resolve the bearer token to an active gateway key.

    The lookup is not cached. Authentication failures all produce the
    same 401 response and do not write a request record.

    Returns
    -------
    AuthenticatedKey or tuple
        The authenticated key, or a Flask 401 response tuple.
    """
    token = _bearer_token()
    if token is None or not is_key_format(token):
        return invalid_api_key()
    sessions = current_app.extensions["lgw_sessions"]
    with sessions() as session:
        statement = select(ApiKey).where(ApiKey.key_hash == hash_key(token))
        api_key = session.scalar(statement)
        if api_key is None or api_key.status != KeyStatus.ACTIVE:
            return invalid_api_key()
        identified = AuthenticatedKey(id=api_key.id, user_sub=api_key.user_sub)
        if _due_for_touch(api_key):
            api_key.last_used_at = utcnow()
            _commit_last_used(session, identified.id)
        return identified


def _commit_last_used(session, key_id):
    try:
        session.commit()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("failed to update last_used_at for key %s", key_id)
        session.rollback()


def _bearer_token():
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    token = header[len(prefix) :].strip()
    if not token:
        return None
    return token


def _due_for_touch(api_key):
    last_used_at = api_key.last_used_at
    if last_used_at is None:
        return True
    elapsed = utcnow() - ensure_utc(last_used_at)
    return elapsed > _TOUCH_AFTER
