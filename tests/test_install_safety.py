import json
import re
import subprocess

import pytest

from repobrain.agent_install import (
    GITIGNORE_ENTRY,
    SKILL_DIRNAME,
    SKILL_FILES,
    SKILL_MARKER,
    install_agent,
    shipped_skill_file,
    uninstall_agent,
)


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


def test_install_agent_writes_marked_skill_and_is_idempotent(tmp_path):
    first = install_agent(tmp_path)
    second = install_agent(tmp_path)

    skill_dir = tmp_path / SKILL_DIRNAME
    for name in SKILL_FILES:
        content = (skill_dir / name).read_text(encoding="utf-8")
        assert SKILL_MARKER in content
        assert content == shipped_skill_file(name)
    assert first["skill"] == {
        "path": str(SKILL_DIRNAME),
        "installed": True,
        "changed": True,
    }
    assert second["skill"]["changed"] is False
    assert second["changed"] is False


def test_install_agent_upgrades_an_outdated_owned_skill(tmp_path):
    install_agent(tmp_path)
    skill = tmp_path / SKILL_DIRNAME / "SKILL.md"
    skill.write_text(f"# stale shipped version\n{SKILL_MARKER}\n", encoding="utf-8")

    result = install_agent(tmp_path)

    assert skill.read_text(encoding="utf-8") == shipped_skill_file("SKILL.md")
    assert result["skill"]["changed"] is True


@pytest.mark.parametrize("owned_name", SKILL_FILES)
def test_install_agent_never_touches_a_skill_whose_marker_was_removed(tmp_path, owned_name):
    """Deleting the marker is how a user takes ownership of the whole skill."""
    install_agent(tmp_path)
    skill_dir = tmp_path / SKILL_DIRNAME
    adopted = skill_dir / owned_name
    adopted.write_text("# my own version, no marker\n", encoding="utf-8")
    before = {name: (skill_dir / name).read_text(encoding="utf-8") for name in SKILL_FILES}

    result = install_agent(tmp_path)

    after = {name: (skill_dir / name).read_text(encoding="utf-8") for name in SKILL_FILES}
    assert after == before
    assert result["skill"] == {
        "path": str(SKILL_DIRNAME),
        "installed": False,
        "changed": False,
        "reason": "user_owned",
    }
    # A user-owned skill is not a conflict: the rest of the install still lands.
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".mcp.json").exists()


def test_install_agent_adopts_an_unrelated_preexisting_skill_without_failing(tmp_path):
    skill = tmp_path / SKILL_DIRNAME / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: repobrain\n---\nsomeone else's skill\n", encoding="utf-8")

    result = install_agent(tmp_path)

    assert skill.read_text(encoding="utf-8").endswith("someone else's skill\n")
    assert not (skill.parent / "reference.md").exists()
    assert result["skill"]["reason"] == "user_owned"
    assert result["status"] == "ok"


def test_uninstall_removes_owned_skill_and_prunes_empty_directories(tmp_path):
    install_agent(tmp_path)

    result = uninstall_agent(tmp_path)

    assert not (tmp_path / SKILL_DIRNAME).exists()
    assert not (tmp_path / ".claude" / "skills").exists()
    assert result["skill"]["changed"] is True


def test_uninstall_preserves_user_owned_skill_and_neighbouring_skills(tmp_path):
    install_agent(tmp_path)
    skill_dir = tmp_path / SKILL_DIRNAME
    (skill_dir / "SKILL.md").write_text("# mine now\n", encoding="utf-8")
    neighbour = tmp_path / ".claude" / "skills" / "unrelated" / "SKILL.md"
    neighbour.parent.mkdir(parents=True)
    neighbour.write_text("# unrelated\n", encoding="utf-8")

    result = uninstall_agent(tmp_path)

    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "# mine now\n"
    # The owned reference is still ours to remove even though SKILL.md was adopted.
    assert not (skill_dir / "reference.md").exists()
    assert neighbour.exists()
    assert result["skill"]["changed"] is True


def test_shipped_skill_frontmatter_is_valid_for_claude_code():
    content = shipped_skill_file("SKILL.md")
    match = re.match(r"^---\n(.*?)\n---\n", content, flags=re.DOTALL)
    assert match, "SKILL.md must open with YAML frontmatter"
    frontmatter = match.group(1)
    name = re.search(r"^name: (.+)$", frontmatter, flags=re.MULTILINE)
    description = re.search(r"^description: (.+)$", frontmatter, flags=re.MULTILINE)
    assert name and re.fullmatch(r"[a-z0-9-]+", name.group(1))
    assert description
    assert len(frontmatter) < 1024
