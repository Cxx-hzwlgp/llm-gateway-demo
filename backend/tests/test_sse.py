"""SSE framing keeps the upstream bytes."""

from lgw.proxy.sse import parse_event, split_events


def _feed(chunks):
    events = []
    rest = b""
    for chunk in chunks:
        found, rest = split_events(rest + chunk)
        events.extend(found)
    return events, rest


def test_multiline_comment_and_multiple_events():
    raw = b"event: message\ndata: hello\ndata: world\n\n: keep-alive\n\n"
    events, rest = split_events(raw)
    assert rest == b""
    assert events[0] == b"event: message\ndata: hello\ndata: world\n\n"
    assert events[1] == b": keep-alive\n\n"
    assert parse_event(events[0])["data"] == "hello\nworld"
    assert parse_event(events[1])["data"] is None


def test_crlf_and_lf_bytes_match_the_input():
    raw = b"data: a\r\n\r\ndata: b\n\n"
    events, rest = split_events(raw)
    assert rest == b""
    assert b"".join(events) == raw
    assert events[0] == b"data: a\r\n\r\n"
    assert parse_event(events[0])["data"] == "a"
    assert parse_event(events[1])["data"] == "b"


def test_line_ending_split_across_chunks():
    events, rest = _feed([b"data: hi\r", b"\n\ndata: next\n", b"\n"])
    assert rest == b""
    assert events == [b"data: hi\r\n\n", b"data: next\n\n"]


def test_one_chunk_holds_several_events():
    raw = b"data: a\n\ndata: b\n\ndata: c\n\n"
    events, rest = split_events(raw)
    assert rest == b""
    assert b"".join(events) == raw
    assert [parse_event(event)["data"] for event in events] == ["a", "b", "c"]


def test_missing_blank_line_stays_in_the_remainder():
    raw = b"data: [DONE]"
    events, rest = split_events(raw)
    assert events == []
    assert rest == raw
    assert parse_event(rest)["data"] == "[DONE]"


def test_trailing_cr_is_held_until_the_next_byte():
    events, rest = split_events(b"data: hi\r\r")
    assert events == []
    assert rest == b"data: hi\r\r"
    events, rest = split_events(b"data: hi\r\rX")
    assert events == [b"data: hi\r\r"]
    assert rest == b"X"
    assert parse_event(events[0])["data"] == "hi"
