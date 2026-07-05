"""MCP entry point for the demo project (§9/§12): one server per project.

Run over stdio (the 🔧 default transport):

    uv run python projects/demo/mcp_entrypoint.py
"""

from pathlib import Path

from core.mcp.server import build_server

_HERE = Path(__file__).resolve().parent

server = build_server(project=_HERE.name, config_path=_HERE / "config.yaml")

if __name__ == "__main__":
    server.run()
