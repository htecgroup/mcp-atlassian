import json
import os
import selectors
import subprocess
import time


def _build_probe_payload() -> str:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "homebrew-probe", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    return "\n".join(json.dumps(message) for message in messages) + "\n"


def test_stdio_homebrew_probe_exits_after_stdin_close() -> None:
    """The server must fully answer in-flight requests before it exits on stdin close.

    This mirrors a real MCP client (e.g. Homebrew's health-check probe): requests
    are written and their responses are awaited, and only then is stdin closed.
    Closing stdin immediately after writing (before responses arrive) races the
    transport's own "cancel in-flight handlers when the transport closes" behavior
    (see `mcp.server.lowlevel.server.Server.run`'s task-group teardown) — that
    race is inherent to the underlying MCP SDK regardless of how fast this server
    itself responds, so the probe must wait for the response before closing stdin.
    """
    env = os.environ.copy()
    env.update(
        {
            "JIRA_URL": "https://example.atlassian.net",
            "JIRA_USERNAME": "user@example.com",
            "JIRA_API_TOKEN": "x",
        }
    )

    proc = subprocess.Popen(
        ["uv", "run", "mcp-atlassian"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        proc.stdin.write(_build_probe_payload())
        proc.stdin.flush()

        jsonrpc_lines: list[str] = []
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 15
        saw_tools_response = False
        try:
            while time.monotonic() < deadline and not saw_tools_response:
                if not selector.select(timeout=0.5):
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                if line.startswith('{"jsonrpc"'):
                    jsonrpc_lines.append(line)
                    if '"id":2' in line:
                        saw_tools_response = True
        finally:
            selector.close()

        assert saw_tools_response, (
            f"Never received a tools/list response: {jsonrpc_lines}"
        )
        assert any('"id":1' in line for line in jsonrpc_lines), jsonrpc_lines
        assert any('"id":2' in line and '"tools"' in line for line in jsonrpc_lines), (
            jsonrpc_lines
        )

        proc.stdin.close()
        returncode = proc.wait(timeout=15)
        stderr_tail = (
            proc.stderr.read()[:1000] if returncode != 0 and proc.stderr else ""
        )
        assert returncode == 0, stderr_tail
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
