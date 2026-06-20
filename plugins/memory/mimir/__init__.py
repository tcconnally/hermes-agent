"""Mimir memory provider plugin — thin wrapper around hermes-mimir package.

When the user sets ``memory.provider: mimir`` in config.yaml, Hermes
loads this plugin which imports the full hermes-mimir provider.

hermes-mimir provides 27 MCP tools for full persistent memory lifecycle
backed by a single Rust binary with embedded SQLite and AES-256-GCM
encryption.  All data stays local.

Install: ``pip install hermes-mimir`` (auto-installed via plugin.yaml)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register the Mimir memory provider from the hermes-mimir package."""
    try:
        from hermes_mimir import register as _mimir_register

        _mimir_register(ctx)
        logger.info("Mimir memory provider registered via hermes-mimir plugin")
    except ImportError:
        logger.debug(
            "hermes-mimir package not installed. Install with: pip install hermes-mimir"
        )
    except Exception as exc:
        logger.warning("Failed to register Mimir memory provider: %s", exc)
