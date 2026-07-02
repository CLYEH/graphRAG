"""graphRAG CLI entrypoint (skeleton).

Real subcommands (new/ingest/build/activate/rollback/serve/...) land in Track 1;
see DESIGN.md §10 and §14. This stub keeps the ``graphrag`` script installable so
the harness (packaging, entrypoint) is verified from day one.
"""

from __future__ import annotations

import sys


def main() -> None:
    """Placeholder entrypoint until CLI subcommands are implemented."""
    sys.stdout.write("graphrag CLI — not yet implemented. See docs/DESIGN.md.\n")


if __name__ == "__main__":
    main()
