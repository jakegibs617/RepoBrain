"""Idempotent Claude Code SessionStart integration for RepoBrain briefs."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path

MARKER_START = "<!-- repobrain:brief:start -->"
MARKER_END = "<!-- repobrain:brief:end -->"
PACKAGE_NAME = "repobrain"
MCP_SERVER_NAME = "repobrain"
MCP_COMMAND = "uvx"


def _installed_requirement(*, mcp: bool = False) -> str:
    project = f"{PACKAGE_NAME}[mcp]" if mcp else PACKAGE_NAME
    try:
        installed = distribution(PACKAGE_NAME)
    except PackageNotFoundError:
        return project
    direct_url = installed.read_text("direct_url.json")
    if direct_url:
        try:
            data = json.loads(direct_url)
        except json.JSONDecodeError:
            data = None
        url = data.get("url") if isinstance(data, dict) else None
        vcs_info = data.get("vcs_info") if isinstance(data, dict) else None
        if url and isinstance(vcs_info, dict) and vcs_info.get("vcs"):
            # PEP 610 stores VCS direct URLs without the scheme prefix (e.g.
            # "https://..." not "git+https://..."); PEP 508 requires the
            # prefix, so uv/uvx can't parse the bare form as a direct
            # reference. Reconstruct it, and pin to the resolved commit.
            vcs = vcs_info["vcs"]
            if not url.startswith(f"{vcs}+"):
                url = f"{vcs}+{url}"
            commit_id = vcs_info.get("commit_id")
            if commit_id:
                url = f"{url}@{commit_id}"
        if url:
            return f"{project} @ {url}"
    return f"{project}=={installed.version}"


PACKAGE_REQUIREMENT = _installed_requirement()
MCP_PACKAGE = _installed_requirement(mcp=True)
HOOK_COMMAND = (
    f"uvx --from {shlex.quote(PACKAGE_REQUIREMENT)} repobrain brief "
    '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
)
LEGACY_HOOK_SUFFIX = (
    ' -m repobrain.cli brief --path "$CLAUDE_PROJECT_DIR" --budget 2000'
)
MCP_CONFIG = ".mcp.json"
GIT_MARKER_START = "# repobrain:index:start"
GIT_MARKER_END = "# repobrain:index:end"
GIT_RUNNER = "repobrain-index"
GITIGNORE_ENTRY = ".repobrain/"
SKILL_MARKER = "<!-- repobrain:skill:owned -->"
SKILL_DIRNAME = Path(".claude") / "skills" / "repobrain"
SKILL_FILES = ("SKILL.md", "reference.md")


def shipped_skill_file(name: str) -> str:
    """Read a packaged skill file from the wheel rather than the source tree."""
    return files("repobrain").joinpath("agent_skill", name).read_text(encoding="utf-8")


def _prepare_skill(root: Path) -> tuple[dict[Path, str], dict]:
    """Return the skill files to write plus a JSON-ready status.

    Ownership is carried by a marker inside each file. Removing the marker is
    how a user adopts the skill: RepoBrain then leaves the whole directory
    alone rather than pushing upgrades over their edits. An adopted skill is
    deliberately not a conflict — unlike an MCP entry, a customized skill still
    works, so it must not fail the rest of the installation.
    """
    skill_dir = root / SKILL_DIRNAME
    status: dict = {"path": str(SKILL_DIRNAME), "installed": True, "changed": False}
    existing = {}
    for name in SKILL_FILES:
        path = skill_dir / name
        if not path.exists():
            continue
        existing[name] = path.read_text(encoding="utf-8")
        if SKILL_MARKER not in existing[name]:
            return {}, {**status, "installed": False, "reason": "user_owned"}
    writes = {
        skill_dir / name: shipped
        for name in SKILL_FILES
        if (shipped := shipped_skill_file(name)) != existing.get(name)
    }
    return writes, {**status, "changed": bool(writes)}


def mcp_server_entry(root: str | Path) -> dict:
    """Return the exact, cross-platform MCP entry owned by RepoBrain."""
    root = Path(root).resolve()
    return {
        "command": MCP_COMMAND,
        "args": ["--from", MCP_PACKAGE, "repobrain", "mcp", "--path", str(root)],
    }


def _is_owned_mcp_entry(entry: object, root: Path) -> bool:
    return entry == mcp_server_entry(root)


def _is_repobrain_uvx_hook_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return (
        len(tokens) == 9
        and tokens[:2] == ["uvx", "--from"]
        and (tokens[2] == PACKAGE_NAME
             or tokens[2].startswith(f"{PACKAGE_NAME}==")
             or tokens[2].startswith(f"{PACKAGE_NAME} @ "))
        and tokens[3:] == [
            "repobrain", "brief", "--path", "$CLAUDE_PROJECT_DIR", "--budget", "2000",
        ]
    )


def _read_json_object(path: Path) -> tuple[dict, bool]:
    if not path.exists():
        return {}, False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Refusing to modify invalid JSON in {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Refusing to modify {path}: top-level JSON must be an object")
    return value, True


def _is_owned_hook_command(command: object) -> bool:
    """Recognize the current hook and the exact pre-distribution command form."""
    if not isinstance(command, str):
        return False
    if command == HOOK_COMMAND:
        return True
    if not command.endswith(LEGACY_HOOK_SUFFIX):
        return False
    prefix = command.removesuffix(LEGACY_HOOK_SUFFIX)
    try:
        tokens = shlex.split(prefix)
    except ValueError:
        return False
    if len(tokens) != 1:
        return False
    executable = Path(tokens[0])
    return executable.is_absolute() and re.fullmatch(
        r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?",
        executable.name, flags=re.IGNORECASE,
    ) is not None


def _is_owned_hook(hook: object) -> bool:
    return isinstance(hook, dict) and hook == {
        "type": "command", "command": hook.get("command"),
    } and _is_owned_hook_command(hook.get("command"))


def _prepare_settings(path: Path) -> tuple[dict, bool, bool]:
    settings, existed = _read_json_object(path)
    settings = dict(settings)
    hooks_present = "hooks" in settings
    raw_hooks = settings.get("hooks")
    if not hooks_present:
        hooks = {}
    elif isinstance(raw_hooks, dict):
        hooks = dict(raw_hooks)
    else:
        raise ValueError(f"Refusing to modify {path}: hooks must be an object")
    sessions_present = "SessionStart" in hooks
    raw_sessions = hooks.get("SessionStart")
    if not sessions_present:
        sessions = []
    elif isinstance(raw_sessions, list):
        sessions = list(raw_sessions)
    else:
        raise ValueError(f"Refusing to modify {path}: hooks.SessionStart must be an array")

    owned = {"type": "command", "command": HOOK_COMMAND}
    installed = False
    changed = False
    normalized_sessions = []
    for group in sessions:
        if not isinstance(group, dict):
            normalized_sessions.append(group)
            continue
        group = dict(group)
        group_hooks = group.get("hooks", [])
        if not isinstance(group_hooks, list):
            raise ValueError(
                f"Refusing to modify {path}: each SessionStart hooks value must be an array"
            )
        normalized_hooks = []
        for hook in group_hooks:
            command = hook.get("command") if isinstance(hook, dict) else None
            if _is_repobrain_uvx_hook_command(command) and command != HOOK_COMMAND:
                raise ValueError(
                    f"Refusing to modify {path}: conflicting RepoBrain SessionStart hook"
                )
            if (isinstance(hook, dict)
                    and _is_owned_hook_command(command)
                    and hook != {"type": "command", "command": command}):
                raise ValueError(
                    f"Refusing to modify {path}: conflicting RepoBrain SessionStart hook"
                )
            if _is_owned_hook(hook):
                if not installed:
                    normalized_hooks.append(owned)
                    installed = True
                    changed = changed or hook != owned
                else:
                    changed = True
                continue
            normalized_hooks.append(hook)
        if normalized_hooks:
            group["hooks"] = normalized_hooks
            normalized_sessions.append(group)
        elif not group_hooks:
            normalized_sessions.append(group)
    if not installed:
        normalized_sessions.append({"matcher": "", "hooks": [owned]})
        changed = True
    if changed:
        hooks["SessionStart"] = normalized_sessions
        settings["hooks"] = hooks
    return settings, existed, changed


def _prepare_mcp(path: Path, root: Path) -> tuple[dict, bool, bool]:
    config, existed = _read_json_object(path)
    config = dict(config)
    servers_present = "mcpServers" in config
    raw_servers = config.get("mcpServers")
    if not servers_present:
        servers = {}
    elif isinstance(raw_servers, dict):
        servers = dict(raw_servers)
    else:
        raise ValueError(f"Refusing to modify {path}: mcpServers must be an object")
    expected = mcp_server_entry(root)
    present = MCP_SERVER_NAME in servers
    current = servers.get(MCP_SERVER_NAME)
    if present and current != expected:
        raise ValueError(
            f"Refusing to overwrite conflicting mcpServers.{MCP_SERVER_NAME} in {path}"
        )
    changed = not present
    if changed:
        servers[MCP_SERVER_NAME] = expected
        config["mcpServers"] = servers
    return config, existed, changed


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _prepare_gitignore(path: Path) -> tuple[str, bool]:
    """Return content that safely excludes RepoBrain's local database.

    The line is deliberately not marker-owned: uninstall leaves the safety
    exclusion in place so an existing database cannot suddenly become
    committable.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(line.strip() == GITIGNORE_ENTRY for line in existing.splitlines()):
        return existing, False
    separator = "" if not existing or existing.endswith("\n") else "\n"
    return f"{existing}{separator}{GITIGNORE_ENTRY}\n", True


