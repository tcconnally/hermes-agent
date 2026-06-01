"""Perseus context engine — live context injection + delegated compaction.

Replaces the default compressor with a Perseus-aware engine that:

1. Injects live Perseus context at session start (health checks, @query
   directives, workspace status) instead of relying on cron-rendered files.
2. Delegates conversation compaction to the proven ContextCompressor
   (inheritance — no compaction logic is rewritten).
3. Preserves Perseus-injected context as protected head content so it
   survives compaction.
4. Exposes ``perseus_grep`` and ``perseus_status`` tools to the agent.

Selection: set ``context.engine: perseus`` in config.yaml.
Falls back gracefully when Perseus is not installed.
"""

from __future__ import annotations

import logging
import os
import subprocess as _subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.context_compressor import ContextCompressor

logger = logging.getLogger(__name__)


class PerseusContextEngine(ContextCompressor):
    """Context engine that injects live Perseus context and delegates compaction.

    On session start, renders Perseus directives live (health checks,
    @query directives, workspace status) and injects them into the system
    prompt as session context.  Compaction is delegated to the proven
    ContextCompressor (inherited) so the agent benefits from all the
    battle-tested compaction logic while getting live Perseus context.

    Falls back gracefully: if the Perseus plugin directory is not found,
    the engine still works — it just won't inject Perseus context.
    """

    # Matching the upstream ContextCompressor defaults, but with a slightly
    # lower threshold since Perseus context adds overhead.
    threshold_percent: float = 0.45

    @property
    def name(self) -> str:
        return "perseus"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._perseus_context: str = ""
        self._perseus_path: Optional[Path] = None
        self._resolve_perseus_path()

    # ── Perseus path resolution ──────────────────────────────────────

    def _resolve_perseus_path(self) -> None:
        """Find the Perseus plugin directory."""
        candidates = []
        hermes_home = os.getenv("HERMES_HOME", "")
        if hermes_home:
            candidates.append(Path(hermes_home) / "plugins" / "perseus")
        candidates.append(Path.home() / ".hermes" / "plugins" / "perseus")
        active_profile = os.getenv("HERMES_PROFILE", "")
        if active_profile:
            candidates.append(
                Path.home() / ".hermes" / "profiles" / active_profile / "plugins" / "perseus"
            )
        for p in candidates:
            if (p / "perseus.py").exists():
                self._perseus_path = p.resolve()
                logger.debug("PerseusContextEngine: found perseus at %s", self._perseus_path)
                return
        logger.debug("PerseusContextEngine: perseus not found, injection disabled")

    # ── Session lifecycle ────────────────────────────────────────────

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Load Perseus context at session start."""
        super().on_session_start(session_id, **kwargs)
        self._load_perseus_context()

    def on_session_reset(self) -> None:
        """Reset per-session state including Perseus context."""
        super().on_session_reset()
        self._perseus_context = ""

    # ── Context loading ──────────────────────────────────────────────

    def _load_perseus_context(self) -> None:
        """Render Perseus context from directives and cached context.md."""
        if not self._perseus_path:
            return
        parts: List[str] = []

        # 1. Load pre-rendered context.md (from cron or manual render)
        context_md = self._perseus_path / ".perseus" / "context.md"
        if context_md.exists():
            try:
                content = context_md.read_text(encoding="utf-8")
                if content.strip():
                    parts.append(content.strip())
                    logger.debug("PerseusContextEngine: loaded context.md (%d chars)", len(content))
            except Exception:
                logger.debug("PerseusContextEngine: failed to read context.md", exc_info=True)

        # 2. Optionally run live health checks (perseus doctor --json)
        perseus_py = self._perseus_path / "perseus.py"
        if perseus_py.exists() and self._allow_live_rendering():
            try:
                python = os.getenv("HERMES_PYTHON", "python3")
                env = os.environ.copy()
                env["PERSEUS_ALLOW_DANGEROUS"] = "1"
                result = _subprocess.run(
                    [python, str(perseus_py), "doctor", "--json"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(self._perseus_path), env=env,
                )
                if result.returncode == 0 and result.stdout.strip():
                    parts.insert(0, "## Perseus Live Context\n\n" + result.stdout.strip())
                    logger.debug("PerseusContextEngine: doctor ran successfully")
            except Exception:
                logger.debug("PerseusContextEngine: doctor failed (non-fatal)", exc_info=True)

        self._perseus_context = "\n\n".join(parts)
        if self._perseus_context:
            logger.info("PerseusContextEngine: context loaded (%d chars)", len(self._perseus_context))

    def _allow_live_rendering(self) -> bool:
        """Check if Perseus config permits live shell rendering."""
        config_yaml = self._perseus_path / ".perseus" / "config.yaml"
        if not config_yaml.exists():
            return True
        try:
            import yaml
            with open(config_yaml) as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("allow_query_shell", True)
        except Exception:
            return True

    # ── Context injection hook ───────────────────────────────────────

    def get_session_context(self) -> str:
        """Return Perseus context to inject into the system prompt."""
        return self._perseus_context

    # ── Tools exposed to the agent ───────────────────────────────────

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Expose Perseus context tools to the agent."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "perseus_grep",
                    "description": (
                        "Search the Perseus context (health status, workspace state, "
                        "directive output) for a keyword or pattern."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search term."}
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "perseus_status",
                    "description": "Get current Perseus context engine status.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Handle Perseus context tool calls."""
        import json as _json

        if name == "perseus_grep":
            query = (args.get("query") or "").lower()
            if not query or not self._perseus_context:
                return _json.dumps({"matches": [], "total": 0})
            lines = self._perseus_context.split("\n")
            matches = [
                {"line": i + 1, "content": line[:300]}
                for i, line in enumerate(lines) if query in line.lower()
            ]
            return _json.dumps({
                "matches": matches[:30], "total": len(matches),
                "truncated": len(matches) > 30,
            })

        if name == "perseus_status":
            return _json.dumps({
                "engine": "perseus",
                "context_loaded": bool(self._perseus_context),
                "context_size_chars": len(self._perseus_context),
                "perseus_path": str(self._perseus_path) if self._perseus_path else None,
                "compressor_ready": True,
                "model": getattr(self, "model", "unknown"),
                "compression_count": self.compression_count,
            })

        return _json.dumps({"error": f"Unknown perseus engine tool: {name}"})

    # ── Availability check ───────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """Check if Perseus plugin is installed."""
        candidates = []
        hermes_home = os.getenv("HERMES_HOME", "")
        if hermes_home:
            candidates.append(Path(hermes_home) / "plugins" / "perseus")
        candidates.append(Path.home() / ".hermes" / "plugins" / "perseus")
        return any((p / "perseus.py").exists() for p in candidates)
