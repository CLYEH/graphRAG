"""BA1: projects + sources — the control-plane registry.

NOT build-scoped (like builds/review_ledger): the control plane the build
lifecycle hangs off, keyed by the stable `projects.name` used in API paths and
store scoping. `sources` is a genuine child of `projects`, so it carries a real
FK with ON DELETE CASCADE (deleting a project removes its sources) rather than
the bare-text `project` the build-scoped projection tables use. Shapes mirror
the frozen contract Project/Source schemas; the ingest/build triggers and the
`idempotency_keys` store land in BA1b/BA2.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_projects_sources"
down_revision = "0006_ontology_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column(
            "config",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("name <> ''", name="projects_name_nonempty"),
    )
    op.create_table(
        "sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project",
            sa.Text,
            sa.ForeignKey("projects.name", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text),
        sa.Column("uri", sa.Text, nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "added_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("uri <> ''", name="sources_uri_nonempty"),
    )
    op.create_index("sources_by_project", "sources", ["project"])


def downgrade() -> None:
    op.drop_index("sources_by_project", table_name="sources")
    op.drop_table("sources")
    op.drop_table("projects")
