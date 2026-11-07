"""Shared test configuration.

Tests must never touch the developer's real Windows credential manager: doing so
would overwrite their actual provider keys with throwaway test values. The memory
backend keeps everything inside the process, keyed per data directory, so each
test file also stays isolated from the others.
"""

import os

os.environ.setdefault("ONION_SECRET_STORE", "memory")