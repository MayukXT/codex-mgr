"""Lightweight local Anthropic protocol adapter micro-proxy.

Translates Codex's OpenAI-style requests into Anthropic Messages payloads,
mapping reasoning efforts to extended thinking budgets on loopback 127.0.0.1:4099.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import httpx

# Mapping from Codex reasoning effort tiers to Anthropic thinking token budgets
THINKING_BUDGET_MAP = {
    "low": 4096,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 65536,
    "ultra": 131072,
}


class AnthropicProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler converting OpenAI responses/chat requests to Anthropic Messages."""

    auth_token: Optional[str] = None

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress standard HTTP server access logs."""
        pass

    def do_GET(self) -> None:
        """Handle health check GET requests."""
        if self.path in ("/health", "/v1/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "proxy": "codex-mgr-anthropic-adapter"}')
            return

        if self.path.startswith("/v1/models"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            models_payload = {
                "object": "list",
                "data": [
                    {"id": "claude-5-5-sonnet-20260928", "object": "model"},
                    {"id": "claude-5-5-opus-20260922", "object": "model"},
                    {"id": "claude-5-1-fable-20260901", "object": "model"},
                    {"id": "claude-5-5-haiku-20261007", "object": "model"},
                ]
            }
            self.wfile.write(json.dumps(models_payload).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        """Forward and translate completion requests to Anthropic."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            req_data = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid JSON"}')
            return

        # 1. Translate payload
        model_id = req_data.get("model", "claude-5-5-sonnet-20260928")
        raw_messages = req_data.get("messages", [])

        system_prompt = ""
        anthropic_messages = []

        for m in raw_messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_prompt += f"{content}\n"
            else:
                anthropic_messages.append({"role": role, "content": content})

        if not anthropic_messages:
            anthropic_messages.append({"role": "user", "content": "Hello"})

        anthropic_payload: Dict[str, Any] = {
            "model": model_id,
            "max_tokens": req_data.get("max_tokens", 8192),
            "messages": anthropic_messages,
        }
        if system_prompt:
            anthropic_payload["system"] = system_prompt.strip()

        # Map thinking budget if requested
        effort = req_data.get("reasoning_effort")
        if effort and effort in THINKING_BUDGET_MAP:
            anthropic_payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": THINKING_BUDGET_MAP[effort]
            }

        # 2. Upstream request to Anthropic
        token = self.auth_token or self.headers.get("Authorization", "").replace("Bearer ", "")
        upstream_headers = {
            "x-api-key": token.strip(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }

        try:
            with httpx.Client(timeout=120.0) as client:
                up_resp = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=upstream_headers,
                    json=anthropic_payload
                )

                if up_resp.status_code != 200:
                    self.send_response(up_resp.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(up_resp.content)
                    return

                up_data = up_resp.json()

                # Extract content
                text_content = ""
                for block in up_data.get("content", []):
                    if block.get("type") == "text":
                        text_content += block.get("text", "")

                # Translate back to OpenAI Responses / Chat format
                openai_response = {
                    "id": f"chatcmpl-{up_data.get('id', 'resp')}",
                    "object": "chat.completion",
                    "created": int(up_resp.headers.get("date", 0) or 0),
                    "model": model_id,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text_content
                        },
                        "finish_reason": "stop"
                    }],
                    "usage": up_data.get("usage", {})
                }

                out_bytes = json.dumps(openai_response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out_bytes)))
                self.end_headers()
                self.wfile.write(out_bytes)

        except Exception as exc:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err_body = json.dumps({"error": f"Anthropic Gateway Error: {str(exc)}"}).encode("utf-8")
            self.wfile.write(err_body)


def is_proxy_running(port: int = 4099) -> bool:
    """Check if the local adapter proxy is already listening on the given port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_proxy_background(auth_token: Optional[str] = None, port: int = 4099) -> Tuple[bool, str]:
    """Start the Anthropic adapter proxy on a background daemon thread."""
    if is_proxy_running(port):
        return True, f"Proxy is already listening on 127.0.0.1:{port}"

    class CustomHandler(AnthropicProxyHandler):
        pass

    CustomHandler.auth_token = auth_token

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), CustomHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return True, f"Anthropic adapter listening on http://127.0.0.1:{port}/v1"
    except Exception as exc:
        return False, f"Failed to bind proxy on port {port}: {str(exc)}"
