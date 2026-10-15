"""Insert a request record before the upstream call and finish it after."""

import logging
from dataclasses import dataclass

from lgw.models import Request, RequestStatus, UsageSource, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """Final status and token counts for one request record."""

    status: str
    usage_source: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int


def start_request(sessions, *, key_id, provider, model):
    """Insert an in-progress request and return its id.

    The row is committed before the caller contacts the upstream, so a
    process crash still leaves a record that sweep can find.

    Parameters
    ----------
    sessions : sqlalchemy.orm.sessionmaker
        Short-lived session factory.
    key_id : int
        Authenticated gateway key id.
    provider : str
        Configured provider name.
    model : str
        Model from the client request.

    Returns
    -------
    int
        New request id.
    """
    with sessions() as session:
        row = Request(
            key_id=key_id,
            provider=provider,
            model=model,
            started_at=utcnow(),
            ended_at=None,
            status=RequestStatus.IN_PROGRESS,
            usage_source=UsageSource.UNKNOWN,
            input_tokens=0,
            output_tokens=0,
            cache_hit_tokens=0,
        )
        session.add(row)
        session.flush()
        record_id = row.id
        session.commit()
        return record_id


def finish_request(sessions, record_id, outcome):
    """Store the final status and token counts.

    A database failure is logged and swallowed. The record stays
    in progress and sweep later marks it unknown.

    Parameters
    ----------
    sessions : sqlalchemy.orm.sessionmaker
        Short-lived session factory.
    record_id : int
        Id returned by :func:`start_request`.
    outcome : Outcome
        Status and token counts to store.
    """
    try:
        with sessions() as session:
            row = session.get(Request, record_id)
            if row is None:
                _log_finish_failure(record_id, outcome, "record not found")
                return
            row.ended_at = utcnow()
            row.status = outcome.status
            row.usage_source = outcome.usage_source
            row.input_tokens = outcome.input_tokens
            row.output_tokens = outcome.output_tokens
            row.cache_hit_tokens = outcome.cache_hit_tokens
            session.commit()
    except Exception:  # pylint: disable=broad-exception-caught
        _log_finish_failure(record_id, outcome, "database write failed")


def _log_finish_failure(record_id, outcome, reason):
    logger.error(
        "failed to finish request id=%s reason=%s status=%s "
        "usage_source=%s input_tokens=%s output_tokens=%s "
        "cache_hit_tokens=%s",
        record_id,
        reason,
        outcome.status,
        outcome.usage_source,
        outcome.input_tokens,
        outcome.output_tokens,
        outcome.cache_hit_tokens,
        exc_info=True,
    )
