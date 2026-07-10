"""Tests for graph.queries: find_symbol and explain_file."""
from repobrain.graph.queries import explain_file, find_symbol, resolve_file_path


def test_find_symbol_exact(indexer, store, small_app):
    indexer.index(small_app)
    results = find_symbol(store, "create_user", exact=True)
    assert len(results) == 1
    hit = results[0]
    assert hit["qualified_name"] == "app.services.user_service.create_user"
    assert hit["type"] == "Function"
    assert hit["path"] == "app/services/user_service.py"
    assert hit["start_line"] == 7 and hit["end_line"] == 10
    assert hit["signature"].startswith("def create_user")


def test_find_symbol_substring_ranks_exact_first(indexer, store, small_app):
    indexer.index(small_app)
    results = find_symbol(store, "create_user")
    assert len(results) >= 3  # create_user, create_user_route, handle_create_user...
    assert results[0]["name"] == "create_user"  # exact match first
    names = {r["name"] for r in results}
    assert "handle_create_user" in names
    assert "create_user_route" in names


def test_find_symbol_matches_qualified_name(indexer, store, small_app):
    indexer.index(small_app)
    results = find_symbol(store, "user_service.create")
    assert any(r["name"] == "create_user" for r in results)


def test_find_symbol_types_and_limit(indexer, store, small_app):
    indexer.index(small_app)
    results = find_symbol(store, "user", limit=3)
    assert len(results) == 3
    only_classes = find_symbol(store, "UserRepository", types=("Class",))
    assert only_classes and all(r["type"] == "Class" for r in only_classes)


def test_find_symbol_empty_and_no_match(indexer, store, small_app):
    indexer.index(small_app)
    assert find_symbol(store, "") == []
    assert find_symbol(store, "does_not_exist_anywhere") == []


def test_resolve_file_path_variants(indexer, store, small_app):
    indexer.index(small_app)
    assert resolve_file_path(store, "app/db/config.py") == "app/db/config.py"
    assert resolve_file_path(store, "./app/db/config.py") == "app/db/config.py"
    # unique suffix match
    assert resolve_file_path(store, "user_service.py") == "app/services/user_service.py"
    # ambiguous suffix (__init__.py exists in several packages) -> None
    assert resolve_file_path(store, "__init__.py") is None
    assert resolve_file_path(store, "nope.py") is None


def test_explain_file_python(indexer, store, small_app):
    indexer.index(small_app)
    info = explain_file(store, "app/services/user_service.py")
    assert info is not None
    assert info["language"] == "python"
    assert info["module"]["qualified_name"] == "app.services.user_service"
    assert info["module"]["is_test_file"] is False

    names = {s["name"]: s for s in info["symbols"]}
    assert "create_user" in names and names["create_user"]["type"] == "Function"

    internal = {i["module"] for i in info["imports"]["internal"]}
    assert internal == {"app.repositories.user_repository"}
    importers = {i["module"] for i in info["imported_by"]}
    assert "app.handlers.user_handler" in importers
    assert "tests.test_users" in importers

    callers = {c["caller"] for c in info["called_by"]}
    assert "app.handlers.user_handler.handle_create_user" in callers

    tests = {t["path"] for t in info["tests"]}
    assert "tests/test_users.py" in tests
    assert any(
        doc["path"] == "README.md" and doc["name"] == "Architecture"
        for doc in info["docs"]
    )


def test_explain_file_symbol_tree_nests_methods(indexer, store, small_app):
    indexer.index(small_app)
    info = explain_file(store, "app/repositories/user_repository.py")
    classes = [s for s in info["symbols"] if s["type"] == "Class"]
    assert len(classes) == 1
    method_names = {c["name"] for c in classes[0]["children"]}
    assert {"__init__", "insert", "find_by_id"} <= method_names


def test_explain_file_env_and_external_imports(indexer, store, small_app):
    indexer.index(small_app)
    info = explain_file(store, "app/db/config.py")
    assert info["imports"]["external"] == ["os"]
    env = {e["var"] for e in info["env_vars"]}
    assert env == {"DATABASE_URL"}


def test_explain_file_js(indexer, store, node_app):
    indexer.index(node_app)
    info = explain_file(store, "src/services/userService.js")
    assert info["module"]["qualified_name"] == "src/services/userService"
    internal = {i["module"] for i in info["imports"]["internal"]}
    assert internal == {"src/config"}
    importers = {i["path"] for i in info["imported_by"]}
    assert "src/routes/users.js" in importers
    tests = {t["path"] for t in info["tests"]}
    assert "src/services/userService.test.js" in tests
    callers = {c["caller"] for c in info["called_by"]}
    assert "src/routes/users" in callers  # module-level route callbacks


def test_explain_file_unknown_returns_none(indexer, store, small_app):
    indexer.index(small_app)
    assert explain_file(store, "no/such/file.py") is None
