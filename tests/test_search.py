from repobrain.retrieval.keyword import search


def test_search_database_returns_fixture_hits(indexer, store, small_app):
    indexer.index(small_app)
    results = search(store, "database", limit=10)
    assert results
    paths = [r.path for r in results]
    assert "app/db/config.py" in paths or "README.md" in paths
    hit = results[0]
    assert hit.snippet or hit.reasons  # every result explains itself
    assert all(r.score >= 0 or r.reasons for r in results)
    # snippets come back for full-text matches
    assert any("database" in r.snippet.lower() or "database" in r.name.lower()
               for r in results if r.snippet)


def test_name_match_outranks_content_match(indexer, store, docs_app):
    indexer.index(docs_app)
    # "Database" is a section heading in README.md; other files merely
    # mention the word in prose.
    results = search(store, "Database", limit=10)
    assert results
    top = results[0]
    assert top.name.lower() == "database"
    assert top.node_type == "MarkdownSection"
    assert "exact name match" in top.reasons
    # content-only matches rank below
    content_only = [r for r in results if "exact name match" not in r.reasons]
    assert content_only
    assert all(top.score > r.score for r in content_only)


def test_search_type_filter(indexer, store, docs_app):
    indexer.index(docs_app)
    results = search(store, "users", limit=10, node_type="File")
    assert results
    assert all(r.node_type == "File" for r in results)


def test_search_returns_line_ranges_for_sections(indexer, store, docs_app):
    indexer.index(docs_app)
    results = search(store, "sqlite", limit=10)
    section_hits = [r for r in results if r.node_type == "MarkdownSection"]
    assert section_hits
    assert all(r.start_line and r.end_line for r in section_hits)


def test_search_handles_fts_special_chars(indexer, store, small_app):
    indexer.index(small_app)
    # must not raise an FTS5 syntax error
    assert search(store, 'user" OR (', limit=5) is not None
    assert search(store, "", limit=5) == []
