"""BA2b: builds.project → projects.name FK (RESTRICT).

Closes the registry gap deferred from BA1a: a build cannot exist without its
project, and a project with builds cannot be deleted (the DB backstops
delete_project's count + FOR UPDATE lock, closing the count-then-delete TOCTOU
structurally). RESTRICT not CASCADE — deleting a project must go through the
C9/BA8 multi-store build sweep, never silently drop builds.

Safe to add without a data fix: migration 0007 already backfilled `projects`
from every project-keyed table (builds included), so every existing
builds.project already has a matching projects row.
"""

from __future__ import annotations

from alembic import op

revision = "0010_builds_project_fk"
down_revision = "0009_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_foreign_key(
        "builds_project_fkey",
        "builds",
        "projects",
        ["project"],
        ["name"],
        ondelete="RESTRICT",
    )
    # supporting index for the FK — the partial one_active_build only covers
    # status='active', so the RESTRICT check on a projects DELETE would seq-scan
    op.create_index("builds_by_project", "builds", ["project"])


def downgrade() -> None:
    op.drop_index("builds_by_project", table_name="builds")
    op.drop_constraint("builds_project_fkey", "builds", type_="foreignkey")
