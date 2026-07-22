"""Tests for the tree-sitter code parser (Milestone 3)."""
from pathlib import Path

from repobrain.parsers.code_treesitter import CodeParser, is_test_file


def _node(store, type_, name, path=None):
    sql = "SELECT * FROM nodes WHERE type = ? AND name = ?"
    args = [type_, name]
    if path is not None:
        sql += " AND path = ?"
        args.append(path)
    return store.conn.execute(sql, args).fetchone()


def _edges(store, type_):
    return store.conn.execute(
        """
        SELECT e.*, s.qualified_name AS source_qname, t.qualified_name AS target_qname,
               t.name AS target_name
        FROM edges e
        JOIN nodes s ON s.id = e.source_node_id
        JOIN nodes t ON t.id = e.target_node_id
        WHERE e.type = ?
        """,
        (type_,),
    ).fetchall()


# -- Python extraction -------------------------------------------------------


def test_python_functions_classes_methods(indexer, store, small_app):
    indexer.index(small_app)

    fn = _node(store, "Function", "create_user", "app/services/user_service.py")
    assert fn is not None
    assert fn["qualified_name"] == "app.services.user_service.create_user"
    assert fn["start_line"] == 7 and fn["end_line"] == 10
    assert fn["language"] == "python"

    cls = _node(store, "Class", "UserRepository")
    assert cls is not None
    assert cls["qualified_name"] == "app.repositories.user_repository.UserRepository"

    method = _node(store, "Method", "insert")
    assert method is not None
    assert (
        method["qualified_name"]
        == "app.repositories.user_repository.UserRepository.insert"
    )
    # Class DEFINES Method
    defines = [
        e for e in _edges(store, "DEFINES")
        if e["source_qname"] == cls["qualified_name"]
        and e["target_qname"] == method["qualified_name"]
    ]
    assert len(defines) == 1


def test_python_module_nodes_and_file_defines_module(indexer, store, small_app):
    indexer.index(small_app)
    module = _node(store, "Module", "user_service")
    assert module is not None
    assert module["qualified_name"] == "app.services.user_service"
    # package __init__ maps to the package qname
    pkg = store.conn.execute(
        "SELECT * FROM nodes WHERE type='Module' AND path='app/__init__.py'"
    ).fetchone()
    assert pkg["qualified_name"] == "app"
    # File DEFINES Module
    row = store.conn.execute(
        """
        SELECT COUNT(*) FROM edges e
        JOIN nodes f ON f.id = e.source_node_id AND f.type = 'File'
        JOIN nodes m ON m.id = e.target_node_id AND m.type = 'Module'
        WHERE e.type = 'DEFINES' AND m.path = 'app/services/user_service.py'
        """
    ).fetchone()
    assert row[0] == 1


def test_python_nested_function_is_contained_not_defined(indexer, store, small_app):
    indexer.index(small_app)
    nested = _node(store, "Function", "create_user_route")
    assert nested["qualified_name"] == "app.api.routes.register_routes.create_user_route"
    contains = [
        e for e in _edges(store, "CONTAINS")
        if e["target_qname"] == nested["qualified_name"]
    ]
    assert len(contains) == 1
    assert contains[0]["source_qname"] == "app.api.routes.register_routes"


def test_python_module_level_variable(indexer, store, small_app):
    indexer.index(small_app)
    var = _node(store, "Variable", "DEFAULT_DATABASE_URL", "app/db/config.py")
    assert var is not None
    assert var["qualified_name"] == "app.db.config.DEFAULT_DATABASE_URL"


