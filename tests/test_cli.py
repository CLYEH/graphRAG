"""Unit test for the CLI entrypoint (keeps the packaged script covered)."""

from __future__ import annotations

import pytest

from cli.main import main


def test_main_prints_placeholder(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert "graphrag CLI" in capsys.readouterr().out
