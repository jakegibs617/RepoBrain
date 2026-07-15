"""Real-process MCP protocol coverage over RepoBrain's supported stdio transport."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest

mcp = pytest.importorskip(
    "mcp", reason="protocol integration tests require the optional repobrain[mcp] extra"
)
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import LATEST_PROTOCOL_VERSION

from repobrain import agent_install
from repobrain.agent_install import mcp_server_entry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "index_repo", "search_project", "explain_project", "project_brief",
    "change_context", "explain_file", "find_symbol", "trace_symbol",
    "trace_config", "trace_data_flow", "impact_analysis", "co_change",
    "churn_hotspots", "ownership", "docs_for_code", "code_for_docs",
    "write_agent_memory", "read_agent_memory", "verify_agent_memory",
}


def _local_server(root: Path) -> StdioServerParameters:
    """Launch the real CLI entry with an argument array, never a shell string."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "repobrain.cli", "mcp", "--path", str(root)],
        cwd=PROJECT_ROOT,
    )


@asynccontextmanager
async def _session(parameters: StdioServerParameters, *, timeout: float = 20.0):
    """Bound startup, requests, and SDK-owned subprocess teardown."""
    with tempfile.TemporaryFile(mode="w+") as stderr:
        with anyio.fail_after(timeout):
            async with stdio_client(parameters, errlog=stderr) as streams:
                async with ClientSession(
                    *streams, read_timeout_seconds=timedelta(seconds=5),
                ) as session:
                    yield session, stderr


def _payload(result) -> dict:
    """Decode FastMCP's JSON text content without bypassing the protocol."""
    assert result.isError is False
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


class _RawStdioHarness:
    """Small bounded JSON-lines harness for malformed/lifecycle edge cases."""

    _EOF = object()

    def __init__(self, command: str, args: list[str], *, cwd: Path = PROJECT_ROOT):
        self._stderr = tempfile.TemporaryFile()
        self._stderr_content: bytes | None = None
        self.process = subprocess.Popen(
            [command, *args],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
        )
        self._lines: queue.Queue[bytes | object] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(self._EOF)

    def send(self, message: dict | str) -> None:
        assert self.process.stdin is not None
        data = message if isinstance(message, str) else json.dumps(message)
        self.process.stdin.write((data + "\n").encode())
        self.process.stdin.flush()

    def read_json(self, timeout: float = 3.0) -> dict:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for MCP stdout") from exc
        if line is self._EOF:
            raise EOFError("MCP subprocess closed stdout")
        return json.loads(line)

    def close(self, timeout: float = 3.0) -> int:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                returncode = self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                returncode = self.process.wait(timeout=timeout)
        self._reader.join(timeout=timeout)
        self._stderr.seek(0)
        self._stderr_content = self._stderr.read()
        self._stderr.close()
        return returncode

    def stderr(self) -> str:
        if self._stderr_content is None:
            raise RuntimeError("close the MCP subprocess before reading stderr")
        return self._stderr_content.decode(errors="replace")


def test_real_stdio_protocol_negotiates_discovers_tools_and_returns_envelopes(
    small_app, tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()

    async def scenario():
        async with _session(_local_server(small_app)) as (session, _):
            initialized = await session.initialize()
            assert initialized.protocolVersion == LATEST_PROTOCOL_VERSION
            assert initialized.serverInfo.name == "RepoBrain"
            assert initialized.capabilities.tools is not None

            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS
            assert all(tool.inputSchema["type"] == "object" for tool in listed.tools)

            missing_db = await session.call_tool("search_project", {"query": "user"})
            assert missing_db.isError is True
            assert "call index_repo first" in missing_db.content[0].text

            indexed = _payload(await session.call_tool("index_repo", {"path": "."}))
            assert indexed["status"] == "ok"
            assert indexed["files_scanned"] > 0

            found = _payload(await session.call_tool(
                "find_symbol", {"name": "UserRepository", "exact": True},
            ))
            assert found["status"] == "ok"
            assert found["symbols"][0]["path"].endswith("repositories/user_repository.py")

            not_found = _payload(await session.call_tool(
                "explain_file", {"path": "does-not-exist.py"},
            ))
            assert not_found["status"] == "not_found"
            assert not_found["file"] is None

            domain_error = _payload(await session.call_tool("change_context", {}))
            assert domain_error["status"] == "error"
            assert domain_error["changes"] == []

            invalid = await session.call_tool(
                "trace_data_flow", {"start": "anything", "direction": "sideways"},
            )
            assert invalid.isError is True
            assert "direction must be in, out, or both" in invalid.content[0].text

            unknown = await session.call_tool("not_a_repobrain_tool", {})
            assert unknown.isError is True
            assert "not_a_repobrain_tool" in unknown.content[0].text

            escaped = await session.call_tool("index_repo", {"path": str(outside)})
            assert escaped.isError is True
            assert "scoped to" in escaped.content[0].text

        # The official SDK parsed every stdout line as protocol throughout the
        # lifecycle; FastMCP diagnostics were independently captured on stderr.

    anyio.run(scenario)


def test_freshness_fails_closed_over_stdio_without_leaking_stale_facts(small_app):
    async def scenario():
        async with _session(_local_server(small_app)) as (session, _):
            await session.initialize()
            _payload(await session.call_tool("index_repo", {}))

            fresh = small_app / "fresh_transport_symbol.py"
            fresh.write_text("def transport_fresh_symbol():\n    return True\n", encoding="utf-8")
            repaired = _payload(await session.call_tool(
                "find_symbol", {"name": "transport_fresh_symbol", "exact": True},
            ))
            assert repaired["status"] == "ok"
            assert repaired["freshness"]["status"] == "reindexed"
            assert repaired["symbols"][0]["path"] == "fresh_transport_symbol.py"

            secret = "transport_stale_secret"
            readme = small_app / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8") + f"\n{secret}\n")
            opted_out = _payload(await session.call_tool(
                "search_project", {"query": secret, "auto_index": False},
            ))
            assert opted_out["status"] == "blocked"
            assert opted_out["freshness"]["reason"] == "auto_index_disabled"
            assert "results" not in opted_out
            assert secret not in json.dumps(opted_out)

            _payload(await session.call_tool("index_repo", {}))
            for number in range(11):
                (small_app / f"oversized-{number}.py").write_text(
                    f"transport_oversized_{number} = True\n", encoding="utf-8",
                )
            oversized = _payload(await session.call_tool(
                "search_project", {"query": "transport_oversized"},
            ))
            assert oversized["status"] == "blocked"
            assert oversized["freshness"]["reason"] == "threshold_exceeded"
            assert "results" not in oversized

    anyio.run(scenario)


def test_raw_protocol_recovers_from_malformed_input_accepts_cancellation_and_eof(
    small_app,
):
    harness = _RawStdioHarness(
        sys.executable,
        ["-m", "repobrain.cli", "mcp", "--path", str(small_app)],
    )
    try:
        harness.send("{bad json")
        malformed = harness.read_json()
        assert malformed["method"] == "notifications/message"
        assert malformed["params"]["level"] == "error"

        harness.send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "repobrain-test", "version": "1"},
            },
        })
        initialized = harness.read_json()
        assert initialized["id"] == 1
        assert initialized["result"]["capabilities"]["tools"] == {"listChanged": False}
        harness.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        harness.send({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "not-running", "reason": "lifecycle probe"},
        })
        harness.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = harness.read_json()
        assert listed["id"] == 2
        assert {tool["name"] for tool in listed["result"]["tools"]} == EXPECTED_TOOLS
    finally:
        returncode = harness.close()

    assert returncode == 0
    assert "validation error" in harness.stderr().lower()


