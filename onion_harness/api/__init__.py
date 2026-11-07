"""Layer 5 - API access source.

The thinnest layer: it converts protocol (HTTP/SSE) to service calls and back.
MVP wiring is GUI -> local HTTP -> harness; no business logic lives here.
"""

from onion_harness.api.bus import EventBus
from onion_harness.api.server import OnionHTTPServer
from onion_harness.api.service import ApiError, OnionService
from onion_harness.api.store import AppStore, default_data_dir

__all__ = [
    "OnionService",
    "ApiError",
    "OnionHTTPServer",
    "EventBus",
    "AppStore",
    "default_data_dir",
]