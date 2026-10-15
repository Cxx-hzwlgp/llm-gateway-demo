"""Upstream status mapping."""

import pytest

from lgw.proxy.errors import classify_upstream_status, passthrough_headers


@pytest.mark.parametrize("status", [400, 422, 429, 500, 502, 599])
def test_client_and_upstream_faults_pass_through(status):
    assert classify_upstream_status(status) == ("passthrough", None, None)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "upstream_auth_error"),
        (403, "upstream_auth_error"),
        (402, "upstream_account_error"),
        (302, "upstream_error"),
        (404, "upstream_error"),
        (409, "upstream_error"),
    ],
)
def test_other_statuses_become_gateway_errors(status, code):
    kind, got, message = classify_upstream_status(status)
    assert kind == "reject"
    assert got == code
    assert message


def test_retry_after_is_kept_only_for_429():
    headers = {"content-type": "application/json", "retry-after": "3"}
    assert passthrough_headers(429, headers) == {
        "Content-Type": "application/json",
        "Retry-After": "3",
    }
    assert "Retry-After" not in passthrough_headers(500, headers)
