"""Stale in-progress requests are marked unknown."""

from datetime import timedelta

from sqlalchemy import select

from lgw.models import (
    ApiKey,
    Base,
    Request,
    RequestStatus,
    UsageSource,
    utcnow,
)
from lgw.proxy import create_app
from tests.support import proxy_config
from tests.test_forward import _insert_key


def test_sweep_marks_only_stale_in_progress(tmp_path):
    config = proxy_config(f"sqlite:///{tmp_path / 'lgw.db'}")
    config["proxy"] = {"max_request_seconds": 60}
    app = create_app(config)
    Base.metadata.create_all(app.extensions["lgw_engine"])
    _insert_key(app)
    sessions = app.extensions["lgw_sessions"]
    now = utcnow()
    with sessions() as session:
        key_id = session.scalars(select(ApiKey)).one().id
        session.add_all(
            [
                _row(
                    key_id,
                    now - timedelta(seconds=120),
                    RequestStatus.IN_PROGRESS,
                ),
                _row(
                    key_id,
                    now - timedelta(seconds=10),
                    RequestStatus.IN_PROGRESS,
                ),
                _row(
                    key_id,
                    now - timedelta(seconds=120),
                    RequestStatus.COMPLETED,
                ),
            ]
        )
        session.commit()
    result = app.test_cli_runner().invoke(args=["sweep"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "swept 1 requests"
    with sessions() as session:
        rows = list(session.scalars(select(Request).order_by(Request.id)))
    assert rows[0].status == RequestStatus.GATEWAY_ERROR
    assert rows[0].usage_source == UsageSource.UNKNOWN
    assert rows[0].input_tokens == 0
    assert rows[0].output_tokens == 0
    assert rows[0].cache_hit_tokens == 0
    assert rows[0].ended_at is not None
    assert rows[1].status == RequestStatus.IN_PROGRESS
    assert rows[2].status == RequestStatus.COMPLETED
    app.extensions["lgw_http"].close()


def _row(key_id, started_at, status):
    source = UsageSource.UNKNOWN
    if status == RequestStatus.COMPLETED:
        source = UsageSource.REPORTED
    return Request(
        key_id=key_id,
        provider="deepseek",
        model="m",
        started_at=started_at,
        ended_at=None,
        status=status,
        usage_source=source,
        input_tokens=3,
        output_tokens=4,
        cache_hit_tokens=1,
    )
