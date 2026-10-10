"""Pure protocol codecs; runtime activation requires separate workflow validation."""

from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.responses import adapt_response
from codex_mgr.core.adapters.streaming import adapt_stream
from codex_mgr.core.adapters.tools import ProtocolError

__all__ = ["ProtocolError", "adapt_request", "adapt_response", "adapt_stream"]
