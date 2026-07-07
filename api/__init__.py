"""graphRAG Web Console backend (FastAPI). Built in Track 2 — see DESIGN.md §10.1.

BA0: the app skeleton + frozen-contract OpenAPI + auth placeholder; BA1+
mount domain routers on ``create_app()``."""

from api.app import create_app

__all__ = ["create_app"]