def test_stdio_harness_times_out_and_cleans_up_real_server(small_app):
    harness = _RawStdioHarness(
        sys.executable,
        ["-m", "repobrain.cli", "mcp", "--path", str(small_app)],
    )
    with pytest.raises(TimeoutError, match="MCP stdout"):
        harness.read_json(timeout=0.05)
    assert harness.close() == 0
    assert harness.process.poll() == 0


def test_invalid_repository_path_exits_without_polluting_protocol_stdout(tmp_path):
    missing = tmp_path / "missing repository"
    completed = subprocess.run(
        [sys.executable, "-m", "repobrain.cli", "mcp", "--path", str(missing)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=5,
    )
    assert completed.returncode != 0
    assert completed.stdout == b""
    assert b"does not exist" in completed.stderr


def test_built_wheel_launches_over_installed_argument_array_when_cached(
    small_app, tmp_path, monkeypatch,
):
    """Offline isolated smoke; skip with evidence when uv's cache is incomplete."""
    uvx = shutil.which("uvx")
    if uvx is None:
        pytest.skip("isolated MCP smoke requires uvx")
    try:
        import hatchling  # noqa: F401
    except ImportError:
        pytest.skip("isolated MCP smoke requires locally installed hatchling")

    dist = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable, "-c",
            "from hatchling.build import build_wheel; import sys; build_wheel(sys.argv[1])",
            str(dist),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
    wheel = next(dist.glob("repobrain-*.whl"))
    monkeypatch.setattr(
        agent_install, "MCP_PACKAGE", f"repobrain[mcp] @ {wheel.resolve().as_uri()}",
    )
    spaced_root = tmp_path / "repository with spaces"
    small_app.rename(spaced_root)
    entry = mcp_server_entry(spaced_root)
    assert entry["command"] == "uvx"
    assert entry["args"][-2:] == ["--path", str(spaced_root.resolve())]

    env = {**os.environ, "UV_OFFLINE": "1", "UV_PYTHON": sys.executable}
    probe = subprocess.run(
        [uvx, "--from", entry["args"][1], "repobrain", "--help"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode:
        evidence = (probe.stderr or probe.stdout).strip().splitlines()
        detail = " | ".join(evidence[-3:]) if evidence else "uvx resolution failed"
        if any(marker in detail.lower() for marker in (
            "not found in the cache", "not found in cache", "offline",
            "network connectivity is disabled",
        )):
            pytest.skip("isolated MCP smoke prerequisites unavailable offline: " + detail)
        pytest.fail("isolated built-wheel MCP launch failed: " + detail)

    async def scenario():
        parameters = StdioServerParameters(
            command=entry["command"], args=entry["args"], cwd=PROJECT_ROOT,
            env={"UV_OFFLINE": "1", "UV_PYTHON": sys.executable},
        )
        async with _session(parameters, timeout=60) as (session, _):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "RepoBrain"
            assert {tool.name for tool in (await session.list_tools()).tools} == EXPECTED_TOOLS
            assert _payload(await session.call_tool("index_repo", {}))["status"] == "ok"

    anyio.run(scenario)
