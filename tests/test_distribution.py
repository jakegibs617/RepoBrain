import json
import shlex
import subprocess
import tarfile
import zipfile
from pathlib import Path
import sys

import pytest
from click.testing import CliRunner

from repobrain import agent_install
from repobrain.agent_install import (
    HOOK_COMMAND,
    MCP_SERVER_NAME,
    install_agent,
    mcp_server_entry,
    uninstall_agent,
)
from repobrain.cli import main


class _FakeDistribution:
    def __init__(self, version, direct_url_json):
        self.version = version
        self._direct_url_json = direct_url_json

    def read_text(self, filename):
        if filename == "direct_url.json":
            return self._direct_url_json
        return None


def test_installed_requirement_reconstructs_git_vcs_url_with_scheme_and_commit(monkeypatch):
    # Mirrors the direct_url.json pip/uv write for `uv tool install --from
    # git+https://github.com/jakegibs617/RepoBrain repobrain`: PEP 610 stores
    # the bare https URL plus vcs_info, without the `git+` scheme prefix.
    direct_url_json = json.dumps({
        "url": "https://github.com/jakegibs617/RepoBrain",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "199bbdc26cff3b391e402ae632db785ab823cb8c",
        },
    })
    fake = _FakeDistribution("0.1.0", direct_url_json)
    monkeypatch.setattr(agent_install, "distribution", lambda name: fake)

    result = agent_install._installed_requirement(mcp=True)

    assert result == (
        "repobrain[mcp] @ git+https://github.com/jakegibs617/RepoBrain"
        "@199bbdc26cff3b391e402ae632db785ab823cb8c"
    )


def test_editable_directory_install_launches_from_source_not_a_cached_build(
    monkeypatch, tmp_path
):
    # `uv pip install -e .` writes dir_info.editable. The bare
    # "repobrain @ file:///path" requirement carries no version, commit or
    # content hash, so uv resolves it to a wheel it built once and never
    # rebuilds -- neither `uvx --refresh` nor `--refresh-package` invalidates
    # it. The launcher has to declare the source editable instead.
    source = tmp_path / "checkout"
    source.mkdir()
    direct_url_json = json.dumps({
        "url": source.as_uri(),
        "dir_info": {"editable": True},
    })
    fake = _FakeDistribution("0.1.0", direct_url_json)
    monkeypatch.setattr(agent_install, "distribution", lambda name: fake)

    assert agent_install.editable_source_path() == source


def test_registry_install_is_not_treated_as_editable(monkeypatch):
    # A registry install is already immutable: _installed_requirement pins the
    # version, so uv's cached build for it stays correct forever.
    fake = _FakeDistribution("0.1.0", None)
    monkeypatch.setattr(agent_install, "distribution", lambda name: fake)

    assert agent_install.editable_source_path() is None


def test_launcher_carries_the_editable_flags_ahead_of_the_requirement(
    monkeypatch, tmp_path
):
    source = tmp_path / "checkout"
    monkeypatch.setattr(
        agent_install, "EDITABLE_FLAGS", ["--with-editable", str(source)]
    )

    tokens = shlex.split(agent_install.build_hook_command("repobrain @ file:///x"))
    assert tokens[:5] == [
        "uvx", "--with-editable", str(source), "--from", "repobrain @ file:///x",
    ]

    args = agent_install.build_mcp_args("repobrain[mcp] @ file:///x", tmp_path / "repo")
    assert args[:4] == [
        "--with-editable", str(source), "--from", "repobrain[mcp] @ file:///x",
    ]


def test_install_upgrades_a_pre_editable_launcher_in_place(tmp_path):
    # Configs written before D52 carry the same requirement without the
    # editable flags. That is this installer's own earlier output, not a
    # user-selected fork, so it must be upgraded rather than failed closed --
    # otherwise every existing installation stays on the stale build forever,
    # which is the whole defect D52 exists to fix.
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    legacy_hook = agent_install.legacy_uvx_hook_command()
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": legacy_hook}]}]}}),
        encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        MCP_SERVER_NAME: agent_install.legacy_mcp_server_entry(root)}}),
        encoding="utf-8")

    result = install_agent(root)

    assert result["changed"] is True
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    commands = [h["command"] for group in hooks for h in group["hooks"]]
    assert commands == [agent_install.HOOK_COMMAND]
    entry = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    assert entry["mcpServers"][MCP_SERVER_NAME] == mcp_server_entry(root)


def test_launcher_stays_plain_when_the_install_is_immutable(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_install, "EDITABLE_FLAGS", [])

    tokens = shlex.split(agent_install.build_hook_command("repobrain==0.1.0"))
    assert tokens[:3] == ["uvx", "--from", "repobrain==0.1.0"]
    assert "--with-editable" not in tokens


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
    # Positional indexing would break whenever the launcher gains a flag (D52
    # added --with-editable for editable installs); assert the contract instead.
    args = entry["args"]
    assert args[args.index("--from") + 1].startswith("repobrain[mcp]")
    assert args[-1] == str(root.resolve())
    assert args[-2] == "--path"
    assert args[-4:-2] == ["repobrain", "mcp"]

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
        # The agent skill is installed from the wheel, not the source tree.
        assert "repobrain/agent_skill/SKILL.md" in names
        assert "repobrain/agent_skill/reference.md" in names
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
    assert any(name.endswith("/repobrain/agent_skill/SKILL.md") for name in names)
    assert any(name.endswith("/repobrain/agent_skill/reference.md") for name in names)
    assert not any("__pycache__" in name for name in names)
