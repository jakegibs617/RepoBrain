"""Idempotent Claude Code SessionStart integration for RepoBrain briefs."""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

MARKER_START = "<!-- repobrain:brief:start -->"
MARKER_END = "<!-- repobrain:brief:end -->"
HOOK_COMMAND = (
    f"{shlex.quote(sys.executable)} -m repobrain.cli brief "
    '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
)


def install_agent(root: str | Path) -> dict:
    root = Path(root).resolve()
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Refusing to modify invalid JSON in {settings_path}") from exc
    else:
        settings = {}
    hooks = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    installed = any(
        hook.get("command") == HOOK_COMMAND
        for group in hooks if isinstance(group, dict)
        for hook in group.get("hooks", []) if isinstance(hook, dict)
    )
    if not installed:
        hooks.append({"matcher": "", "hooks": [{"type": "command", "command": HOOK_COMMAND}]})
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    claude_path = root / "CLAUDE.md"
    existing = claude_path.read_text(encoding="utf-8") if claude_path.exists() else ""
    snippet = (
        f"{MARKER_START}\n"
        "## RepoBrain session context\n\n"
        "RepoBrain injects a source-grounded project brief at session start. "
        "If it reports a stale index, run `repobrain index`.\n"
        f"{MARKER_END}\n"
    )
    claude_changed = False
    if MARKER_START in existing:
        start = existing.index(MARKER_START)
        end_marker = existing.find(MARKER_END, start + len(MARKER_START))
        if end_marker >= 0:
            end = end_marker + len(MARKER_END)
        else:
            # Bound a damaged owned block at the next Markdown heading so
            # repair never consumes later human-authored sections.
            heading = re.search(r"\n#{1,6} (?!RepoBrain session context)", existing[start:])
            # With no boundary, replace only our marker and preserve every
            # unknown trailing byte; the complete repaired block is inserted
            # before that ambiguous content.
            end = start + heading.start() if heading else start + len(MARKER_START)
        replacement = existing[:start] + snippet.rstrip("\n") + existing[end:]
        if replacement != existing:
            claude_path.write_text(replacement.rstrip("\n") + "\n", encoding="utf-8")
            claude_changed = True
    else:
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        claude_path.write_text(existing + separator + snippet, encoding="utf-8")
        claude_changed = True
    return {"status": "ok", "settings": str(settings_path.relative_to(root)),
            "claude_md": str(claude_path.relative_to(root)), "changed": not installed or claude_changed}
