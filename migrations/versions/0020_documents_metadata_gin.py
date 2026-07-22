"""SS1b: GIN index over documents.metadata for filterable-attribute facets.

DR-010 rule 2 declares per-project ``metadata_schema`` attributes with
``filterable: true``; review rule 8 splits "show metadata with results" from
"search BY metadata" — the search half needs an index. The documents list
endpoint filters by JSONB CONTAINMENT (``metadata @> {"context":
{"attributes": {name: value}}}``), and ONE ``jsonb_path_ops`` GIN index
serves every attribute key, present and future — a project adding a
filterable field needs no further DDL. (``jsonb_path_ops`` supports exactly
the ``@>`` operator this path uses, at a smaller index size than the default
opclass.)
"""

from __future__ import annotations

from alembic import op

revision = "0020_documents_metadata_gin"
down_revision = "0019_builds_parent_build_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "documents_metadata_gin",
        "documents",
        ["metadata"],
        postgresql_using="gin",
        postgresql_ops={"metadata": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index("documents_metadata_gin", table_name="documents")
