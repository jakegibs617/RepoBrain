"""Deterministic synthetic large-repository generator.

`generate_synthetic_repo` writes a fully deterministic, real (parseable)
Python + Markdown repository to a directory. It is fast (pure string writes,
no parsing) and produces files with *known* graph answers so scale tests can
assert exact counts, not just "it didn't crash":

- ``n_modules`` Python files, one per index, each defining:
  - ``helper_{i}(x=0)`` — a distinct, globally-unique function name.
  - ``worker_{i}()`` — calls ``helper_{(i + 1) % n_modules}()`` as a *bare*
    call. The callee has a globally unique name and lives in a different
    file, so this is only resolvable via the cross-file name-match pass in
    ``CodeParser.finish_run`` (D16) — this is the fan-out this fixture is
    built to stress.
  - ``Service{i}.run()`` — calls ``worker_{i}()`` in the *same* file, which
    resolves during parsing (not through ``finish_run``).
  - Every ``env_every``-th module reads ``os.environ["SERVICE_TOKEN"]``, so
    many files converge on one repo-global ``EnvVar`` node (D17).
- A handful of Markdown docs under ``docs/`` that reference known modules by
  exact backticked path and exact qualified symbol name, so MENTIONS
  reconciliation (D20) has real, countable work to do at scale.

Nothing here is committed to source control: callers generate into a
pytest ``tmp_path`` or an explicit scratch directory and are responsible for
cleanup (``tmp_path`` cleans itself up; the benchmark script removes its own
temp directory when done).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FILES_PER_PACKAGE = 25
DEFAULT_ENV_EVERY = 10
ENV_VAR_NAME = "SERVICE_TOKEN"


@dataclass(frozen=True)
class SyntheticRepoInfo:
    root: Path
    n_modules: int
    files_per_package: int
    module_paths: list[str] = field(default_factory=list)
    doc_paths: list[str] = field(default_factory=list)
    env_var_name: str = ENV_VAR_NAME
    env_reader_count: int = 0
    total_files: int = 0

    def module_path(self, index: int) -> str:
        return self.module_paths[index]

    def qname(self, index: int) -> str:
        """Dotted qualified module name matching python_module_qname()."""
        return self.module_path(index)[: -len(".py")].replace("/", ".")


def _module_path(index: int, files_per_package: int) -> str:
    group = index // files_per_package
    return f"src/pkg{group:04d}/mod{index:05d}.py"


def _module_source(index: int, n_modules: int, env_every: int) -> str:
    nxt = (index + 1) % n_modules
    lines = [
        f'"""Synthetic module {index}."""',
        "import os",
        "",
        "",
        f"def helper_{index}(x=0):",
        f"    return x + {index}",
        "",
        "",
        f"def worker_{index}():",
        f"    return helper_{nxt}()",
        "",
        "",
        f"class Service{index}:",
        '    """Deterministic synthetic service."""',
        "",
        "    def run(self):",
        f"        return worker_{index}()",
        "",
    ]
    if index % env_every == 0:
        lines += [
            "",
            f"def read_token_{index}():",
            f'    return os.environ.get("{ENV_VAR_NAME}")',
            "",
        ]
    return "\n".join(lines) + "\n"


def generate_synthetic_repo(
    root: str | Path,
    n_modules: int = 1200,
    files_per_package: int = DEFAULT_FILES_PER_PACKAGE,
    env_every: int = DEFAULT_ENV_EVERY,
    n_docs: int = 12,
) -> SyntheticRepoInfo:
    """Write a deterministic synthetic repository under ``root``.

    Returns a :class:`SyntheticRepoInfo` describing exactly what was
    written, so tests can assert on known graph/query answers instead of
    guessing at scale-corpus content.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    module_paths: list[str] = []
    env_reader_count = 0
    seen_dirs: set[Path] = set()
    for i in range(n_modules):
        rel = _module_path(i, files_per_package)
        module_paths.append(rel)
        abs_path = root / rel
        if abs_path.parent not in seen_dirs:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(abs_path.parent)
        abs_path.write_text(_module_source(i, n_modules, env_every), encoding="utf-8")
        if i % env_every == 0:
            env_reader_count += 1

    doc_paths: list[str] = []
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    docs_stride = max(1, n_modules // max(1, n_docs))
    for d in range(n_docs):
        idx = min((d * docs_stride) % n_modules, n_modules - 1)
        rel = f"docs/notes_{d:03d}.md"
        doc_paths.append(rel)
        target_path = module_paths[idx]
        qname = target_path[: -len(".py")].replace("/", ".")
        (root / rel).write_text(
            "\n".join(
                [
                    f"# Notes {d:03d}",
                    "",
                    # leading "/" anchors the reference to the repo root
                    # (MarkdownMentionReconciler treats un-anchored backtick
                    # paths as relative to the referencing document).
                    f"See `/{target_path}` for the synthetic worker chain.",
                    "",
                    f"`{qname}.helper_{idx}` is the entry point used in benchmarks.",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    total_files = n_modules + n_docs
    return SyntheticRepoInfo(
        root=root,
        n_modules=n_modules,
        files_per_package=files_per_package,
        module_paths=module_paths,
        doc_paths=doc_paths,
        env_reader_count=env_reader_count,
        total_files=total_files,
    )


def touch_modules(info: SyntheticRepoInfo, indices: list[int], suffix: str = "# touched\n") -> list[str]:
    """Deterministically append a comment to the given module indices.

    Used to construct a bounded "small change" scenario: content (and
    therefore hash) changes for exactly ``len(indices)`` files, independent
    of total corpus size.
    """
    changed: list[str] = []
    for idx in indices:
        rel = info.module_path(idx)
        path = info.root / rel
        with path.open("a", encoding="utf-8") as fh:
            fh.write(suffix)
        changed.append(rel)
    return changed
