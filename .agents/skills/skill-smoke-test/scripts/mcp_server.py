"""Local HTTP MCP server for manual Skill smoke tests."""

from __future__ import annotations

from fastmcp import FastMCP

server: FastMCP[None] = FastMCP("skill-smoke")


@server.tool
def echo(message: str) -> dict[str, object]:
    """Return a message through the MCP transport."""
    return {"ok": True, "kind": "skill-mcp", "message": message}


@server.tool
def add(left: int, right: int) -> dict[str, object]:
    """Add two integers through the MCP transport."""
    return {"ok": True, "kind": "skill-mcp", "result": left + right}


if __name__ == "__main__":
    server.run(
        transport="http",
        host="127.0.0.1",
        port=8877,
        show_banner=False,
        log_level="warning",
    )