def test_python_import_resolution_internal_vs_external(indexer, store, small_app):
    import json

    indexer.index(small_app)
    imports = {
        (e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")
    }
    assert ("app.handlers.user_handler", "app.services.user_service") in imports
    assert ("app.services.user_service", "app.repositories.user_repository") in imports
    assert ("tests.test_users", "app.services.user_service") in imports
    # external import (os) is metadata, not a node or dangling edge
    module = store.conn.execute(
        "SELECT metadata_json FROM nodes WHERE type='Module' AND path='app/db/config.py'"
    ).fetchone()
    assert "os" in json.loads(module["metadata_json"])["external_imports"]
    assert _node(store, "Module", "os") is None


# -- CALLS ---------------------------------------------------------------------


def test_same_file_call_edge(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text(
        "def helper(x):\n    return x\n\n\ndef caller():\n    return helper(1)\n"
    )
    indexer.index(repo)
    calls = _edges(store, "CALLS")
    assert len(calls) == 1
    edge = calls[0]
    assert edge["source_qname"] == "mod.caller"
    assert edge["target_qname"] == "mod.helper"
    assert edge["confidence"] == 0.9
    assert edge["is_inferred"] == 0


def test_import_resolved_cross_file_call(indexer, store, small_app):
    indexer.index(small_app)
    calls = {
        (e["source_qname"], e["target_qname"]): e for e in _edges(store, "CALLS")
    }
    edge = calls[
        (
            "app.handlers.user_handler.handle_create_user",
            "app.services.user_service.create_user",
        )
    ]
    assert edge["confidence"] == 0.9
    assert edge["is_inferred"] == 0


def test_cross_file_name_only_call_is_inferred(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def unique_helper(x):\n    return x\n")
    # no import: resolvable only by name
    (repo / "b.py").write_text("def caller():\n    return unique_helper(1)\n")
    indexer.index(repo)
    calls = _edges(store, "CALLS")
    assert len(calls) == 1
    edge = calls[0]
    assert edge["source_qname"] == "b.caller"
    assert edge["target_qname"] == "a.unique_helper"
    assert edge["confidence"] == 0.7
    assert edge["is_inferred"] == 1
    assert edge["inference_reason"] == "name-match"


def test_ambiguous_name_only_call_is_skipped(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def dup(x):\n    return x\n")
    (repo / "b.py").write_text("def dup(x):\n    return x\n")
    (repo / "c.py").write_text("def caller():\n    return dup(1)\n")
    indexer.index(repo)
    assert _edges(store, "CALLS") == []


def test_ambiguous_name_only_call_with_many_duplicates_is_skipped(indexer, store, tmp_path: Path):
    """finish_run batches candidate lookup by name (name IN (...)); this
    exercises the batched path with more than a couple of same-named
    definitions, not just the minimal 2-duplicate ambiguity case above."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"dup_{i}.py").write_text("def widely_used(x):\n    return x\n")
    (repo / "caller.py").write_text("def caller():\n    return widely_used(1)\n")
    indexer.index(repo)
    assert _edges(store, "CALLS") == []


def test_name_only_call_excludes_same_named_definition_in_callers_own_file(
    indexer, store, tmp_path: Path,
):
    """The caller's own file also happens to define an unrelated *method*
    with the same name as the real (different-file, module-level) target --
    a Method decoy, since a same-named module-level function in the
    caller's own file would resolve via same-file `func_by_name` before
    ever reaching finish_run. finish_run's exclusion of same-path
    candidates must still leave exactly one true match: this exercises the
    arithmetic (total match count minus per-path count) the batched
    candidate lookup relies on instead of the old per-candidate id filter.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "impl.py").write_text("def helper(x):\n    return x\n")
    (repo / "caller.py").write_text(
        "class Decoy:\n"
        "    def helper(self):\n"
        "        return 'decoy, never called'\n"
        "\n"
        "def entry():\n"
        "    return helper(1)\n"
    )
    indexer.index(repo)
    calls = {
        (e["source_qname"], e["target_qname"]): e for e in _edges(store, "CALLS")
    }
    edge = calls[("caller.entry", "impl.helper")]
    assert edge["confidence"] == 0.7
    assert edge["is_inferred"] == 1
    assert edge["inference_reason"] == "name-match"


def test_self_method_call_resolves_within_class(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "svc.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return self.step()\n"
        "    def step(self):\n"
        "        return 1\n"
    )
    indexer.index(repo)
    calls = _edges(store, "CALLS")
    assert len(calls) == 1
    assert calls[0]["source_qname"] == "svc.Service.run"
    assert calls[0]["target_qname"] == "svc.Service.step"
    assert calls[0]["confidence"] == 0.9


# -- READS_ENV -------------------------------------------------------------------


def test_reads_env_converges_across_files(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("import os\nURL = os.getenv('SHARED_URL')\n")
    (repo / "b.py").write_text(
        "import os\n\ndef read():\n    return os.environ['SHARED_URL']\n"
    )
    indexer.index(repo)
    env_nodes = store.conn.execute(
        "SELECT * FROM nodes WHERE type = 'EnvVar' AND name = 'SHARED_URL'"
    ).fetchall()
    assert len(env_nodes) == 1  # repo-global identity
    assert env_nodes[0]["path"] == ""
    reads = _edges(store, "READS_ENV")
    assert {e["path"] for e in reads} == {"a.py", "b.py"}
    assert {e["source_qname"] for e in reads} == {"a", "b.read"}


def test_reads_env_python_fixture(indexer, store, small_app):
    indexer.index(small_app)
    reads = _edges(store, "READS_ENV")
    assert any(
        e["target_name"] == "DATABASE_URL"
        and e["source_qname"] == "app.db.config.get_database_url"
        and e["path"] == "app/db/config.py"
        for e in reads
    )


# -- JavaScript (node_api_app) -----------------------------------------------------


def test_js_extraction_on_node_api_app(indexer, store, node_app):
    indexer.index(node_app)

    fn = _node(store, "Function", "createUser", "src/services/userService.js")
    assert fn is not None
    assert fn["qualified_name"] == "src/services/userService.createUser"
    assert fn["language"] == "javascript"

    # relative require resolution with extension inference
    imports = {
        (e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")
    }
    assert ("src/routes/users", "src/services/userService") in imports
    assert ("src/server", "src/config") in imports

    # express is external metadata, never a node
    import json

    module = store.conn.execute(
        "SELECT metadata_json FROM nodes WHERE type='Module' AND path='src/routes/users.js'"
    ).fetchone()
    assert "express" in json.loads(module["metadata_json"])["external_imports"]

    # Inline callbacks retain the parser's module-level observation, while a
    # named callback is attributed to its precise Function identity.
    calls = {(e["source_qname"], e["target_name"]) for e in _edges(store, "CALLS")}
    assert ("src/routes/users", "createUser") in calls
    assert ("src/routes/users.getUserRoute", "getUser") in calls


def test_js_process_env_reads(indexer, store, node_app):
    indexer.index(node_app)
    reads = _edges(store, "READS_ENV")
    read_vars = {e["target_name"] for e in reads if e["path"] == "src/config.js"}
    assert read_vars == {"PORT", "DATABASE_URL", "LOG_LEVEL"}
    env = _node(store, "EnvVar", "DATABASE_URL")
    assert env["path"] == ""


def test_js_testfile_and_testcases(indexer, store, node_app):
    indexer.index(node_app)
    tf = _node(store, "TestFile", "userService.test.js")
    assert tf is not None
    cases = store.conn.execute(
        "SELECT name FROM nodes WHERE type='TestCase' AND path='src/services/userService.test.js'"
    ).fetchall()
    names = {r["name"] for r in cases}
    assert names == {"creates a user with a name", "returns null for a missing user"}
    # TestFile CONTAINS TestCase
    contains = [
        e for e in _edges(store, "CONTAINS")
        if e["path"] == "src/services/userService.test.js"
        and e["target_name"] in names
    ]
    assert len(contains) == 2
    # test cases call into the service across files, resolved via require
    calls = {
        (e["source_qname"], e["target_qname"]): e for e in _edges(store, "CALLS")
    }
    key = next(k for k in calls if k[1] == "src/services/userService.createUser"
               and "test" in k[0])
    assert calls[key]["confidence"] == 0.9


def test_python_testcase_nodes(indexer, store, small_app):
    indexer.index(small_app)
    tc = _node(store, "TestCase", "test_create_user", "tests/test_users.py")
    assert tc is not None
    assert _node(store, "TestFile", "test_users.py") is not None
    # not double-counted as a Function
    assert _node(store, "Function", "test_create_user") is None


# -- PHP / Bash smoke -----------------------------------------------------------


def test_php_smoke():
    parser = CodeParser()
    parser.begin_run(set())
    php = (
        "<?php\n"
        "function send_welcome($user) {\n"
        "    $url = getenv('APP_URL');\n"
        "    return format_name($user);\n"
        "}\n"
        "function format_name($u) { return trim($u); }\n"
        "class UserService {\n"
        "    public function create($data) { return $this->validate($data); }\n"
        "    private function validate($d) { return $d; }\n"
        "}\n"
    )
    result = parser.parse("src/UserService.php", php)
    assert not result.warnings
    by_type = {}
    for n in result.nodes:
        by_type.setdefault(str(n.type), set()).add(n.qualified_name)
    assert "src/UserService.send_welcome" in by_type["Function"]
    assert "src/UserService.UserService" in by_type["Class"]
    assert "src/UserService.UserService.validate" in by_type["Method"]
    assert "APP_URL" in {n.name for n in result.nodes if str(n.type) == "EnvVar"}
    callees = {
        e.metadata.get("callee") for e in result.edges if str(e.type) == "CALLS"
    }
    assert callees == {"format_name", "validate"}  # $this-> resolved, trim skipped


def test_bash_smoke():
    parser = CodeParser()
    parser.begin_run(set())
    bash = (
        "#!/usr/bin/env bash\n"
        'DB_URL="postgres://localhost"\n'
        "build() {\n"
        "  echo building\n"
        "}\n"
        "function deploy {\n"
        "  build\n"
        "}\n"
        "deploy\n"
    )
    result = parser.parse("scripts/deploy.sh", bash)
    assert not result.warnings
    functions = {n.name for n in result.nodes if str(n.type) == "Function"}
    assert functions == {"build", "deploy"}
    variables = {n.name for n in result.nodes if str(n.type) == "Variable"}
    assert variables == {"DB_URL"}
    callees = {
        e.metadata.get("callee") for e in result.edges if str(e.type) == "CALLS"
    }
    assert callees == {"build", "deploy"}  # echo (external command) skipped


# -- graceful failure -------------------------------------------------------------


def test_broken_file_warns_but_run_completes(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "good.py").write_text("def fine():\n    return 1\n")
    (repo / "bad.py").write_text("def broken(:\n    pass\n")
    stats = indexer.index(repo)
    assert any("bad.py" in w for w in stats.warnings)
    # the broken file still has its generic File node and FTS row
    assert _node(store, "File", "bad.py", "bad.py") is not None
    assert _node(store, "Function", "fine", "good.py") is not None


def test_is_test_file_conventions():
    assert is_test_file("tests/test_users.py")
    assert is_test_file("app/user_test.py")
    assert is_test_file("src/services/userService.test.js")
    assert is_test_file("src/thing.spec.ts")
    assert is_test_file("__tests__/thing.js")
    assert not is_test_file("app/services/user_service.py")
    assert not is_test_file("src/testimonials.js")


# -- Go import resolution (D19) ------------------------------------------------


def test_go_internal_import_resolves_to_package_files(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
    (repo / "main.go").write_text(
        'package main\n\nimport "example.com/foo/util"\n\n'
        "func main() {\n\tutil.Helper()\n}\n"
    )
    (repo / "util").mkdir()
    (repo / "util" / "helper.go").write_text("package util\n\nfunc Helper() {}\n")
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert ("main", "util/helper") in imports


def test_go_internal_import_covers_every_file_in_package(indexer, store, tmp_path: Path):
    """A Go import names a package (directory), which may hold multiple
    files; precision over recall means resolving to every file-Module in
    that directory rather than guessing one (D19)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/foo\n")
    (repo / "main.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/foo/util"\n)\n\n'
        'func main() {\n\tfmt.Println("hi")\n\tutil.Helper()\n}\n'
    )
    (repo / "util").mkdir()
    (repo / "util" / "helper.go").write_text("package util\n\nfunc Helper() {}\n")
    (repo / "util" / "other.go").write_text("package util\n\nfunc Other() {}\n")
    # a package's own test file is never an import target
    (repo / "util" / "helper_test.go").write_text(
        "package util\n\nfunc TestHelper() {}\n"
    )
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert ("main", "util/helper") in imports
    assert ("main", "util/other") in imports
    assert not any(t == "util/helper_test" for _, t in imports)
    # stdlib import stays external, no dangling/guessed node
    module = store.conn.execute(
        "SELECT metadata_json FROM nodes WHERE type='Module' AND path='main.go'"
    ).fetchone()
    import json
    assert "fmt" in json.loads(module["metadata_json"])["external_imports"]
    assert _node(store, "Module", "fmt") is None


def test_go_import_outside_module_stays_external(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/foo\n")
    (repo / "main.go").write_text(
        'package main\n\nimport "github.com/other/pkg"\n\n'
        "func main() {\n\tpkg.Do()\n}\n"
    )
    (repo / "pkg").mkdir()
    (repo / "pkg" / "pkg.go").write_text("package pkg\n\nfunc Do() {}\n")
    indexer.index(repo)
    assert _edges(store, "IMPORTS") == []
    import json
    module = store.conn.execute(
        "SELECT metadata_json FROM nodes WHERE type='Module' AND path='main.go'"
    ).fetchone()
    assert "github.com/other/pkg" in json.loads(module["metadata_json"])["external_imports"]


def test_go_without_go_mod_stays_external(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.go").write_text(
        'package main\n\nimport "example.com/foo/util"\n\n'
        "func main() {\n\tutil.Helper()\n}\n"
    )
    (repo / "util").mkdir()
    (repo / "util" / "helper.go").write_text("package util\n\nfunc Helper() {}\n")
    indexer.index(repo)
    assert _edges(store, "IMPORTS") == []


def test_go_mod_module_directive_ignores_block_comments(indexer, store, tmp_path: Path):
    """A commented-out `module` line inside a `/* ... */` block must not be
    mistaken for the real directive."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text(
        "/*\nmodule example.com/wrong\n*/\nmodule example.com/foo\n\ngo 1.21\n"
    )
    (repo / "main.go").write_text(
        'package main\n\nimport "example.com/foo/util"\n\n'
        "func main() {\n\tutil.Helper()\n}\n"
    )
    (repo / "util").mkdir()
    (repo / "util" / "helper.go").write_text("package util\n\nfunc Helper() {}\n")
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert ("main", "util/helper") in imports


def test_go_import_target_deleted_edge_swept(indexer, store, tmp_path: Path):
    """The importer file itself isn't reparsed, so its previously-resolved
    edge lingers until the orphan-edge sweep removes it once the target node
    is gone (same convergence contract as D16's CALLS pitfall)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/foo\n")
    (repo / "main.go").write_text(
        'package main\n\nimport "example.com/foo/util"\n\n'
        "func main() {\n\tutil.Helper()\n}\n"
    )
    (repo / "util").mkdir()
    (repo / "util" / "helper.go").write_text("package util\n\nfunc Helper() {}\n")
    indexer.index(repo)
    assert ("main", "util/helper") in {
        (e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")
    }
    (repo / "util" / "helper.go").unlink()
    indexer.index(repo)
    assert _edges(store, "IMPORTS") == []


def test_go_import_target_renamed_converges_on_reparse(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/foo\n")
    (repo / "main.go").write_text(
        'package main\n\nimport "example.com/foo/util"\n\n'
        "func main() {\n\tutil.Helper()\n}\n"
    )
    (repo / "util").mkdir()
    (repo / "util" / "helper.go").write_text("package util\n\nfunc Helper() {}\n")
    indexer.index(repo)
    (repo / "util" / "helper.go").unlink()
    (repo / "util" / "helper2.go").write_text("package util\n\nfunc Helper() {}\n")
    # touch the importer so it re-parses and recomputes its import edges
    (repo / "main.go").write_text(
        'package main\n\n// re-touched\nimport "example.com/foo/util"\n\n'
        "func main() {\n\tutil.Helper()\n}\n"
    )
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert ("main", "util/helper2") in imports
    assert not any(t == "util/helper" for _, t in imports)


# -- Java import resolution (D19) ----------------------------------------------


def test_java_internal_import_resolves_via_src_main_java(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pkg = repo / "src" / "main" / "java" / "com" / "example"
    (pkg / "util").mkdir(parents=True)
    (pkg / "util" / "Helper.java").write_text(
        "package com.example.util;\n\npublic class Helper {\n"
        "    public static void greet() {}\n}\n"
    )
    (pkg / "App.java").write_text(
        "package com.example;\n\nimport com.example.util.Helper;\n\n"
        "public class App {\n    void run() { Helper.greet(); }\n}\n"
    )
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert (
        "src/main/java/com/example/App",
        "src/main/java/com/example/util/Helper",
    ) in imports


def test_java_wildcard_import_covers_every_file_in_package(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pkg = repo / "src" / "main" / "java" / "com" / "example"
    (pkg / "util").mkdir(parents=True)
    (pkg / "util" / "Helper.java").write_text(
        "package com.example.util;\n\npublic class Helper {}\n"
    )
    (pkg / "util" / "Other.java").write_text(
        "package com.example.util;\n\npublic class Other {}\n"
    )
    (pkg / "App.java").write_text(
        "package com.example;\n\nimport com.example.util.*;\n\n"
        "public class App {\n    void run() { new Helper(); }\n}\n"
    )
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert ("src/main/java/com/example/App", "src/main/java/com/example/util/Helper") in imports
    assert ("src/main/java/com/example/App", "src/main/java/com/example/util/Other") in imports


def test_java_static_import_resolves_to_declaring_class(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pkg = repo / "src" / "main" / "java" / "com" / "example"
    (pkg / "util").mkdir(parents=True)
    (pkg / "util" / "Constants.java").write_text(
        "package com.example.util;\n\npublic class Constants {\n"
        "    public static final int MAX = 10;\n}\n"
    )
    (pkg / "App.java").write_text(
        "package com.example;\n\nimport static com.example.util.Constants.MAX;\n\n"
        "public class App {\n    int limit() { return MAX; }\n}\n"
    )
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert (
        "src/main/java/com/example/App",
        "src/main/java/com/example/util/Constants",
    ) in imports


def test_java_ambiguous_source_roots_stay_external(indexer, store, tmp_path: Path):
    """Two distinct `src/main/java` trees (a multi-module layout) make the
    conventional root ambiguous; imports stay external rather than guessing
    which tree the caller means (D19)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    backend = repo / "backend" / "src" / "main" / "java" / "com" / "example"
    backend.mkdir(parents=True)
    (backend / "App.java").write_text(
        "package com.example;\n\nimport com.other.Thing;\n\n"
        "public class App {\n    void run() {}\n}\n"
    )
    frontend = repo / "frontend" / "src" / "main" / "java" / "com" / "other"
    frontend.mkdir(parents=True)
    (frontend / "Thing.java").write_text(
        "package com.other;\n\npublic class Thing {}\n"
    )
    indexer.index(repo)
    assert _edges(store, "IMPORTS") == []
    import json
    module = store.conn.execute(
        "SELECT metadata_json FROM nodes WHERE type='Module' "
        "AND path='backend/src/main/java/com/example/App.java'"
    ).fetchone()
    assert "com.other.Thing" in json.loads(module["metadata_json"])["external_imports"]


def test_java_without_conventional_root_stays_external(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "Other.java").write_text(
        "package other;\n\npublic class Other {}\n"
    )
    (repo / "src" / "App.java").write_text(
        "import other.Other;\n\npublic class App {\n    void run() {}\n}\n"
    )
    indexer.index(repo)
    assert _edges(store, "IMPORTS") == []


def test_java_source_root_marker_matches_path_segments_not_substring(
    indexer, store, tmp_path: Path,
):
    """A directory that merely *ends* in `...src` immediately followed by
    `main/java/` (e.g. a vendored `thirdparty-src/main/java/` tree) must not
    be mistaken for a real `src/main/java` root and must not poison
    detection of the one legitimate root by looking ambiguous."""
    from repobrain.parsers.code_treesitter import _detect_java_source_roots

    known = frozenset({
        "backend/src/main/java/com/example/App.java",
        "thirdparty-src/main/java/com/other/Thing.java",
    })
    roots = _detect_java_source_roots(known)
    assert roots["main"] == "backend/src/main/java/"

    repo = tmp_path / "repo"
    repo.mkdir()
    pkg = repo / "backend" / "src" / "main" / "java" / "com" / "example"
    (pkg / "util").mkdir(parents=True)
    (pkg / "util" / "Helper.java").write_text(
        "package com.example.util;\n\npublic class Helper {}\n"
    )
    (pkg / "App.java").write_text(
        "package com.example;\n\nimport com.example.util.Helper;\n\n"
        "public class App {\n    void run() { new Helper(); }\n}\n"
    )
    (repo / "thirdparty-src" / "main" / "java" / "com" / "other").mkdir(parents=True)
    (
        repo / "thirdparty-src" / "main" / "java" / "com" / "other" / "Thing.java"
    ).write_text("package com.other;\n\npublic class Thing {}\n")
    indexer.index(repo)
    imports = {(e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")}
    assert (
        "backend/src/main/java/com/example/App",
        "backend/src/main/java/com/example/util/Helper",
    ) in imports


def test_java_import_target_deleted_edge_swept(indexer, store, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pkg = repo / "src" / "main" / "java" / "com" / "example"
    (pkg / "util").mkdir(parents=True)
    (pkg / "util" / "Helper.java").write_text(
        "package com.example.util;\n\npublic class Helper {}\n"
    )
    (pkg / "App.java").write_text(
        "package com.example;\n\nimport com.example.util.Helper;\n\n"
        "public class App {\n    void run() { new Helper(); }\n}\n"
    )
    indexer.index(repo)
    assert ("src/main/java/com/example/App", "src/main/java/com/example/util/Helper") in {
        (e["source_qname"], e["target_qname"]) for e in _edges(store, "IMPORTS")
    }
    (pkg / "util" / "Helper.java").unlink()
    indexer.index(repo)
    assert _edges(store, "IMPORTS") == []
    assert not is_test_file("src/testimonials.js")
