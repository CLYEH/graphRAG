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
    for verb in ("builds", "activate", "rollback", "diff", "eval", "prune"):
        assert verb in out


def test_prune_refuses_a_negative_window_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The remedy REST names must not end in a traceback (QA7/D7).

    A project that has ever built cannot be deleted until its builds are
    pruned, and REST says so — which makes `graphrag prune` the surface an
    operator reaches by following instructions. It answered an invalid window
    with an uncaught ValueError. It now exits 1 with the same REFUSED prefix
    the sibling `diff` branch uses, so a script can gate on it.

    (keep=0 is deliberately NOT refused any more — that is the whole point of
    the fix; the guard it replaced was redundant with the keepers union.)
    """
    import argparse
    import asyncio
    from typing import Any

    import cli.main as cli

    class _Ctx:
        async def __aenter__(self) -> Any:
            return object()

        async def __aexit__(self, *exc: object) -> None:
            return None

    class _Engine:
        def connect(self) -> Any:
            return _Ctx()

        async def dispose(self) -> None:
            return None

    class _Driver:
        def session(self) -> Any:
            return _Ctx()

        async def close(self) -> None:
            return None

    class _Qdrant:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "create_async_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(cli, "vector_client", lambda *a, **k: _Qdrant())
    monkeypatch.setattr(cli, "graph_driver", lambda *a, **k: _Driver())

    async def _raises(*args: object, **kwargs: object) -> list[object]:
        raise ValueError("keep must be >= 0, got -1")

    monkeypatch.setattr("core.builds.lifecycle.prune", _raises)
    args = argparse.Namespace(command="prune", project="p", keep=-1)
    assert asyncio.run(cli._run(args)) == 1
    assert "REFUSED: keep must be >= 0" in capsys.readouterr().err
