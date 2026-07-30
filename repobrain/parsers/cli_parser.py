"""Source-local syntax facts for decorator-declared CLI commands.

The declaration shape — ``@<x>.command(...)`` on a function — is shared by
Click and Typer, so the shape is what this parser matches and the file's own
imports are what name the framework. ``argparse`` declares subcommands through
``subparsers.add_parser(...)`` calls with no decorator at all and is not
covered; that is a documented gap rather than a silent one.
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from ..graph.schema import FtsRow, Node, NodeType
from .base import ParseResult, Parser

#: Frameworks whose command decorator is this shape, most specific first.
#: Typer is built on Click and a file may well import both; the more specific
#: claim wins so that the answer does not depend on import order.
_FRAMEWORKS = ("typer", "click")

#: Decorator attributes that declare something. ``group`` declares a container
#: for other commands and is deliberately not an entrypoint itself: `repobrain
#: history` does no work, and printing it as an invocation would be wrong.
_DECLARES = {"command", "group"}


def _console_scripts(root: str | Path | None) -> dict[str, dict[str, str]]:
    """Map ``module path -> {attribute: script name}`` from ``[project.scripts]``.

    One bounded manifest read per index run, in ``begin_run`` and never during
    parsing — the same shape as :func:`_read_go_module_prefix` (D19). It is
    needed because the console script's name is the only place the program's
    own name is written down, and the graph cannot supply it: structured
    configuration is stored value-free, so ``project.scripts.repobrain`` records
    ``{"value_type": "str"}`` and never the target it points at.

    A manifest that is missing, unreadable, or malformed yields an empty map.
    The prefix is a caption on a fact, and a caption must never acquire the
    power to abort the run that prints it (D56).
    """
    if root is None:
        return {}
    try:
        raw = (Path(root) / "pyproject.toml").read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    project = data.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict):
        return {}
    mapping: dict[str, dict[str, str]] = {}
    for script, target in scripts.items():
        if not isinstance(target, str) or ":" not in target:
            continue
        module, _, attribute = target.partition(":")
        # `attribute` may be dotted (`cli:main.cli`); only the first name is
        # ever a module-level binding this parser can see.
        attribute = attribute.split(".")[0].strip()
        stem = module.strip().replace(".", "/")
        if not attribute or not stem:
            continue
        # A package's `__init__.py` and the `src/` layout are the two other
        # places the same dotted target resolves to. Recording all of them
        # costs nothing and avoids a layout-specific rule.
        for candidate in (f"{stem}.py", f"{stem}/__init__.py",
                          f"src/{stem}.py", f"src/{stem}/__init__.py"):
            mapping.setdefault(candidate, {})[attribute] = script
    return mapping


class CliParser(Parser):
    """Persist CLI command declarations; handler resolution is separate.

    No edges are emitted. Linking a command to the function it decorates needs
    ``CodeParser``'s qualified-name convention, which is a reconciler's job for
    the same reason ``HANDLES_ROUTE`` is kept out of ``RouteParser``. The
    handler's name and line are in metadata so that reconciler has what it
    needs.
    """

    name = "cli_parser"

    def __init__(self) -> None:
        self._scripts: dict[str, dict[str, str]] = {}

    def begin_run(
        self, known_files: set[str], root: str | Path | None = None,
    ) -> None:
        """Read ``[project.scripts]`` once, before any file is parsed.

        ``root`` is optional: without it every command still gets its declared
        path, just without the program name in front of it.
        """
        self._scripts = _console_scripts(root)

    def can_parse(self, path: str, language: str | None) -> bool:
        return language == "python"

    def parse(self, path: str, content: str) -> ParseResult:
        result = ParseResult()
        # Behaviour-preserving, and not a micro-optimisation: every decorator
        # this parser matches spells `command`, so a file without that substring
        # cannot declare one and there is nothing here to say about it — errors
        # included. Without the guard `ast.parse` ran on all 100 Python files in
        # this repository to find declarations in one, and a measured full
        # re-index went from 0.852 s to 1.153 s (+35%). With it, 21 files are
        # parsed and the same measurement is 0.981 s (+15%) — smaller, not free.
        if "command" not in content:
            return result
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError) as exc:
            result.warnings.append(f"{path}: CLI syntax extraction failed: {exc}")
            return result
        framework = self._framework(tree)
        scripts = self._scripts.get(path, {})

        #: function name -> the invocation path its children are nested under
        group_paths: dict[str, list[str]] = {}
        #: function name -> the module-level identifier its tree is rooted at,
        #: which is what `[project.scripts]` names
        roots: dict[str, str] = {}

        for function, decorator, attribute, receiver in self._declarations(tree):
            command = self._command_name(decorator, function)
            if command is None:
                continue  # a name this file does not state is not a name
            nested = receiver in group_paths
            if nested:
                prefix = group_paths[receiver]
                root = roots[receiver]
            elif receiver in _FRAMEWORKS:
                # `@click.command()` / `@click.group()`: the declaration is its
                # own root, named after the function it decorates.
                prefix, root = [], function.name
            else:
                # An app object built elsewhere in the module — `app =
                # typer.Typer()`. The binding is the root the manifest names.
                prefix, root = [], receiver
            if attribute == "group":
                # A *root* group contributes no path segment: it is the program
                # itself, and its name is the console script's, not its
                # function's. Only a group nested inside another one is typed.
                group_paths[function.name] = (prefix + [command]) if nested else []
                roots[function.name] = root
                continue
            script = scripts.get(root)
            invocation = " ".join(([script] if script else []) + prefix + [command])
            metadata = {
                "framework": framework, "command": command,
                "group_path": prefix, "handler": function.name,
                "handler_start_line": function.lineno, "line": decorator.lineno,
                "end_line": getattr(function, "end_lineno", function.lineno),
            }
            if script:
                metadata["script"] = script
            node = Node(
                type=NodeType.CLI_COMMAND, name=command, qualified_name=invocation,
                path=path, start_line=decorator.lineno,
                end_line=getattr(function, "end_lineno", function.lineno),
                language="cli", metadata=metadata, extractor=self.name,
            )
            result.nodes.append(node)
            result.fts_rows.append(FtsRow(path, command, invocation, node.id))
        return result

    @staticmethod
    def _framework(tree: ast.Module) -> str:
        """Name the framework from the imports, or decline to guess.

        The decorator shape is identical across Click and Typer, so the shape
        alone cannot say which one a command belongs to. `unknown` is a value:
        a command that is really there is still worth reporting when the only
        thing missing is its label.
        """
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        return next((name for name in _FRAMEWORKS if name in imported), "unknown")

    @staticmethod
    def _declarations(tree: ast.Module) -> list[tuple]:
        """Every declaring decorator, in source order.

        Order is load-bearing: a group must be seen before the commands nested
        under it, which is exactly the order Python itself requires.
        """
        found = []
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in function.decorator_list:
                call = decorator.func if isinstance(decorator, ast.Call) else decorator
                if not isinstance(call, ast.Attribute) or call.attr not in _DECLARES:
                    continue
                if not isinstance(call.value, ast.Name):
                    continue  # dynamic receiver such as get_app().command(...)
                found.append((function, decorator, call.attr, call.value.id))
        return sorted(found, key=lambda item: item[1].lineno)

    @staticmethod
    def _command_name(decorator, function) -> str | None:
        """The name this command is actually invoked by.

        Reproduces Click's own rule for the derived case —
        ``f.__name__.lower().replace("_", "-")`` — which is not cosmetic:
        `find_symbol` is typed as `find-symbol`, and a brief that printed the
        function name would name a command that does not exist.
        """
        if isinstance(decorator, ast.Call):
            explicit = next(
                (keyword.value for keyword in decorator.keywords if keyword.arg == "name"),
                decorator.args[0] if decorator.args else None,
            )
            if explicit is not None:
                if isinstance(explicit, ast.Constant) and isinstance(explicit.value, str):
                    return explicit.value
                # `@main.command(SOME_CONST)` states a name this parser cannot
                # read. Guessing the derived one would be a different command.
                return None
        return function.name.lower().replace("_", "-")
