"""Unit tests for the CLI entrypoint (keeps the packaged script covered).

Why: the console script is the §14 operator surface — bad usage must exit 2
with usage text (argparse), and `--help` must name every lifecycle verb, so
an operator can discover the surface without the source."""

from __future__ import annotations

import pytest

from cli.main import main


def test_no_arguments_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["graphrag"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2  # argparse usage error, not a crash
    assert "usage:" in capsys.readouterr().err


def test_help_names_the_lifecycle_verbs(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["graphrag", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for verb in ("builds", "activate", "rollback", "diff", "prune"):
        assert verb in out
