import shutil
from pathlib import Path

import pytest

from repobrain.graph.store import GraphStore
from repobrain.indexing.indexer import Indexer

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def small_app(tmp_path: Path) -> Path:
    dest = tmp_path / "small_python_app"
    shutil.copytree(FIXTURES / "small_python_app", dest)
    return dest


@pytest.fixture
def docs_app(tmp_path: Path) -> Path:
    dest = tmp_path / "markdown_docs_app"
    shutil.copytree(FIXTURES / "markdown_docs_app", dest)
    return dest


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    s = GraphStore(tmp_path / "db" / "repobrain.sqlite")
    yield s
    s.close()


@pytest.fixture
def indexer(store: GraphStore) -> Indexer:
    return Indexer(store)
