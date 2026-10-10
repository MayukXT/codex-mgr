"""Bounded complete-history reads with explicit compressed-frame integrity."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import zstandard


class HistoryReadError(ValueError):
    """Fixed diagnostics without transcript or compression-library payloads."""


@dataclass(frozen=True)
class HistoryLimits:
    record_bytes: int = 16 * 1024 * 1024
    frame_bytes: int = 64 * 1024 * 1024
    window_bytes: int = 64 * 1024 * 1024
    decoded_bytes: int = 1024 * 1024 * 1024
    source_bytes: int = 1024 * 1024 * 1024
    frames: int = 65_536
    blocks: int = 65_536

    def __post_init__(self):
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in vars(self).values()
        ):
            raise HistoryReadError("Invalid history read limits.")


class _Source:
    def __init__(self, source, limit):
        self.source = source
        self.remaining = limit

    def read(self, size, *, eof=False):
        if not eof and size > self.remaining:
            raise HistoryReadError("History exceeds the source read limit.")
        # At the input boundary, one probe byte distinguishes EOF from excess.
        data = self.source.read(min(size, self.remaining + 1) if eof else size)
        if len(data) > self.remaining:
            raise HistoryReadError("History exceeds the source read limit.")
        self.remaining -= len(data)
        if len(data) != size and not (eof and not data):
            raise HistoryReadError("Compressed history is truncated.")
        return data


@contextmanager
def _regular_source(path):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HistoryReadError("History source is unavailable or unsafe.") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HistoryReadError("History source must be a regular file.")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source
    finally:
        os.close(descriptor)


def _compressed_chunks(source, limits):
    """Frame boundaries follow the Zstandard format; the library decodes blocks.

    Complete bounded frames allow the decoder to require a terminal/checksum.
    A stream reader's EOF alone does not establish complete-frame integrity.
    """
    decoder = zstandard.ZstdDecompressor(max_window_size=limits.window_bytes)
    decoded = 0
    frames = 0
    blocks = 0
    while magic := source.read(4, eof=True):
        frames += 1
        if frames > limits.frames:
            raise HistoryReadError("History exceeds the compressed frame limit.")
        number = int.from_bytes(magic, "little")
        if 0x184D2A50 <= number <= 0x184D2A5F:
            size = int.from_bytes(source.read(4), "little")
            while size:
                part = min(size, 65_536)
                source.read(part)
                size -= part
            continue
        if number != 0xFD2FB528:
            raise HistoryReadError("Compressed history has an invalid frame.")
        descriptor = source.read(1)[0]
        if descriptor & 8:
            raise HistoryReadError("Compressed history has an unsupported frame.")
        single_segment = bool(descriptor & 32)
        size_flag = descriptor >> 6
        size_bytes = (1 if single_segment else 0, 2, 4, 8)[size_flag]
        header_size = (0 if single_segment else 1) + (0, 1, 2, 4)[descriptor & 3] + size_bytes
        frame = bytearray(magic + bytes([descriptor]) + source.read(header_size))
        parameters = zstandard.get_frame_parameters(frame)
        if parameters.window_size > limits.window_bytes or parameters.dict_id:
            raise HistoryReadError("Compressed history requires unsupported decoder parameters.")
        remaining = min(limits.frame_bytes, limits.decoded_bytes - decoded)
        if (
            parameters.content_size
            not in {zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR}
            and parameters.content_size > remaining
        ):
            raise HistoryReadError("History exceeds the decoded frame limit.")
        while True:
            blocks += 1
            if blocks > limits.blocks:
                raise HistoryReadError("History exceeds the compressed block limit.")
            header = source.read(3)
            block = int.from_bytes(header, "little")
            kind, size = (block >> 1) & 3, block >> 3
            if kind == 3 or size > 131_072:
                raise HistoryReadError("Compressed history has an invalid block.")
            payload_size = 1 if kind == 1 else size
            if len(frame) + 3 + payload_size + (4 if descriptor & 4 else 0) > limits.frame_bytes:
                raise HistoryReadError("History exceeds the compressed frame size limit.")
            frame.extend(header)
            frame.extend(source.read(payload_size))
            if block & 1:
                break
        if descriptor & 4:
            frame.extend(source.read(4))
        try:
            output = decoder.decompress(
                frame, max_output_size=max(1, remaining), allow_extra_data=False
            )
        except zstandard.ZstdError as exc:
            raise HistoryReadError("Compressed history failed integrity validation.") from exc
        decoded += len(output)
        if decoded > limits.decoded_bytes:
            raise HistoryReadError("History exceeds the decoded read limit.")
        yield output


def iter_history_records(path: Path, *, limits: HistoryLimits | None = None) -> Iterator[bytes]:
    """Yield exact newline-terminated records; never publish decompressed files.

    The caller must exhaust this iterator for whole-history validation or close
    it when abandoning a read. JSON/ordinal/lineage validation is a separate step.
    """
    limits = limits or HistoryLimits()
    with _regular_source(path) as handle:
        if not path.name.endswith(".jsonl.zst"):
            total = 0
            budget = min(limits.decoded_bytes, limits.source_bytes)
            while raw := handle.readline(min(limits.record_bytes + 1, budget - total + 1)):
                total += len(raw)
                if len(raw) > limits.record_bytes or total > budget:
                    raise HistoryReadError("History exceeds the record or read limit.")
                if not raw.endswith(b"\n"):
                    raise HistoryReadError("History ends with an incomplete record.")
                yield raw
            return
        pending = bytearray()
        try:
            for chunk in _compressed_chunks(_Source(handle, limits.source_bytes), limits):
                start = 0
                while (end := chunk.find(b"\n", start)) >= 0:
                    if len(pending) + end + 1 - start > limits.record_bytes:
                        raise HistoryReadError("History exceeds the record read limit.")
                    pending.extend(chunk[start : end + 1])
                    yield bytes(pending)
                    pending.clear()
                    start = end + 1
                if len(pending) + len(chunk) - start > limits.record_bytes:
                    raise HistoryReadError("History exceeds the record read limit.")
                pending.extend(chunk[start:])
            if pending:
                raise HistoryReadError("History ends with an incomplete record.")
        except zstandard.ZstdError as exc:
            raise HistoryReadError("Compressed history failed integrity validation.") from exc
