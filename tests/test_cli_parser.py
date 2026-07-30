"""CLI entrypoint extraction: the shapes, the names, and the invocation prefix.

The brief's `Entrypoints` section answers *how do I invoke this?*, so a name
that is merely close is a wrong answer wearing the right shape. These tests
check the derived names against Click's own resolution rather than against a
hand-written list.
"""
from pathlib import Path

import click
import pytest

from repobrain.parsers.cli_parser import CliParser

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "click_cli_app"
FIXTURE_MODULE = "mytool/cli.py"


def _extract(content: str, path: str = FIXTURE_MODULE, root: Path | None = None):
    parser = CliParser()
    parser.begin_run({path}, root)
    return parser.parse(path, content)


def _invocations(result) -> set[str]:
    return {node.qualified_name for node in result.nodes}


def _real_click_invocations(source: str) -> set[str]:
    """Ask Click what these declarations actually resolve to.

    ``pyproject.toml`` pins only ``click>=8.1``. Asserting against a literal
    list would pass forever while Click's own naming rule moved underneath it;
    asking the installed Click makes that a failure instead.
    """
    namespace: dict = {}
    exec(compile(source, "<fixture>", "exec"), namespace)

    def walk(group: click.Group, prefix: tuple[str, ...]) -> set[str]:
        found: set[str] = set()
        for name, command in group.commands.items():
            path = prefix + (name,)
            if isinstance(command, click.Group):
                found |= walk(command, path)
            else:
                found.add(" ".join(path))
        return found

    return walk(namespace["main"], ())


@pytest.fixture
def fixture_source() -> str:
    return (FIXTURE_ROOT / FIXTURE_MODULE).read_text(encoding="utf-8")


def test_derived_names_match_what_click_itself_resolves(fixture_source):
    """The invocation the brief prints has to be the one that runs.

    `find_symbol` is typed as `repobrain find-symbol`; an extractor that
    promoted the function name verbatim would put a command that does not exist
    into the first thing an agent reads at session start.
    """
    result = _extract(fixture_source, root=FIXTURE_ROOT)

    expected = {f"mytool {name}" for name in _real_click_invocations(fixture_source)}
    assert _invocations(result) == expected
    assert "mytool list-items" in expected, "the fixture must exercise the dashing rule"


def test_a_group_is_not_itself_an_invocation(fixture_source):
    """`mytool db` does no work; only its leaves are entrypoints."""
    result = _extract(fixture_source, root=FIXTURE_ROOT)

    assert "mytool db" not in _invocations(result)
    assert "mytool" not in _invocations(result)
    assert "mytool db migrate-up" in _invocations(result)


def test_an_explicit_name_beats_the_function_name(fixture_source):
    result = _extract(fixture_source, root=FIXTURE_ROOT)

    assert "mytool build-all" in _invocations(result)
    assert "mytool build" not in _invocations(result)


def test_a_nested_command_carries_the_whole_path(fixture_source):
    result = _extract(fixture_source, root=FIXTURE_ROOT)
    node = next(n for n in result.nodes if n.name == "migrate-up")

    assert node.qualified_name == "mytool db migrate-up"
    assert node.metadata["group_path"] == ["db"]
    assert node.metadata["handler"] == "db_migrate"
    assert node.path == FIXTURE_MODULE
    assert node.start_line and node.end_line


def test_the_console_script_supplies_the_prefix_and_only_for_its_own_module():
    """`[project.scripts]` is the only place the program's name is written down.

    The graph cannot supply it: `project.scripts.mytool` is stored value-free,
    so the target `mytool.cli:main` never reaches the database.
    """
    source = "import click\n\n@click.group()\ndef main():\n    pass\n\n@main.command()\ndef go():\n    pass\n"
    prefixed = _extract(source, root=FIXTURE_ROOT)
    assert _invocations(prefixed) == {"mytool go"}

    other = _extract(source, path="elsewhere/tool.py", root=FIXTURE_ROOT)
    assert _invocations(other) == {"go"}


def test_a_repository_with_no_console_script_still_names_the_command(tmp_path):
    source = "import click\n\n@click.group()\ndef main():\n    pass\n\n@main.command()\ndef go():\n    pass\n"

    assert _invocations(_extract(source, root=tmp_path)) == {"go"}
    assert _invocations(_extract(source, root=None)) == {"go"}


def test_an_unreadable_or_malformed_manifest_is_not_fatal(tmp_path):
    """A prefix is a caption. It must never be able to abort the run that prints it."""
    (tmp_path / "pyproject.toml").write_text("[project.scripts\nbroken = ", encoding="utf-8")
    source = "import click\n\n@click.command()\ndef go():\n    pass\n"

    assert _invocations(_extract(source, root=tmp_path)) == {"go"}


def test_a_standalone_command_needs_no_group():
    source = "import click\n\n@click.command('run-once')\ndef go():\n    pass\n"

    assert _invocations(_extract(source)) == {"run-once"}


def test_the_bare_decorator_form_is_recognised():
    """`@click.command` without a call is valid Click and declares a command."""
    source = "import click\n\n@click.command\ndef do_thing():\n    pass\n"

    assert _invocations(_extract(source)) == {"do-thing"}


@pytest.mark.parametrize(
    "imports,framework",
    [
        ("import click", "click"),
        ("import typer", "typer"),
        ("from typer import Typer", "typer"),
        ("import somethingelse", "unknown"),
    ],
)
def test_the_framework_is_read_from_the_file_that_declares_the_command(imports, framework):
    """The decorator shape is shared; the import is what distinguishes them.

    Typer's `@app.command()` is syntactically identical to Click's, so the
    shape alone cannot say which framework a command belongs to. Guessing would
    be worse than reporting `unknown`.
    """
    source = f"{imports}\n\n@app.command()\ndef go():\n    pass\n"

    nodes = _extract(source).nodes
    assert [node.metadata["framework"] for node in nodes] == [framework]


def test_a_file_that_does_not_parse_warns_instead_of_raising():
    result = _extract("import click\n\n@main.command()\ndef broken(:\n    pass\n")

    assert result.nodes == []
    assert result.warnings and FIXTURE_MODULE in result.warnings[0]


def test_a_file_that_cannot_declare_a_command_is_never_parsed(monkeypatch):
    """Every decorator this parser matches spells `command`.

    Without the guard, `ast.parse` ran on all 100 Python files in this
    repository to find declarations in one, and a full re-index cost 35% more.
    The guard has to be free of behaviour, not merely cheap: it may only skip
    files that could not have produced anything.
    """
    import ast as ast_module

    def refuse(*args, **kwargs):
        raise AssertionError("parsed a file that cannot declare a command")

    monkeypatch.setattr(ast_module, "parse", refuse)
    result = _extract("import click\n\napp = click.Group()\n")

    assert result.nodes == []
    assert result.warnings == []


def test_a_dynamic_receiver_is_declined_rather_than_guessed():
    """`get_app().command()` names no group this file can resolve."""
    source = "import click\n\n@get_app().command()\ndef go():\n    pass\n"

    assert _extract(source).nodes == []


def test_only_python_is_parsed():
    parser = CliParser()

    assert parser.can_parse("cli.py", "python")
    assert not parser.can_parse("cli.js", "javascript")
    assert not parser.can_parse("README.md", "markdown")
