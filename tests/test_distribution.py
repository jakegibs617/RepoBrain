import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
import sys

import pytest
from click.testing import CliRunner

from repobrain.agent_install import (
    HOOK_COMMAND,
    MCP_SERVER_NAME,
    install_agent,
    mcp_server_entry,
    uninstall_agent,
)
from repobrain.cli import main


def test_agent_install_merges_mcp_config_and_handles_spaces(tmp_path):
    root = tmp_path / "repository with spaces"
    root.mkdir()
    (root / "CLAUDE.md").write_text("# Human context\n", encoding="utf-8")
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8")
    mcp = root / ".mcp.json"
    mcp.write_text(json.dumps({
        "projectSetting": True,
        "mcpServers": {"human-server": {"command": "human", "args": ["serve"]}},
    }), encoding="utf-8")

    first = install_agent(root)
    second = install_agent(root)

    assert first["changed"] is True
    assert second["changed"] is False
    config = json.loads(mcp.read_text(encoding="utf-8"))
    assert config["projectSetting"] is True
    assert config["mcpServers"]["human-server"] == {
        "command": "human", "args": ["serve"],
    }
    assert config["mcpServers"][MCP_SERVER_NAME] == mcp_server_entry(root)
    entry = config["mcpServers"][MCP_SERVER_NAME]
    assert entry["command"] == "uvx"
    assert entry["args"][1].startswith("repobrain[mcp]")
    assert entry["args"][-1] == str(root.resolve())
    assert entry["args"][-2] == "--path"
    assert len(entry["args"]) == 6

    removed = uninstall_agent(root)
    assert removed["changed"] is True
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "permissions": {"allow": ["Read"]},
    }
    assert json.loads(mcp.read_text(encoding="utf-8")) == {
        "projectSetting": True,
        "mcpServers": {"human-server": {"command": "human", "args": ["serve"]}},
    }
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "# Human context\n"


@pytest.mark.parametrize("bad_config", [
    "{not json",
    json.dumps({"mcpServers": []}),
    json.dumps({"mcpServers": {MCP_SERVER_NAME: None}}),
    json.dumps({"mcpServers": {MCP_SERVER_NAME: {"command": "custom"}}}),
])
def test_agent_install_conflicts_fail_before_any_write(tmp_path, bad_config):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original_settings = json.dumps({"permissions": {"allow": ["Read"]}})
    settings.write_text(original_settings, encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(bad_config, encoding="utf-8")

    with pytest.raises(ValueError, match="Refusing"):
        install_agent(tmp_path)

    assert settings.read_text(encoding="utf-8") == original_settings
    assert not (tmp_path / "CLAUDE.md").exists()


def test_agent_install_rejects_modified_copy_of_owned_session_hook(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = json.dumps({
        "hooks": {"SessionStart": [{
            "matcher": "", "hooks": [{
                "type": "command", "command": HOOK_COMMAND, "timeout": 30,
            }],
        }]},
    })
    settings.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="conflicting RepoBrain SessionStart"):
        install_agent(tmp_path)

    assert settings.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


@pytest.mark.parametrize("settings_value", [
    {"hooks": None},
    {"hooks": {"SessionStart": None}},
])
def test_agent_install_rejects_explicit_null_settings_containers(tmp_path, settings_value):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = json.dumps(settings_value)
    settings.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="must be"):
        install_agent(tmp_path)

    assert settings.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".mcp.json").exists()


def test_agent_install_migrates_legacy_interpreter_hook_without_duplication(tmp_path):
    legacy = (
        f"{sys.executable} -m repobrain.cli brief "
        '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
    )
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": legacy}]},
        ]},
    }), encoding="utf-8")

    first = install_agent(tmp_path)
    second = install_agent(tmp_path)

    assert first["changed"] is True
    assert second["changed"] is False
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [hook["command"] for group in data["hooks"]["SessionStart"]
                for hook in group["hooks"]]
    assert commands == [HOOK_COMMAND]

    uninstall_agent(tmp_path)
    assert json.loads(settings.read_text(encoding="utf-8")) == {}


def test_agent_install_preserves_user_selected_package_requirement(tmp_path):
    old_hook = (
        'uvx --from "repobrain==0.0.9" repobrain brief '
        '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
    )
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": old_hook}]},
        ]},
    }), encoding="utf-8")
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({
        "mcpServers": {MCP_SERVER_NAME: {
            "command": "uvx",
            "args": [
                "--from", "repobrain[mcp]==0.0.9", "repobrain", "mcp",
                "--path", str(tmp_path.resolve()),
            ],
        }},
    }), encoding="utf-8")

    original_settings = settings.read_text(encoding="utf-8")
    original_mcp = mcp.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="conflicting"):
        install_agent(tmp_path)

    assert settings.read_text(encoding="utf-8") == original_settings
    assert mcp.read_text(encoding="utf-8") == original_mcp


