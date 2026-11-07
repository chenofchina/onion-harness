"""Layer 3 - the harness framework.

Public surface: ``OnionRuntime``.  Everything about the underlying engine is
confined to :mod:`onion_harness.harness.bridge` and the adapters.
"""

from onion_harness.harness.runtime import (
    ModelConfig,
    OnionRuntime,
    TaskSession,
    build_model_config,
)

__all__ = ["OnionRuntime", "ModelConfig", "TaskSession", "build_model_config"]