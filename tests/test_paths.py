"""Why: ``safe_project_subdir`` is the ONE filesystem containment guard the managed
upload corpus (``api.routers.uploads``) and the on-disk eval config
(``api.workers.build_worker``) both lean on. Project names are only length-validated,
so a name that normalizes back under the base (``foo/../bar``), an absolute path to a
child, or an OS-specific separator must FAIL CLOSED (None) — otherwise it aliases a
DIFFERENT project's corpus/config instead of escaping outright, a silent cross-project
read/write. These pin the single-relative-component rule and the happy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.paths import safe_project_subdir


def test_plain_name_resolves_to_a_direct_child(tmp_path: Path) -> None:
    got = safe_project_subdir(tmp_path, "nmmst")
    assert got == (tmp_path / "nmmst").resolve()


@pytest.mark.parametrize(
    "project",
    [
        "..",  # the classic escape
        ".",  # the base itself, not a child
        "",  # empty component
        "a/b",  # a nested path, not one component
        "foo/../bar",  # normalizes to base/bar — ALIASES project 'bar' (the finding)
        "../sibling",  # escapes to a sibling of base
        "a\\b",  # a Windows separator (a literal char on POSIX → aliases on Windows)
        "..\\evil",  # backslash traversal
    ],
)
def test_alias_or_traversal_names_fail_closed(tmp_path: Path, project: str) -> None:
    # None so the caller maps it to a 400 / failed job — never a resolved path that
    # reads or writes another project's tree.
    assert safe_project_subdir(tmp_path, project) is None


def test_absolute_child_path_is_rejected(tmp_path: Path) -> None:
    # An absolute path naming a child of base (``base / "/abs/base/bar"`` replaces to
    # the absolute operand) would pass the old parent-equality check while aliasing
    # 'bar'. A single relative component is required, so an absolute name fails closed.
    child = (tmp_path / "bar").resolve()
    assert safe_project_subdir(tmp_path, str(child)) is None
