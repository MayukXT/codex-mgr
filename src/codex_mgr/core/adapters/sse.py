"""Bounded incremental SSE framing without logging upstream payloads."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

from codex_mgr.core.adapters.tools import ProtocolError, encoded_json, object_json

_NEWLINE = re.compile(r"[\r\n]")


@dataclass(frozen=True)
class ServerEvent:
    data: str = field(repr=False)
    name: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class JsonEvent:
    data: dict | None = field(repr=False)
    name: str | None = field(default=None, repr=False)
    done: bool = False


class _Framer:
    def __init__(self, line_limit, event_limit, event_count_limit):
        self.buffer = ""
        self.parts = []
        self.name = None
        self.size = 0
        self.count = 0
        self.line_limit = line_limit
        self.event_limit = event_limit
        self.event_count_limit = event_count_limit

    def _line(self, line, delimiter_bytes):
        size = len(line.encode("utf-8"))
        if size > self.line_limit:
            raise ProtocolError("Upstream SSE line exceeds the size limit.")
        self.size += size + delimiter_bytes
        if self.size > self.event_limit:
            raise ProtocolError("Upstream SSE event exceeds the size limit.")
        if not line:
            event = ServerEvent("\n".join(self.parts), self.name) if self.parts else None
            self.parts, self.name, self.size = [], None, 0
            if event is not None:
                self.count += 1
                if self.count > self.event_count_limit:
                    raise ProtocolError("Upstream SSE event count exceeds the limit.")
            return event
        if line.startswith(":"):
            return None
        key, separator, value = line.partition(":")
        if not separator:
            value = ""
        value = value.removeprefix(" ")
        if key == "data":
            self.parts.append(value)
        elif key == "event":
            self.name = value or None
        # id/retry and extension fields are transport metadata, not JSON data.
        return None

    def feed(self, value, *, final=False):
        self.buffer += value
        offset = 0
        while match := _NEWLINE.search(self.buffer, offset):
            position = match.start()
            if self.buffer[position] == "\r" and position + 1 == len(self.buffer) and not final:
                break
            length = 2 if self.buffer[position : position + 2] == "\r\n" else 1
            event = self._line(self.buffer[offset:position], length)
            offset = position + length
            if event is not None:
                yield event
        self.buffer = self.buffer[offset:]
        if len(self.buffer.removesuffix("\r").encode("utf-8")) > self.line_limit:
            raise ProtocolError("Upstream SSE line exceeds the size limit.")
        if self.size + len(self.buffer.encode("utf-8")) > self.event_limit:
            raise ProtocolError("Upstream SSE event exceeds the size limit.")
        if final:
            if self.buffer.startswith(":") and not self.parts:
                self.buffer = ""  # A final keepalive comment carries no event data.
            if self.buffer or self.parts:
                raise ProtocolError("Upstream ended inside an SSE event.")


def server_events(
    chunks,
    *,
    max_line_bytes=1024 * 1024,
    max_event_bytes=1024 * 1024,
    max_total_bytes=32 * 1024 * 1024,
    max_events=100000,
):
    """Decode arbitrary UTF-8/CRLF chunk boundaries and preserve SSE data lines."""
    for limit in (max_line_bytes, max_event_bytes, max_total_bytes, max_events):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ProtocolError("SSE limits must be positive integers.")
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    framer = _Framer(max_line_bytes, max_event_bytes, max_events)
    first, total = True, 0
    try:
        for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ProtocolError("Upstream stream must contain bytes.")
            total += chunk.nbytes if isinstance(chunk, memoryview) else len(chunk)
            if total > max_total_bytes:
                raise ProtocolError("Upstream stream exceeds the total size limit.")
            value = decoder.decode(chunk.tobytes() if isinstance(chunk, memoryview) else chunk)
            if value and first:
                value, first = value.removeprefix("\ufeff"), False
            yield from framer.feed(value)
        yield from framer.feed(decoder.decode(b"", final=True), final=True)
    except UnicodeError as exc:
        raise ProtocolError("Upstream SSE contains invalid UTF-8.") from exc


def json_events(chunks, **limits):
    """Decode object events or the Chat completion sentinel; reject ambiguous JSON."""
    for event in server_events(chunks, **limits):
        if event.data == "[DONE]":
            yield JsonEvent(None, event.name, done=True)
            continue
        if not event.data:
            continue
        try:
            data = object_json(event.data)
        except ProtocolError as exc:
            raise ProtocolError("Upstream SSE contains invalid event JSON.") from exc
        yield JsonEvent(data, event.name)


def encode_event(event, *, max_bytes=8 * 1024 * 1024):
    """Create one UTF-8 SSE record; event names and JSON cannot inject records."""
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("type"), str)
        or not re.fullmatch(r"[a-z0-9_.]{1,128}", event["type"])
    ):
        raise ProtocolError("Invalid outgoing SSE event type.")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ProtocolError("SSE limits must be positive integers.")
    try:
        encoded = ("event: " + event["type"] + "\ndata: " + encoded_json(event) + "\n\n").encode()
    except UnicodeError as exc:
        raise ProtocolError("Invalid outgoing SSE text.") from exc
    if len(encoded) > max_bytes:
        raise ProtocolError("Outgoing SSE event exceeds the size limit.")
    return encoded
