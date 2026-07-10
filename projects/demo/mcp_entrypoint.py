"""MCP entry point for the demo project (§9/§12): one server per project.

Transports (🔧, selected at run time; C8b):

    uv run python projects/demo/mcp_entrypoint.py                    # stdio
    uv run python projects/demo/mcp_entrypoint.py --transport http   # streamable HTTP
                                                                     # on core.config's
                                                                     # mcp_http_host:port
"""

import argparse
from pathlib import Path

from core.mcp.server import TRANSPORTS, build_server, run_server

_HERE = Path(__file__).resolve().parent

server = build_server(project=_HERE.name, config_path=_HERE / "config.yaml")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=sorted(TRANSPORTS), default="stdio")
    run_server(server, parser.parse_args().transport)
