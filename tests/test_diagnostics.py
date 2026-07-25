import io
import json

import pytest
from click.testing import CliRunner

from repobrain.cli import main
from repobrain.diagnostics import LOG_LEVEL_ENV, configure_logging
from repobrain.mcp_server import RepoBrainTools, run_server


@pytest.fixture(autouse=True)
def _reset_diagnostics(monkeypatch):
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    configure_logging()
    yield
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    configure_logging()


def test_cli_logging_is_silent_by_default(tmp_path):
    result = CliRunner().invoke(main, ["init", str(tmp_path)])

    assert result.exit_code == 0
    assert result.stderr == ""


def test_verbose_cli_emits_structured_stderr_only(tmp_path):
    result = CliRunner().invoke(main, ["--verbose", "init", str(tmp_path)])

    assert result.exit_code == 0
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert records
    assert records[0]["event"] == "cli.command.start"
    assert records[0]["command"] == "init"
    assert records[0]["level"] == "INFO"
    assert "cli.command.start" not in result.stdout


def test_mcp_query_logs_outcome_without_query_payload(small_app):
    tools = RepoBrainTools(small_app)
    tools.index_repo()
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    secret_query = "CANARY-do-not-log-query-payload"

    result = tools.search_project(secret_query, auto_index=False)

    assert result["status"] == "ok"
    output = stream.getvalue()
    assert secret_query not in output
    record = json.loads(output)
    assert record["event"] == "mcp.query.completed"
    assert record["status"] == "ok"
    assert record["auto_index"] is False


def test_direct_mcp_launch_uses_env_and_never_logs_to_stdout(
    monkeypatch, capsys, tmp_path
):
    class _Server:
        def run(self, *, transport):
            assert transport == "stdio"

    monkeypatch.setenv(LOG_LEVEL_ENV, "INFO")
    monkeypatch.setattr("repobrain.mcp_server.create_server", lambda root: _Server())

    run_server(tmp_path)

    captured = capsys.readouterr()
    assert captured.out == ""
    record = json.loads(captured.err)
    assert record["event"] == "mcp.server.start"
    assert record["transport"] == "stdio"


def test_mcp_launch_preserves_existing_cli_logging(monkeypatch, tmp_path):
    class _Server:
        def run(self, *, transport):
            assert transport == "stdio"

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    monkeypatch.setattr("repobrain.mcp_server.create_server", lambda root: _Server())

    run_server(tmp_path)

    assert json.loads(stream.getvalue())["event"] == "mcp.server.start"
