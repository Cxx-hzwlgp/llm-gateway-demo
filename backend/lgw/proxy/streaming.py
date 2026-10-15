"""Stream chat completions and record how the stream ended."""

import json
import logging
from dataclasses import dataclass, field

import httpx
from flask import Response

from lgw.models import RequestStatus, UsageSource
from lgw.proxy.estimate import estimate_tokens
from lgw.proxy.providers.deepseek import ChunkNotes, note_payload
from lgw.proxy.recorder import Outcome, finish_request
from lgw.proxy.sse import parse_event, split_events

logger = logging.getLogger(__name__)

_READ_ERRORS = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)
_ERROR_EVENT = (
    b'data: {"error": {"message": "upstream connection interrupted", '
    b'"type": "upstream_error", "code": "upstream_interrupted"}}\n\n'
)


@dataclass
class _Stream:
    notes: ChunkNotes = field(default_factory=ChunkNotes)
    done: bool = False
    client_disconnected: bool = False
    upstream_failed: bool = False
    gateway_failed: bool = False


def streaming_response(sessions, record_id, upstream, body):
    """Return a streaming response that finishes ``record_id``.

    The generator closes the upstream response, including when the
    client disconnects. It does not read ``flask.request``.

    Parameters
    ----------
    sessions : sqlalchemy.orm.sessionmaker
        Short-lived session factory.
    record_id : int
        In-progress request id.
    upstream : httpx.Response
        Open upstream response with status 200.
    body : dict
        Parsed client request, kept for token estimation.

    Returns
    -------
    flask.Response
        ``text/event-stream`` response.
    """
    progress = _Stream()

    def generate():
        try:
            yield from _iter_events(upstream, progress)
        except GeneratorExit:
            progress.client_disconnected = True
            raise
        finally:
            upstream.close()
            finish_request(sessions, record_id, _outcome(progress, body))

    return Response(
        generate(),
        status=200,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _iter_events(upstream, progress):
    buffer = b""
    try:
        for chunk in upstream.iter_bytes():
            buffer += chunk
            events, buffer = split_events(buffer)
            for raw in events:
                _note_event(progress, raw)
                yield raw
    except _READ_ERRORS:
        progress.upstream_failed = True
        yield _ERROR_EVENT
        return
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("gateway error while streaming")
        progress.gateway_failed = True
        yield _ERROR_EVENT
        return
    if buffer:
        _note_event(progress, buffer)
        yield buffer
    if not progress.done:
        progress.upstream_failed = True
        yield _ERROR_EVENT


def _note_event(progress, raw):
    data = parse_event(raw)["data"]
    if data is None:
        return
    if data == "[DONE]":
        progress.done = True
        return
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        logger.warning("sse data event is not json")
        return
    if not isinstance(payload, dict):
        logger.warning("sse data event is not an object")
        return
    note_payload(progress.notes, payload)


def _outcome(progress, body):
    notes = progress.notes
    if progress.client_disconnected:
        status = RequestStatus.CLIENT_INTERRUPTED
    elif progress.gateway_failed and not progress.done:
        status = RequestStatus.GATEWAY_ERROR
    elif progress.done:
        status = RequestStatus.COMPLETED
    else:
        status = RequestStatus.UPSTREAM_INTERRUPTED
    if notes.usage is not None:
        return Outcome(status, UsageSource.REPORTED, *notes.usage)
    if status == RequestStatus.GATEWAY_ERROR:
        return Outcome(status, UsageSource.UNKNOWN, 0, 0, 0)
    if status == RequestStatus.COMPLETED:
        logger.warning("stream completed without usage")
    tokens = _estimated_tokens(body, notes.parts)
    return Outcome(status, UsageSource.ESTIMATED, *tokens)


def _estimated_tokens(request_body, output_parts):
    return estimate_tokens(request_body, "".join(output_parts))
