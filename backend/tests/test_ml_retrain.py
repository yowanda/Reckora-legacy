"""Tests for Phase 7d — ML retrain pipeline."""

from __future__ import annotations

import json
from collections.abc import Generator

import pytest

from app import ml_retrain as retrain_mod
from app.ml_retrain import RetrainStatus, last_status, run_retrain


@pytest.fixture(autouse=True)
def _clean() -> Generator[None, None, None]:
    retrain_mod.reset_status()
    yield
    retrain_mod.reset_status()


class TestRunRetrain:
    def test_writes_status_on_success(self) -> None:
        status = run_retrain(
            triggered_by="alice",
            trainer=lambda: (True, "trained ok\n", None),
        )
        assert status.ok is True
        assert status.error is None
        assert status.triggered_by == "alice"
        assert "trained ok" in status.log
        assert status.duration_s >= 0
        # Persisted to disk and matches the returned status.
        loaded = last_status()
        assert loaded is not None
        assert loaded.ok is True
        assert loaded.triggered_by == "alice"

    def test_writes_status_on_failure(self) -> None:
        status = run_retrain(
            triggered_by="bob",
            trainer=lambda: (False, "boom\n", "trainer exited 1"),
        )
        assert status.ok is False
        assert status.error == "trainer exited 1"
        assert status.triggered_by == "bob"

    def test_captures_trainer_exception(self) -> None:
        def crash() -> tuple[bool, str, str | None]:
            raise RuntimeError("kaboom")

        status = run_retrain(triggered_by="charlie", trainer=crash)
        assert status.ok is False
        assert status.error is not None
        assert "kaboom" in status.error

    def test_log_truncated(self) -> None:
        big_log = "x" * 20000
        status = run_retrain(trainer=lambda: (True, big_log, None))
        # Capped at 8KB so the JSON file stays small.
        assert len(status.log) == 8192


class TestPersistedStatus:
    def test_reads_back_from_disk(self) -> None:
        run_retrain(trainer=lambda: (True, "ok", None), triggered_by="dave")
        loaded = last_status()
        assert isinstance(loaded, RetrainStatus)
        assert loaded.ok is True
        assert loaded.triggered_by == "dave"

    def test_corrupt_status_returns_none(self, tmp_path) -> None:
        # Force-write garbage to the persistence file and ensure
        # last_status degrades gracefully.
        retrain_mod._STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        retrain_mod._STATUS_PATH.write_text("not-json{", encoding="utf-8")
        assert last_status() is None

    def test_partial_status_returns_none(self) -> None:
        # File parses as JSON but isn't a dict.
        retrain_mod._STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        retrain_mod._STATUS_PATH.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert last_status() is None


class TestModelAge:
    def test_returns_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pathlib import Path

        monkeypatch.setattr(retrain_mod, "_MODEL_PATH", Path("/nonexistent/model.onnx"))
        assert retrain_mod.model_age_days() is None

    def test_returns_float_when_present(self) -> None:
        # The repo ships a model.onnx; its age is bounded by the test
        # process's wall clock.
        age = retrain_mod.model_age_days()
        if age is None:
            pytest.skip("model.onnx not present in this checkout")
        assert age >= 0.0