def install_agent(root: str | Path, *, git_hooks: bool = False) -> dict:
    root = Path(root).resolve()
    settings_path = root / ".claude" / "settings.json"
    mcp_path = root / MCP_CONFIG
    gitignore_path = root / ".gitignore"
    settings, _, settings_changed = _prepare_settings(settings_path)
    mcp_config, _, mcp_changed = _prepare_mcp(mcp_path, root)
    gitignore_content, gitignore_changed = _prepare_gitignore(gitignore_path)
    skill_writes, skill_result = _prepare_skill(root)
    # Validate optional Git integration before any configuration is changed.
    if git_hooks:
        _git_hooks_dir(root)

    claude_path = root / "CLAUDE.md"
    existing = claude_path.read_text(encoding="utf-8") if claude_path.exists() else ""
    snippet = (
        f"{MARKER_START}\n"
        "## RepoBrain session context\n\n"
        "RepoBrain injects a source-grounded project brief at session start. "
        "If it reports a stale index, run `repobrain index`.\n\n"
        "The `repobrain` skill in `.claude/skills/` covers how to query the index; "
        "use it for questions about this codebase.\n"
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
            claude_changed = True
    else:
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        claude_changed = True
    if settings_changed:
        _write_json(settings_path, settings)
    if mcp_changed:
        _write_json(mcp_path, mcp_config)
    if gitignore_changed:
        gitignore_path.write_text(gitignore_content, encoding="utf-8")
    for skill_file, content in skill_writes.items():
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content, encoding="utf-8")
    if claude_changed:
        if MARKER_START in existing:
            claude_path.write_text(replacement.rstrip("\n") + "\n", encoding="utf-8")
        else:
            claude_path.write_text(existing + separator + snippet, encoding="utf-8")
    git_result = _install_git_hooks(root) if git_hooks else {"installed": False, "changed": False}
    return {"status": "ok", "settings": str(settings_path.relative_to(root)),
            "claude_md": str(claude_path.relative_to(root)),
            "mcp_config": str(mcp_path.relative_to(root)),
            "gitignore": {
                "path": str(gitignore_path.relative_to(root)),
                "installed": True,
                "changed": gitignore_changed,
            },
            "skill": skill_result,
            "git_hooks": git_result,
            "changed": (
                settings_changed or mcp_changed or gitignore_changed
                or claude_changed or skill_result["changed"] or git_result["changed"]
            )}


