"""Identity of the RepoBrain build that produced an answer.

D44 fingerprints ``repobrain/parsers/**`` to answer *would re-extracting these
files produce different facts*, and that scope is deliberately narrow. It
leaves the read path — briefing, queries, the CLI, the MCP server — outside
any identity at all, so a build that answers arbitrarily old can report
``current`` in perfect sincerity: the facts and the code that stored them agree
with each other, and nothing compares either to the code doing the reading.

This module is that missing identity. It is provenance, never a gate; see
:func:`code_identity`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, typing only
    from .graph.store import GraphStore

#: meta key holding the code identity that built the stored graph
CODE_FINGERPRINT_KEY = "code_fingerprint"

#: the installed package's own directory, wherever it was installed from
PACKAGE_ROOT = Path(__file__).resolve().parent


def package_source_digest(root: Path | None = None) -> str:
    """Digest every ``*.py`` this package ships, recursively.

    Recursive because most of the read path is one directory down: ``graph/``,
    ``indexing/``, ``parsers/``. Read fresh on every call for D44's reason —
    a cache would hold a stale answer across exactly the edit this exists to
    notice — and re-measured before widening the scope, since ``freshness`` may
    be polled on a timer. The numbers are in D56.

    Paths are hashed *relative to the package root*, never absolute: an
    identity that moved with the install directory would name the machine
    rather than the build, and could not be compared across the only boundary
    worth comparing it across. Names are hashed alongside the bytes so that
    moving a definition between modules is a change, as D44 does for renames.
    """
    root = Path(root) if root is not None else PACKAGE_ROOT
    digest = hashlib.sha256()
    sources = sorted(root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix())
    for source in sources:
        digest.update(source.relative_to(root).as_posix().encode("utf-8"))
        digest.update(source.read_bytes())
    return digest.hexdigest()[:16]


def code_identity(store: GraphStore | None = None) -> dict:
    """Name the build that is answering, and whether it built the index.

    ``changed_since_index`` is a **label, not a gate**, and no caller may treat
    it as one. The staleness axes D40 designed are repairable by the agent
    reading them — it runs ``repobrain index`` — whereas no agent can reinstall
    the code it is running inside, and for a read-path change re-indexing would
    not alter one stored fact. A status that can never become ``ok`` by any
    action available to the caller would break what ``can_query`` promises, so
    this value never reaches ``is_stale``.

    ``None`` means unknown, which is what every database written before this
    field existed reports. Calling that a mismatch would light the advisory on
    every pre-existing install, and an advisory that is always on is off.

    An unreadable package source is unknown too, never an exception: this value
    decorates an answer, and a label that can abort the query it labels is a
    worse failure than the one it reports.
    """
    try:
        fingerprint: str | None = package_source_digest()
    except OSError:
        fingerprint = None
    recorded = store.get_meta(CODE_FINGERPRINT_KEY) if store is not None else None
    if fingerprint is None:
        recorded = None
    return {
        "fingerprint": fingerprint,
        # D52's failure was a *location*: a four-day-old cached wheel answered
        # while a checkout sat beside it. A digest alone cannot say which.
        "path": str(PACKAGE_ROOT),
        "changed_since_index": None if recorded is None else recorded != fingerprint,
    }