def test_agent_install_rejects_different_version_hook_without_mcp_config(tmp_path):
    custom = (
        'uvx --from "repobrain==0.0.9" repobrain brief '
        '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
    )
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = json.dumps({
        "hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": custom}]},
        ]},
    })
    settings.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="conflicting RepoBrain SessionStart"):
        install_agent(tmp_path)

    assert settings.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".mcp.json").exists()


def test_agent_install_preserves_empty_groups_and_similar_user_commands(tmp_path):
    similar = (
        "env FLAG=1 python -m repobrain.cli brief "
        '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
    )
    empty_group = {"matcher": "human-only", "hooks": []}
    omitted_group = {"matcher": "human-omitted"}
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            empty_group, omitted_group,
            {"matcher": "custom", "hooks": [{"type": "command", "command": similar}]},
        ]},
    }), encoding="utf-8")

    install_agent(tmp_path)
    installed = json.loads(settings.read_text(encoding="utf-8"))
    assert empty_group in installed["hooks"]["SessionStart"]
    assert omitted_group in installed["hooks"]["SessionStart"]
    assert any(
        hook.get("command") == similar
        for group in installed["hooks"]["SessionStart"]
        for hook in group.get("hooks", [])
    )

    uninstall_agent(tmp_path)
    removed = json.loads(settings.read_text(encoding="utf-8"))
    assert empty_group in removed["hooks"]["SessionStart"]
    assert omitted_group in removed["hooks"]["SessionStart"]
    assert removed["hooks"]["SessionStart"][2]["hooks"][0]["command"] == similar


def test_agent_install_preserves_relative_python_hook_that_was_not_generated(tmp_path):
    custom = (
        "python3 -m repobrain.cli brief "
        '--path "$CLAUDE_PROJECT_DIR" --budget 2000'
    )
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"matcher": "custom", "hooks": [{"type": "command", "command": custom}]},
        ]},
    }), encoding="utf-8")

    install_agent(tmp_path)
    uninstall_agent(tmp_path)

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == custom


def test_uninstall_preserves_user_modified_repobrain_server(tmp_path):
    install_agent(tmp_path)
    mcp = tmp_path / ".mcp.json"
    config = json.loads(mcp.read_text(encoding="utf-8"))
    config["mcpServers"][MCP_SERVER_NAME]["env"] = {"USER_SETTING": "keep"}
    mcp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    uninstall_agent(tmp_path)

    preserved = json.loads(mcp.read_text(encoding="utf-8"))
    assert preserved["mcpServers"][MCP_SERVER_NAME]["env"] == {
        "USER_SETTING": "keep",
    }


def test_cli_combines_mcp_claude_and_optional_git_hooks(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    result = CliRunner().invoke(main, ["install-agent", str(tmp_path), "--git-hooks"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["git_hooks"]["installed"] is True
    assert payload["mcp_config"] == ".mcp.json"
    assert (tmp_path / ".mcp.json").exists()
    assert (tmp_path / "CLAUDE.md").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    commands = [hook["command"] for group in settings["hooks"]["SessionStart"]
                for hook in group["hooks"]]
    assert commands == [HOOK_COMMAND]


def test_wheel_and_sdist_contain_runtime_and_console_entry(tmp_path):
    root = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from hatchling.build import build_wheel, build_sdist; "
            "import sys; build_wheel(sys.argv[1]); build_sdist(sys.argv[1])",
            str(dist),
        ],
        cwd=root, check=True, capture_output=True, text=True,
    )
    wheel = next(dist.glob("repobrain-*.whl"))
    sdist = next(dist.glob("repobrain-*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "repobrain/cli.py" in names
        assert "repobrain/mcp_server.py" in names
        assert not any("__pycache__" in name for name in names)
        entry_points = archive.read(next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )).decode()
        metadata = archive.read(next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )).decode()
    assert "repobrain = repobrain.cli:main" in entry_points
    assert "Provides-Extra: mcp" in metadata
    assert "mcp" in metadata

    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
    assert any(name.endswith("/pyproject.toml") for name in names)
    assert any(name.endswith("/README.md") for name in names)
    assert any(name.endswith("/repobrain/cli.py") for name in names)
    assert any(name.endswith("/repobrain/mcp_server.py") for name in names)
    assert not any("__pycache__" in name for name in names)