def uninstall_agent(root: str | Path) -> dict:
    """Remove only RepoBrain-owned settings, Markdown blocks, and Git hook blocks."""
    root = Path(root).resolve()
    changed = False
    settings_path = root / ".claude" / "settings.json"
    mcp_path = root / MCP_CONFIG
    settings, settings_exists = _read_json_object(settings_path)
    mcp_config, mcp_exists = _read_json_object(mcp_path)
    hooks_present = settings_exists and "hooks" in settings
    raw_hooks = settings.get("hooks") if hooks_present else None
    if hooks_present and not isinstance(raw_hooks, dict):
        raise ValueError(f"Refusing to modify {settings_path}: hooks must be an object")
    sessions_present = isinstance(raw_hooks, dict) and "SessionStart" in raw_hooks
    raw_sessions = (
        raw_hooks.get("SessionStart") if isinstance(raw_hooks, dict) and sessions_present
        else None
    )
    if sessions_present and not isinstance(raw_sessions, list):
        raise ValueError(
            f"Refusing to modify {settings_path}: hooks.SessionStart must be an array"
        )
    servers_present = mcp_exists and "mcpServers" in mcp_config
    raw_servers = mcp_config.get("mcpServers") if servers_present else None
    if servers_present and not isinstance(raw_servers, dict):
        raise ValueError(f"Refusing to modify {mcp_path}: mcpServers must be an object")

    settings_changed = False
    if settings_exists:
        settings = dict(settings)
        sessions = raw_sessions or []
        filtered = []
        for group in sessions:
            if not isinstance(group, dict):
                filtered.append(group)
                continue
            group = dict(group)
            before = group.get("hooks", [])
            if not isinstance(before, list):
                raise ValueError(
                    f"Refusing to modify {settings_path}: each SessionStart hooks value "
                    "must be an array"
                )
            after = [hook for hook in before if not _is_owned_hook(hook)]
            if after:
                group["hooks"] = after
                filtered.append(group)
            elif not before:
                filtered.append(group)
            settings_changed = settings_changed or after != before
        if "hooks" in settings and "SessionStart" in settings["hooks"]:
            if filtered:
                settings["hooks"]["SessionStart"] = filtered
            else:
                settings["hooks"].pop("SessionStart", None)
            if not settings["hooks"]:
                settings.pop("hooks", None)
        if settings_changed:
            _write_json(settings_path, settings)

    mcp_changed = False
    if mcp_exists:
        servers = dict(raw_servers or {})
        if _is_owned_mcp_entry(servers.get(MCP_SERVER_NAME), root):
            servers.pop(MCP_SERVER_NAME)
            mcp_config = dict(mcp_config)
            if servers:
                mcp_config["mcpServers"] = servers
            else:
                mcp_config.pop("mcpServers", None)
            _write_json(mcp_path, mcp_config)
            mcp_changed = True
    changed = settings_changed

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

    skill_result = _uninstall_skill(root)
    git_result = _uninstall_git_hooks(root)
    return {"status": "ok", "settings": str(settings_path.relative_to(root)),
            "claude_md": str(claude_path.relative_to(root)),
            "mcp_config": str(mcp_path.relative_to(root)), "skill": skill_result,
            "git_hooks": git_result,
            "changed": (changed or mcp_changed or skill_result["changed"]
                        or git_result["changed"])}


def _uninstall_skill(root: Path) -> dict:
    """Delete only marker-bearing skill files, then prune directories we emptied."""
    skill_dir = root / SKILL_DIRNAME
    changed = False
    for name in SKILL_FILES:
        path = skill_dir / name
        if not path.exists():
            continue
        if SKILL_MARKER not in path.read_text(encoding="utf-8"):
            continue
        path.unlink()
        changed = True
    for directory in (skill_dir, skill_dir.parent):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return {"path": str(SKILL_DIRNAME), "installed": False, "changed": changed}


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
        f"uvx --from {shlex.quote(PACKAGE_REQUIREMENT)} repobrain index \"$root\"\n"
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
