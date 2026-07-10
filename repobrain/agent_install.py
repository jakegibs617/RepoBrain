"""Idempotent Claude Code SessionStart integration for RepoBrain briefs."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

MARKER_START = "<!-- repobrain:brief:start -->"
MARKER_END = "<!-- repobrain:brief:end -->"
HOOK_COMMAND = (
    f"{shlex.quote(sys.executable)} -m repobrain.cli brief "
    '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
)
GIT_MARKER_START = "# repobrain:index:start"
GIT_MARKER_END = "# repobrain:index:end"
GIT_RUNNER = "repobrain-index"


def install_agent(root: str | Path, *, git_hooks: bool = False) -> dict:
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
    git_result = _install_git_hooks(root) if git_hooks else {"installed": False, "changed": False}
    return {"status": "ok", "settings": str(settings_path.relative_to(root)),
            "claude_md": str(claude_path.relative_to(root)),
            "git_hooks": git_result, "changed": not installed or claude_changed or git_result["changed"]}


def uninstall_agent(root: str | Path) -> dict:
    """Remove only RepoBrain-owned settings, Markdown blocks, and Git hook blocks."""
    root = Path(root).resolve()
    changed = False
    settings_path = root / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Refusing to modify invalid JSON in {settings_path}") from exc
        sessions = settings.get("hooks", {}).get("SessionStart", [])
        filtered = []
        for group in sessions:
            if not isinstance(group, dict):
                filtered.append(group)
                continue
            group = dict(group)
            before = group.get("hooks", [])
            after = [hook for hook in before
                     if not (isinstance(hook, dict) and hook.get("command") == HOOK_COMMAND)]
            if after:
                group["hooks"] = after
                filtered.append(group)
            changed = changed or after != before
        if "hooks" in settings and "SessionStart" in settings["hooks"]:
            if filtered:
                settings["hooks"]["SessionStart"] = filtered
            else:
                settings["hooks"].pop("SessionStart", None)
            if not settings["hooks"]:
                settings.pop("hooks", None)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    claude_path = root / "CLAUDE.md"
    if claude_path.exists():
        existing = claude_path.read_text(encoding="utf-8")
        cleaned, count = re.subn(
            re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END) + r"\n?",
            "", existing, flags=re.DOTALL,
        )
        if count:
            claude_path.write_text(cleaned.rstrip("\n") + ("\n" if cleaned.strip() else ""),
                                   encoding="utf-8")
            changed = True

    git_result = _uninstall_git_hooks(root)
    return {"status": "ok", "settings": str(settings_path.relative_to(root)),
            "claude_md": str(claude_path.relative_to(root)), "git_hooks": git_result,
            "changed": changed or git_result["changed"]}


def _git_hooks_dir(root: Path) -> Path:
    process = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"], cwd=root,
        capture_output=True, text=True, timeout=5,
    )
    if process.returncode != 0:
        raise ValueError(f"{root} is not a Git repository")
    path = Path(process.stdout.strip())
    return path if path.is_absolute() else (root / path).resolve()


def _install_git_hooks(root: Path) -> dict:
    hooks_dir = _git_hooks_dir(root)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    runner = hooks_dir / GIT_RUNNER
    runner_content = (
        "#!/bin/sh\n"
        "root=$(git rev-parse --show-toplevel) || exit 0\n"
        f"{shlex.quote(sys.executable)} -m repobrain.cli index \"$root\"\n"
    )
    changed = not runner.exists() or runner.read_text(encoding="utf-8") != runner_content
    if changed:
        runner.write_text(runner_content, encoding="utf-8")
    runner.chmod(0o755)

    block = (
        f"{GIT_MARKER_START}\n"
        "\"$(git rev-parse --git-path hooks)/repobrain-index\" "
        "|| echo 'RepoBrain: automatic index failed' >&2\n"
        f"{GIT_MARKER_END}\n"
    )
    dispatchers = []
    for name in ("post-commit", "post-merge"):
        path = hooks_dir / name
        existing = path.read_text(encoding="utf-8") if path.exists() else "#!/bin/sh\n"
        if GIT_MARKER_START not in existing:
            separator = "" if existing.endswith("\n") else "\n"
            path.write_text(existing + separator + block, encoding="utf-8")
            changed = True
        path.chmod(path.stat().st_mode | 0o111)
        dispatchers.append(str(path))
    return {"installed": True, "changed": changed, "runner": str(runner),
            "dispatchers": dispatchers}


def _uninstall_git_hooks(root: Path) -> dict:
    try:
        hooks_dir = _git_hooks_dir(root)
    except ValueError:
        return {"installed": False, "changed": False}
    changed = False
    runner = hooks_dir / GIT_RUNNER
    if runner.exists():
        runner.unlink()
        changed = True
    pattern = re.compile(
        re.escape(GIT_MARKER_START) + r".*?" + re.escape(GIT_MARKER_END) + r"\n?",
        re.DOTALL,
    )
    for name in ("post-commit", "post-merge"):
        path = hooks_dir / name
        if not path.exists():
            continue
        existing = path.read_text(encoding="utf-8")
        cleaned, count = pattern.subn("", existing)
        if not count:
            continue
        if cleaned.strip() == "#!/bin/sh":
            path.unlink()
        else:
            path.write_text(cleaned.rstrip("\n") + "\n", encoding="utf-8")
        changed = True
    return {"installed": False, "changed": changed}
