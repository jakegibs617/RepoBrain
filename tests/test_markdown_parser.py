from repobrain.parsers.markdown_parser import MarkdownParser

DOC = """\
# Title

Intro paragraph linking to [the plan](docs/plan.md).

## Setup

Run this:

```bash
export DATABASE_URL=sqlite:///local.db
```

### Details

More depth here.

## Tasks

- TODO: add validation
- [ ] FIXME: default path is wrong
- a normal list item
"""


def parse(content=DOC, path="README.md"):
    return MarkdownParser().parse(path, content)


def _nodes(result, type_):
    return [n for n in result.nodes if n.type == type_]


def test_document_node():
    result = parse()
    docs = _nodes(result, "MarkdownDocument")
    assert len(docs) == 1
    doc = docs[0]
    assert doc.name == "README.md"
    assert doc.metadata["title"] == "Title"
    assert doc.start_line == 1
    assert doc.end_line == DOC.count("\n")


def test_heading_hierarchy_and_spans():
    result = parse()
    sections = {n.name: n for n in _nodes(result, "MarkdownSection")}
    assert set(sections) == {"Title", "Setup", "Details", "Tasks"}

    title, setup, details, tasks = (
        sections["Title"], sections["Setup"], sections["Details"], sections["Tasks"]
    )
    assert title.metadata["level"] == 1
    assert setup.metadata["level"] == 2
    # spans: Setup runs until Tasks starts; Details nested inside Setup
    assert setup.start_line < details.start_line <= setup.end_line
    assert tasks.start_line == setup.end_line + 1
    assert title.end_line == DOC.count("\n")

    # CONTAINS edges follow the hierarchy: doc -> Title -> Setup -> Details
    doc = _nodes(result, "MarkdownDocument")[0]
    contains = {(e.source_node_id, e.target_node_id) for e in result.edges if e.type == "CONTAINS"}
    assert (doc.id, title.id) in contains
    assert (title.id, setup.id) in contains
    assert (setup.id, details.id) in contains
    assert (title.id, tasks.id) in contains


def test_links_extracted():
    result = parse()
    doc = _nodes(result, "MarkdownDocument")[0]
    links = doc.metadata["links"]
    assert any(l["href"] == "docs/plan.md" and l["text"] == "the plan" for l in links)
    # link is attributed to the section containing it (Title intro)
    title = next(n for n in _nodes(result, "MarkdownSection") if n.name == "Title")
    assert any(l["href"] == "docs/plan.md" for l in title.metadata["links"])


def test_code_blocks_extracted():
    result = parse()
    doc = _nodes(result, "MarkdownDocument")[0]
    blocks = doc.metadata["code_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["language"] == "bash"
    assert "DATABASE_URL" in blocks[0]["content"]
    setup = next(n for n in _nodes(result, "MarkdownSection") if n.name == "Setup")
    assert len(setup.metadata["code_blocks"]) == 1


def test_todo_fixme_tasks():
    result = parse()
    task_names = [n.name for n in _nodes(result, "Task")]
    assert len(task_names) == 2
    assert any("add validation" in t for t in task_names)
    assert any("default path is wrong" in t for t in task_names)
    # tasks live under the Tasks section
    tasks_section = next(n for n in _nodes(result, "MarkdownSection") if n.name == "Tasks")
    task_edges = [
        e for e in result.edges
        if e.type == "CONTAINS" and e.source_node_id == tasks_section.id
        and any(n.id == e.target_node_id and n.type == "Task" for n in result.nodes)
    ]
    assert len(task_edges) == 2


def test_section_fts_rows():
    result = parse()
    by_name = {r.name: r for r in result.fts_rows}
    assert "Setup" in by_name
    assert "DATABASE_URL" in by_name["Setup"].content


def test_duplicate_headings_get_unique_ids():
    doc = "# A\n\n## Same\n\ntext\n\n## Same\n\nother\n"
    result = parse(doc)
    sections = _nodes(result, "MarkdownSection")
    ids = [n.id for n in sections]
    assert len(ids) == len(set(ids))


def test_parse_result_deterministic():
    a, b = parse(), parse()
    assert [n.id for n in a.nodes] == [n.id for n in b.nodes]
    assert [e.id for e in a.edges] == [e.id for e in b.edges]
