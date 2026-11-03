"""CLI entrypoint for the Zocrates Model Context Protocol (MCP) server.

Runs over stdio transport for direct integration with external LLM clients
(Claude Desktop, Cursor, Zed, Pi, Continue, Roo Code).

STRICT INVARIANT:
Stdout is reserved strictly for JSON-RPC 2.0 protocol frames.
All logging, diagnostic messages, and tracebacks MUST route exclusively to sys.stderr.
"""

import logging
import sys

from zocrates.mcp.server import mcp


def main() -> None:
    """Run the FastMCP server over stdio transport."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [zocrates-mcp] %(message)s",
        force=True,
    )

    try:
        mcp.run(transport="stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        sys.stderr.write("Zocrates MCP server shut down cleanly.\n")
        sys.stderr.flush()
        sys.exit(0)


if __name__ == "__main__":
    main()
