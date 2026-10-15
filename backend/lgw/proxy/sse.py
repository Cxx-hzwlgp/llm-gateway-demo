"""Split an upstream byte stream into Server-Sent Events.

Events are cut on the raw bytes so the gateway can forward them
unchanged. Line endings may be LF, CRLF or CR, including a mix in one
stream. A trailing CR stays in the remainder because the next byte may
turn it into CRLF.
"""


def split_events(buffer):
    """Return complete events and the unparsed remainder.

    Each event includes the blank line that terminated it.

    Parameters
    ----------
    buffer : bytes
        Bytes received so far and not yet forwarded.

    Returns
    -------
    tuple
        ``(events, remainder)``. ``events`` is a list of raw event
        bytes. ``remainder`` is an incomplete event, or empty.
    """
    events = []
    event_start = 0
    cursor = 0
    while True:
        found = _line_break(buffer, cursor)
        if found is None:
            break
        break_at, after = found
        if break_at == cursor:
            events.append(buffer[event_start:after])
            event_start = after
        cursor = after
    return events, buffer[event_start:]


def parse_event(raw):
    """Return the decoded ``data`` field of one event.

    Parameters
    ----------
    raw : bytes
        One event, with or without its terminating blank line.

    Returns
    -------
    dict
        ``data`` is the joined data field, or None when the event has
        no data field (for example a comment).
    """
    data_lines = []
    has_data = False
    for line in _event_lines(raw):
        if line.startswith(b":"):
            continue
        if not line.startswith(b"data:"):
            continue
        has_data = True
        value = line[5:]
        if value.startswith(b" "):
            value = value[1:]
        data_lines.append(value)
    if not has_data:
        return {"data": None}
    text = b"\n".join(data_lines).decode("utf-8", errors="replace")
    return {"data": text}


def _line_break(data, start):
    """Return the line ending span, or None if more bytes are needed."""
    index = start
    limit = len(data)
    while index < limit:
        byte = data[index]
        if byte == 0x0A:
            return index, index + 1
        if byte == 0x0D:
            if index + 1 >= limit:
                return None
            if data[index + 1] == 0x0A:
                return index, index + 2
            return index, index + 1
        index += 1
    return None


def _event_lines(raw):
    cursor = 0
    while cursor < len(raw):
        found = _line_break(raw, cursor)
        if found is None:
            if cursor < len(raw):
                yield raw[cursor:]
            break
        break_at, after = found
        yield raw[cursor:break_at]
        cursor = after
