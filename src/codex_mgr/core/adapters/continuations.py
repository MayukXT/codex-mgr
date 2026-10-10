"""Authenticated, connection-bound storage for exact Messages thinking blocks."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codex_mgr.core.adapters.tools import ProtocolError, encoded_json, object_json, text
from codex_mgr.utils.files import atomic_write, mutation_lock
from codex_mgr.utils.paths import get_codex_home

_MAX_CLEAR = 2 * 1024 * 1024
_MAX_ENVELOPE = 3 * 1024 * 1024
_DOMAIN = b"cmgr/messages-thinking/v1\0"
_PREFIX_DOMAIN = b"cmgr/messages-prefix/v1\0"
_FAILURE = "Reasoning continuation is invalid or belongs to another connection."
_PREFIX_FAILURE = "Reasoning history no longer matches its saved conversation prefix."


def _canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid conversation prefix.") from exc


class MessagesPrefix:
    """Incremental commitment to normalized Messages history; retain no content."""

    def __init__(self, payload):
        if not isinstance(payload, dict):
            raise ProtocolError("Invalid conversation prefix.")
        system, tools, messages = (payload.get(key, []) for key in ("system", "tools", "messages"))
        if any(not isinstance(value, list) for value in (system, tools, messages)):
            raise ProtocolError("Invalid conversation prefix.")
        self._digest = hashlib.sha256(_PREFIX_DOMAIN + _canonical([system, tools])).digest()
        self._role = None
        for message in messages:
            if not isinstance(message, dict):
                raise ProtocolError("Invalid conversation prefix.")
            self.append(message.get("role"), message.get("content"))

    @property
    def digest(self):
        return self._digest

    def append(self, role, content):
        if (
            not isinstance(role, str)
            or role not in {"user", "assistant"}
            or not isinstance(content, list)
        ):
            raise ProtocolError("Invalid conversation prefix.")
        if role != self._role:
            self._digest = hashlib.sha256(
                _PREFIX_DOMAIN + self._digest + b"role\0" + _canonical(role)
            ).digest()
            self._role = role
        for block in content:
            if not isinstance(block, dict):
                raise ProtocolError("Invalid conversation prefix.")
            self._digest = hashlib.sha256(
                _PREFIX_DOMAIN + self._digest + b"block\0" + _canonical(block)
            ).digest()


def thinking_block(value):
    """Accept only the two documented opaque/signed Messages block shapes."""
    if not isinstance(value, dict):
        raise ProtocolError("Invalid Messages thinking block.")
    if value.get("type") == "thinking" and set(value) == {"type", "thinking", "signature"}:
        text(value["thinking"])
        text(value["signature"], nonempty=True)
    elif value.get("type") == "redacted_thinking" and set(value) == {"type", "data"}:
        text(value["data"], nonempty=True)
    else:
        raise ProtocolError("Invalid Messages thinking block.")
    return value


def connection_scope(protocol, endpoint, headers):
    """Bind the selected route and resolved headers without exposing their values."""
    if protocol != "messages" or not isinstance(headers, dict):
        raise ProtocolError("Unsupported reasoning continuation connection.")
    normalized = {}
    for name, value in headers.items():
        key = text(name, nonempty=True).lower()
        if key in normalized:
            raise ProtocolError("Ambiguous continuation authentication headers.")
        normalized[key] = text(value)
    document = [protocol, text(endpoint, nonempty=True), sorted(normalized.items())]
    return hashlib.sha256(encoded_json(document).encode("utf-8")).digest()


def messages_connection_scope(provider, headers):
    """A manager version change must not invalidate an unchanged upstream identity."""
    custom_names = {
        name.lower()
        for values in (provider.http_headers, provider.env_http_headers)
        for name in (values or {})
    }
    scoped_headers = {
        name: value
        for name, value in headers.items()
        if name.lower() != "user-agent" or "user-agent" in custom_names
    }
    return connection_scope("messages", provider.base_url + "/messages", scoped_headers)


def load_continuation_key(create):
    """Keep a checksummed home key; never repair existing storage implicitly."""
    root = get_codex_home()
    path = root / "cmgr-adapter-continuations.key"
    checksum = path.with_name(path.name + ".sha256")
    lock = path.with_name(path.name + ".cmgr.lock")

    def checked(candidate):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProtocolError("Continuation key storage must use ordinary private files.")
        if os.name != "nt" and info.st_mode & 0o077:
            raise ProtocolError("Continuation key storage has unsafe permissions.")

    def read_bytes(candidate, size):
        checked(candidate)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(candidate, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
                raise ProtocolError("Continuation key storage is corrupt.")
            value = os.read(descriptor, size + 1)
        finally:
            os.close(descriptor)
        if len(value) != size:
            raise ProtocolError("Continuation key storage is corrupt.")
        return value

    def read():
        key = read_bytes(path, 32)
        expected = hashlib.sha256(key).hexdigest().encode("ascii") + b"\n"
        if not hmac.compare_digest(read_bytes(checksum, 65), expected):
            raise ProtocolError("Continuation key integrity check failed.")
        return key

    try:
        if path.exists():
            return read()
        checked(path)  # Includes dangling links.
        checked(checksum)
        if checksum.exists():
            raise ProtocolError("Restore the original continuation key before resuming reasoning.")
        if not create:
            raise ProtocolError("Restore the original continuation key before resuming reasoning.")
        checked(lock)
        with mutation_lock(path):
            checked(path)
            checked(checksum)
            checked(lock)
            if not path.exists():
                if checksum.exists():
                    raise ProtocolError("Continuation key storage is incomplete.")
                key = AESGCM.generate_key(bit_length=256)
                atomic_write(path, key, expected=None)
                atomic_write(
                    checksum, hashlib.sha256(key).hexdigest().encode("ascii") + b"\n", expected=None
                )
            return read()
    except ProtocolError:
        raise
    except (OSError, ValueError) as exc:
        raise ProtocolError("Continuation key storage is unavailable.") from exc


@dataclass(frozen=True)
class ContinuationCodec:
    scope: bytes = field(repr=False)
    key_loader: Callable[[bool], bytes] = field(default=load_continuation_key, repr=False)

    def __post_init__(self):
        if not isinstance(self.scope, bytes) or len(self.scope) != 32:
            raise ProtocolError("Invalid continuation connection binding.")

    def _binding(self, model):
        return hashlib.sha256(
            self.scope + encoded_json(text(model, nonempty=True)).encode()
        ).digest()

    def seal(self, block, model, *, prefix=None):
        block = thinking_block(block)
        if prefix is not None and (not isinstance(prefix, bytes) or len(prefix) != 32):
            raise ProtocolError("Invalid continuation prefix binding.")
        version = "cmgr1" if prefix is None else "cmgr2"
        value = block if prefix is None else {"block": block, "prefix": prefix.hex()}
        clear = encoded_json(value).encode("utf-8")
        if len(clear) > _MAX_CLEAR:
            raise ProtocolError("Reasoning continuation exceeds the size limit.")
        binding = self._binding(model)
        nonce = secrets.token_bytes(12)
        domain = _DOMAIN if version == "cmgr1" else b"cmgr/messages-thinking/v2\0"
        cipher = AESGCM(self.key_loader(True)).encrypt(nonce, clear, domain + binding)
        payload = base64.urlsafe_b64encode(nonce + cipher).decode("ascii").rstrip("=")
        return version + "." + binding.hex() + "." + payload

    def _decode(self, envelope, model):
        try:
            if not isinstance(envelope, str) or len(envelope) > _MAX_ENVELOPE:
                raise ValueError
            match = re.fullmatch(r"(cmgr[12])\.([a-f0-9]{64})\.([A-Za-z0-9_-]+)", envelope)
            binding = self._binding(model)
            if match is None or not hmac.compare_digest(match[2], binding.hex()):
                raise ValueError
            encoded = match[3]
            raw = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            if len(raw) < 28 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
                raise ValueError
            domain = _DOMAIN if match[1] == "cmgr1" else b"cmgr/messages-thinking/v2\0"
            clear = AESGCM(self.key_loader(False)).decrypt(raw[:12], raw[12:], domain + binding)
            if len(clear) > _MAX_CLEAR:
                raise ValueError
            value = object_json(clear.decode("utf-8"))
            if match[1] == "cmgr1":
                return thinking_block(value), None
            if (
                set(value) != {"block", "prefix"}
                or not isinstance(value["prefix"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["prefix"])
            ):
                raise ValueError
            return thinking_block(value["block"]), bytes.fromhex(value["prefix"])
        except (
            ProtocolError,
            ValueError,
            TypeError,
            UnicodeError,
            binascii.Error,
            InvalidTag,
        ) as exc:
            raise ProtocolError(_FAILURE) from exc

    def inspect(self, envelope, model):
        """Authenticate for offline inspection; this does not certify replay context."""
        return self._decode(envelope, model)[0]

    def open(self, envelope, model, *, prefix=None):
        """Verify v2 against the actual prefix; legacy v1 retains its older binding."""
        block, saved = self._decode(envelope, model)
        if saved is not None and (
            not isinstance(prefix, bytes)
            or len(prefix) != 32
            or not hmac.compare_digest(saved, prefix)
        ):
            raise ProtocolError(_PREFIX_FAILURE)
        return block
