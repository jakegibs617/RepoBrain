import json
import subprocess

from repobrain.agent_install import GITIGNORE_ENTRY, install_agent, uninstall_agent


def test_install_agent_creates_gitignore_and_hides_existing_database(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    database = tmp_path / ".repobrain" / "repobrain.sqlite"
    database.parent.mkdir()
    database.write_bytes(b"audit canary")

    first = install_agent(tmp_path)
    second = install_agent(tmp_path)

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
        f"{GITIGNORE_ENTRY}\n"
    )
    assert first["gitignore"] == {
        "path": ".gitignore",
        "installed": True,
        "changed": True,
    }
    assert second["gitignore"]["changed"] is False
    assert second["changed"] is False
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".repobrain" not in status


def test_install_agent_preserves_gitignore_and_uninstall_keeps_safety_entry(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("dist/\n# human rule\n", encoding="utf-8")

    result = install_agent(tmp_path)
    uninstall_agent(tmp_path)

    assert result["gitignore"]["changed"] is True
    assert gitignore.read_text(encoding="utf-8") == (
        "dist/\n# human rule\n.repobrain/\n"
    )


def test_install_agent_json_reports_unchanged_existing_safety_entry(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("dist/\n  .repobrain/  \n", encoding="utf-8")

    result = install_agent(tmp_path)

    # The direct result is the object emitted by the JSON CLI wrapper.
    json.dumps(result)
    assert result["gitignore"]["installed"] is True
    assert result["gitignore"]["changed"] is False
    assert gitignore.read_text(encoding="utf-8") == "dist/\n  .repobrain/  \n"
