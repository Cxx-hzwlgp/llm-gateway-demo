"""Final request writes do not escape to the client."""

import logging

from lgw.models import RequestStatus, UsageSource
from lgw.proxy.recorder import Outcome, finish_request


def test_finish_failure_is_logged_and_swallowed(caplog):
    class _Broken:
        def __enter__(self):
            raise RuntimeError("locked")

        def __exit__(self, exc_type, exc, traceback):
            return False

    outcome = Outcome(RequestStatus.COMPLETED, UsageSource.REPORTED, 1, 2, 3)
    with caplog.at_level(logging.ERROR):
        finish_request(lambda: _Broken(), 9, outcome)
    assert "id=9" in caplog.text
    assert "status=completed" in caplog.text
    assert "usage_source=reported" in caplog.text
    assert "input_tokens=1" in caplog.text
    assert "output_tokens=2" in caplog.text
    assert "cache_hit_tokens=3" in caplog.text
