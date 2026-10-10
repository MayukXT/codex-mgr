"""Bounded incremental SSE framing without logging upstream payloads."""

from __future__ import annotations

import codecs
import io
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
        self.line = io.StringIO()
        self.line_size = 0
        self.pending_cr = False
        self.parts = []
        self.name = None
        self.size = 0
        self.count = 0
        self.line_limit = line_limit
        self.event_limit = event_limit
        self.event_count_limit = event_count_limit

    def _line(self, delimiter_bytes):
        line = self.line.getvalue()
        self.size += self.line_size + delimiter_bytes
        self.line, self.line_size = io.StringIO(), 0
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

    def _append(self, value):
        if value:
            self.line_size += len(value.encode("utf-8"))
            self.line.write(value)
        if self.line_size > self.line_limit:
            raise ProtocolError("Upstream SSE line exceeds the size limit.")
        if self.size + self.line_size > self.event_limit:
            raise ProtocolError("Upstream SSE event exceeds the size limit.")

    def feed(self, value, *, final=False):
        offset = 0
        if self.pending_cr and (value or final):
            length = 2 if value.startswith("\n") else 1
            offset = length - 1
            self.pending_cr = False
            event = self._line(length)
            if event is not None:
                yield event
        while match := _NEWLINE.search(value, offset):
            position = match.start()
            self._append(value[offset:position])
            if value[position] == "\r" and position + 1 == len(value) and not final:
                self.pending_cr = True
                offset = position + 1
                break
            length = 2 if value[position : position + 2] == "\r\n" else 1
            event = self._line(length)
            offset = position + length
            if event is not None:
                yield event
        self._append(value[offset:])
        if self.size + self.line_size + int(self.pending_cr) > self.event_limit:
            raise ProtocolError("Upstream SSE event exceeds the size limit.")
        if final:
            if self.line.getvalue().startswith(":") and not self.parts:
                self.line_size = 0  # A final keepalive comment carries no event data.
            if self.line_size or self.parts:
                raise ProtocolError("Upstream ended inside an SSE event.")


class _Decoder:
    def __init__(
        self,
        *,
        max_line_bytes=1024 * 1024,
        max_event_bytes=1024 * 1024,
        max_total_bytes=32 * 1024 * 1024,
        max_events=100000,
    ):
        for limit in (max_line_bytes, max_event_bytes, max_total_bytes, max_events):
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ProtocolError("SSE limits must be positive integers.")
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.framer = _Framer(max_line_bytes, max_event_bytes, max_events)
        self.first, self.total, self.total_limit = True, 0, max_total_bytes

    def feed(self, chunk):
        try:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ProtocolError("Upstream stream must contain bytes.")
            self.total += chunk.nbytes if isinstance(chunk, memoryview) else len(chunk)
            if self.total > self.total_limit:
                raise ProtocolError("Upstream stream exceeds the total size limit.")
            value = self.decoder.decode(chunk.tobytes() if isinstance(chunk, memoryview) else chunk)
            if value and self.first:
                value, self.first = value.removeprefix("\ufeff"), False
            yield from self.framer.feed(value)
        except UnicodeError as exc:
            raise ProtocolError("Upstream SSE contains invalid UTF-8.") from exc

    def finish(self):
        try:
            yield from self.framer.feed(self.decoder.decode(b"", final=True), final=True)
        except UnicodeError as exc:
            raise ProtocolError("Upstream SSE contains invalid UTF-8.") from exc


def server_events(chunks, **limits):
    """Decode arbitrary UTF-8/CRLF chunk boundaries and preserve SSE data lines."""
    decoder = _Decoder(**limits)
    for chunk in chunks:
        yield from decoder.feed(chunk)
    yield from decoder.finish()


async def async_server_events(chunks, **limits):
    decoder = _Decoder(**limits)
    async for chunk in chunks:
        for event in decoder.feed(chunk):
            yield event
    for event in decoder.finish():
        yield event


def _json_event(event):
    if event.data == "[DONE]":
        return JsonEvent(None, event.name, done=True)
    if not event.data:
        return None
    try:
        return JsonEvent(object_json(event.data), event.name)
    except ProtocolError as exc:
        raise ProtocolError("Upstream SSE contains invalid event JSON.") from exc


def json_events(chunks, **limits):
    """Decode object events or the Chat completion sentinel; reject ambiguous JSON."""
    for event in server_events(chunks, **limits):
        result = _json_event(event)
        if result is not None:
            yield result


async def async_json_events(chunks, **limits):
    async for event in async_server_events(chunks, **limits):
        result = _json_event(event)
        if result is not None:
            yield result


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
