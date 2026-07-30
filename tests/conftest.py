import shutil
from pathlib import Path

import pytest

from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer

FIXTURES = Path(__file__).parent / "fixtures"

# Stray .repobrain/ dirs (e.g. from running the CLI against a fixture by hand)
# must never leak into test copies: the DB inside pins its original root and
# would poison the copied tree.
_COPY_IGNORE = shutil.ignore_patterns(".repobrain")


def copy_fixture(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest, ignore=_COPY_IGNORE)
    return dest


@pytest.fixture
def small_app(tmp_path: Path) -> Path:
    return copy_fixture("small_python_app", tmp_path)


@pytest.fixture
def docs_app(tmp_path: Path) -> Path:
    return copy_fixture("markdown_docs_app", tmp_path)


@pytest.fixture
def node_app(tmp_path: Path) -> Path:
    return copy_fixture("node_api_app", tmp_path)


@pytest.fixture
def click_app(tmp_path: Path) -> Path:
    return copy_fixture("click_cli_app", tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    s = GraphStore(tmp_path / "db" / "repobrain.sqlite")
    yield s
    s.close()


@pytest.fixture
def indexer(store: GraphStore) -> Indexer:
    return Indexer(store)
