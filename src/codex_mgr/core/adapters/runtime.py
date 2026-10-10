"""Per-owner authenticated loopback transport for the pure protocol codecs."""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets
import threading
from urllib.parse import urlparse

import httpx

from codex_mgr.core.adapters.continuations import ContinuationCodec, messages_connection_scope
from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.responses import adapt_response
from codex_mgr.core.adapters.sse import encode_event
from codex_mgr.core.adapters.streaming import async_adapt_stream, buffered_stream
from codex_mgr.core.adapters.tools import ProtocolError, encoded_json, object_json
from codex_mgr.core.auth import CredentialError
from codex_mgr.core.models import provider_headers

_MAX_BODY = 8 * 1024 * 1024
_MAX_HEADER = 16 * 1024
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class AdapterError(RuntimeError):
    """Fixed public diagnostics; upstream URLs, headers and bodies stay private."""


class _RequestError(Exception):
    def __init__(self, status):
        self.status = status


async def _bounded(awaitable, timeout):
    """Bound owned I/O without swallowing cancellation on Python 3.10."""
    pending = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait((pending,), timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


class AdapterRuntime:
    """Own one loop, listener, HTTP client and bounded set of request tasks.

    The base URL/token are temporary launch credentials, never durable settings.
    This transport does not enable a provider until native workflow acceptance.
    """

    def __init__(self, provider, *, max_active=4, idle_timeout=30.0, request_timeout=10.0):
        if (
            not isinstance(max_active, int)
            or isinstance(max_active, bool)
            or not 1 <= max_active <= 32
            or not isinstance(idle_timeout, (int, float))
            or isinstance(idle_timeout, bool)
            or not 0 < idle_timeout <= 300
            or not isinstance(request_timeout, (int, float))
            or isinstance(request_timeout, bool)
            or not 0 < request_timeout <= 60
        ):
            raise AdapterError("Invalid adapter limits.")
        if (
            provider.wire_api not in {"chat", "anthropic_proxy"}
            or provider.auth_type == "official_oauth"
        ):
            raise AdapterError("This connection has no verified adapter transport.")
        self._protocol = "messages" if provider.wire_api == "anthropic_proxy" else "chat"
        self._url = provider.base_url + (
            "/messages" if self._protocol == "messages" else "/chat/completions"
        )
        parsed = urlparse(self._url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AdapterError("Invalid upstream endpoint.")
        try:
            httpx.URL(self._url)
            _ = parsed.port
        except (ValueError, httpx.InvalidURL) as exc:
            raise AdapterError("Invalid upstream endpoint.") from exc
        try:
            if (
                provider.auth_type != "none"
                and not provider.auth_token
                and not provider.http_headers
                and not provider.env_http_headers
            ):
                raise CredentialError("The selected upstream credential is missing.")
            selected = provider.model_copy(deep=True)
            if selected.auth_type == "none":
                selected.auth_token = None
            headers = provider_headers(selected)
        except CredentialError as exc:
            raise AdapterError("The selected upstream credential is unavailable.") from exc
        forbidden = {
            "host",
            "connection",
            "content-length",
            "transfer-encoding",
            "content-type",
            "content-encoding",
            "accept-encoding",
            "upgrade",
            "proxy-authorization",
            "proxy-connection",
            "keep-alive",
            "trailer",
            "te",
        }
        normalized = [key.lower() for key in headers]
        if len(set(normalized)) != len(normalized) or any(key in forbidden for key in normalized):
            raise AdapterError("An upstream header conflicts with the adapter transport.")
        if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in headers.values()):
            raise AdapterError("Invalid upstream headers.")
        try:
            httpx.Headers(headers)
        except (ValueError, UnicodeError) as exc:
            raise AdapterError("Invalid upstream headers.") from exc
        self._headers = dict(headers)
        self._continuations = (
            ContinuationCodec(messages_connection_scope(selected, self._headers))
            if self._protocol == "messages"
            else None
        )
        self._model = provider.default_model
        self._max_output_tokens = provider.max_output_tokens
        self._thinking_budget = provider.reasoning_budget
        self._thinking_summary = provider.messages_summary if self._protocol == "messages" else None
        self._upstream_streaming = provider.streaming
        self._max_active, self._idle_timeout, self._request_timeout = (
            max_active,
            idle_timeout,
            request_timeout,
        )
        self._token = secrets.token_urlsafe(32)
        self._ready = threading.Event()
        self._closing = threading.Event()
        self._loop = self._stop = self._thread = self._server = None
        self._base_url = None
        self._startup_failed = False
        self._tasks = set()

    @property
    def base_url(self):
        if self._base_url is None or self._thread is None or not self._thread.is_alive():
            raise AdapterError("The owned adapter is not running.")
        return self._base_url

    def reasoning_compatible(self, item):
        """Authenticate offline for this route/model; actual replay also checks its prefix."""
        if self._continuations is None or not isinstance(item, dict):
            return False
        try:
            self._continuations.inspect(item.get("encrypted_content"), self._model)
            return True
        except ProtocolError:
            return False

    @property
    def token(self):
        _ = self.base_url  # Require a live owner before exposing launch credentials.
        return self._token

    def __enter__(self):
        if self._thread is not None:
            raise AdapterError("An adapter owner cannot be started twice.")
        self._thread = threading.Thread(target=self._run, name="cmgr-adapter", daemon=False)
        self._thread.start()
        if not self._ready.wait(5) or self._startup_failed or not self._thread.is_alive():
            self.close()
            raise AdapterError("The owned adapter could not start.")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._closing.set()
        thread = self._thread
        if thread is None:
            return
        if self._loop is not None and self._stop is not None and thread.is_alive():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        thread.join(5)
        self._base_url = None
        self._headers.clear()
        self._token = ""
        if thread.is_alive():
            raise AdapterError("The owned adapter did not finish shutting down.")

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception:  # noqa: BLE001 - suppress credential-bearing thread tracebacks
            # Thread exceptions otherwise print credential-bearing tracebacks.
            self._startup_failed = True
        finally:
            self._ready.set()

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        if self._closing.is_set():
            return
        self._loop.set_exception_handler(lambda *_: None)
        timeout = httpx.Timeout(self._idle_timeout, connect=min(5.0, self._idle_timeout))
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=self._max_active),
        ) as client:
            self._client = client
            self._server = await asyncio.start_server(
                self._connected, "127.0.0.1", 0, limit=_MAX_HEADER + 1
            )
            port = self._server.sockets[0].getsockname()[1]
            self._base_url = f"http://127.0.0.1:{port}/v1"
            self._ready.set()
            if self._closing.is_set():
                self._stop.set()
            try:
                await self._stop.wait()
            finally:
                self._server.close()
                tasks = list(self._tasks)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                # Newer asyncio waits for active client transports here. Close
                # their owned tasks first so an idle upstream cannot delay exit.
                await self._server.wait_closed()

    def _connected(self, reader, writer):
        if self._stop.is_set() or len(self._tasks) >= self._max_active:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, writer, data):
        writer.write(data)
        await _bounded(writer.drain(), self._request_timeout)

    async def _json(self, writer, status, body):
        data = encoded_json(body).encode()
        header = f"HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n".encode()
        await self._write(writer, header + data)

    async def _request(self, reader):
        try:
            raw = await _bounded(reader.readuntil(b"\r\n\r\n"), self._request_timeout)
        except (
            asyncio.LimitOverrunError,
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
        ) as exc:
            raise _RequestError(400) from exc
        if len(raw) > _MAX_HEADER:
            raise _RequestError(431)
        try:
            lines = raw.decode("ascii").split("\r\n")
        except UnicodeError as exc:
            raise _RequestError(400) from exc
        if lines[0] != "POST /v1/responses HTTP/1.1":
            raise _RequestError(404)
        headers = {}
        for line in lines[1:-2]:
            name, separator, value = line.partition(":")
            if (
                not separator
                or not _HEADER_NAME.fullmatch(name)
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise _RequestError(400)
            name = name.lower()
            if name in headers:
                raise _RequestError(400)
            headers[name] = value.strip()
        if (
            headers.get("host") != self._base_url.removeprefix("http://").removesuffix("/v1")
            or "origin" in headers
        ):
            raise _RequestError(403)
        if not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self._token):
            raise _RequestError(401)
        if "transfer-encoding" in headers or "content-encoding" in headers or "expect" in headers:
            raise _RequestError(400)
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise _RequestError(415)
        size = headers.get("content-length", "")
        if not re.fullmatch(r"[0-9]{1,10}", size):
            raise _RequestError(400)
        size = int(size)
        if not 0 < size <= _MAX_BODY:
            raise _RequestError(413)
        try:
            body = await _bounded(reader.readexactly(size), self._request_timeout)
            request = object_json(body.decode("utf-8"))
            return adapt_request(
                request,
                self._protocol,
                max_output_tokens=self._max_output_tokens,
                thinking_budget=self._thinking_budget,
                thinking_summary=self._thinking_summary,
                continuations=self._continuations,
            )
        except (
            ProtocolError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
        ) as exc:
            raise _RequestError(400) from exc

    async def _forward(self, adapted, writer):
        streamed = False
        try:
            upstream_payload = dict(adapted.payload)
            upstream_payload["stream"] = adapted.payload["stream"] and self._upstream_streaming
            if not upstream_payload["stream"]:
                upstream_payload.pop("stream_options", None)
            async with self._client.stream(
                "POST",
                self._url,
                headers={**self._headers, "Accept-Encoding": "identity"},
                json=upstream_payload,
            ) as upstream:
                if upstream.status_code != 200:
                    status = (
                        upstream.status_code if upstream.status_code in {401, 403, 429} else 502
                    )
                    await self._json(
                        writer,
                        status,
                        {"error": {"message": "The selected upstream rejected the request."}},
                    )
                    return
                if upstream.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    raise ProtocolError("Unexpected upstream content encoding.")
                content_type = (
                    upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if upstream_payload["stream"]:
                    if content_type != "text/event-stream":
                        raise ProtocolError("Upstream did not return an event stream.")
                    await self._write(
                        writer,
                        b"HTTP/1.1 200 Response\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
                    )
                    streamed = True
                    iterator = async_adapt_stream(upstream.aiter_raw(), adapted)
                    try:
                        async for event in iterator:
                            await self._write(writer, encode_event(event))
                    finally:
                        await iterator.aclose()
                else:
                    if content_type != "application/json":
                        raise ProtocolError("Upstream did not return response JSON.")
                    chunks, size = [], 0
                    async for chunk in upstream.aiter_raw():
                        size += len(chunk)
                        if size > _MAX_BODY:
                            raise ProtocolError("Upstream response exceeds the size limit.")
                        chunks.append(chunk)
                    body = object_json(b"".join(chunks).decode("utf-8"))
                    if adapted.payload["stream"]:
                        iterator = buffered_stream(body, adapted)
                        await self._write(
                            writer,
                            b"HTTP/1.1 200 Response\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
                        )
                        streamed = True
                        for event in iterator:
                            await self._write(writer, encode_event(event))
                    else:
                        await self._json(writer, 200, adapt_response(body, adapted))
        except (ProtocolError, UnicodeError, httpx.HTTPError, OSError, asyncio.TimeoutError):
            if not streamed:
                await self._json(
                    writer,
                    502,
                    {"error": {"message": "The upstream response could not be validated."}},
                )
            # A transport failure after SSE headers closes the connection; native
            # Codex rejects EOF without a validated terminal instead of succeeding.

    async def _handle(self, reader, writer):
        forward = disconnect = None
        try:
            adapted = await self._request(reader)
            forward = asyncio.create_task(self._forward(adapted, writer))
            # This is a single-request connection. EOF or extra client bytes end
            # its scope and cancel an idle upstream read immediately.
            disconnect = asyncio.create_task(reader.read(1))
            await asyncio.wait((forward, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if not disconnect.done():
                await forward
        except _RequestError as exc:
            await self._json(
                writer,
                exc.status,
                {"error": {"message": "The local adapter rejected the request."}},
            )
        except (OSError, asyncio.TimeoutError, httpx.HTTPError):
            pass
        finally:
            writer.close()
            for task in (forward, disconnect):
                if task is not None and not task.done():
                    task.cancel()
            cleanup = asyncio.gather(
                *(task for task in (forward, disconnect) if task is not None),
                return_exceptions=True,
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Owner shutdown may arrive during disconnect cleanup. Avoid a
                # second cancellation that interrupts upstream resource closing.
                await cleanup
                raise
            try:
                await _bounded(writer.wait_closed(), self._request_timeout)
            except (OSError, asyncio.TimeoutError):
                pass
