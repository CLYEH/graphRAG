"""Why: a green CI `integration` job must mean the integration tests actually ran.

Without this gate, unreachable services made conftest *skip* every integration
test, so the required `integration` check could pass having tested nothing
(fail-loud violation). In CI the gate must fail; locally skipping stays the
right developer experience.
"""

from __future__ import annotations

import pytest

from tests import conftest


def test_gate_fails_loud_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_services_up", lambda: False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        conftest._gate_on_services()


def test_gate_skips_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_services_up", lambda: False)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        conftest._gate_on_services()


def test_gate_passes_when_services_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_services_up", lambda: True)
    monkeypatch.setenv("CI", "true")
    conftest._gate_on_services()  # must neither skip nor fail
