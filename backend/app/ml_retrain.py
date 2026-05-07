"""Phase 7d — ML reranker retrain pipeline.

The Phase 4 reranker is trained offline by ``backend/scripts/train_ml.py``
and the resulting ONNX model file is committed to the repo so the
runtime image bundles it. Phase 7d wires that script into the admin
panel: an authenticated admin can trigger a retrain on demand, see
the model age, and pull up the most recent drift / retrain report.

Design decisions
----------------
* **No background scheduler.** A retrain takes ~1 second on the
  synthetic training set; we run it inline in a thread off the
  request loop and return immediately to the caller. The handler
  simply waits for the thread to finish before returning the
  status dict.
* **Trainer is injectable.** :func:`run_retrain` accepts a
  ``trainer`` callable so tests can pass a fast stub. The default
  trainer subprocess-runs ``python scripts/train_ml.py``.
* **Status persisted to JSON** under ``app/ml/retrain_status.json``
  — one source of truth across worker restarts. Callers should
  treat the file as opaque.
* **Model age** is derived from the model file mtime. No need to
  duplicate that into the JSON status.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Paths are anchored on the running app file so the module works
# whether you ``cd`` into ``backend/`` or run from the repo root.
_APP_ROOT = Path(__file__).resolve().parent
_BACKEND_ROOT = _APP_ROOT.parent
_MODEL_PATH = _APP_ROOT / "ml" / "model.onnx"
_STATUS_PATH = _APP_ROOT / "ml" / "retrain_status.json"
_TRAIN_SCRIPT = _BACKEND_ROOT / "scripts" / "train_ml.py"

# Hard cap so a stuck trainer can't pin a worker forever. The real
# script finishes in ~1 second on the synthetic set; 60s is a wide
# margin that still surfaces as an "errored" status to the admin.
_RETRAIN_TIMEOUT_S: float = 60.0


@dataclass
class RetrainStatus:
    """Captured state of the most recent retrain attempt.

    Persisted as JSON between worker invocations so the admin panel
    can render the last run even if the worker that ran it has been
    cycled.
    """

    ok: bool
    started_at: float
    finished_at: float
    duration_s: float
    log: str = ""
    error: str | None = None
    triggered_by: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Persistence helpers.
# ---------------------------------------------------------------------------


def _read_status() -> RetrainStatus | None:
    if not _STATUS_PATH.exists():
        return None
    try:
        raw = json.loads(_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return RetrainStatus(
            ok=bool(raw.get("ok", False)),
            started_at=float(raw.get("started_at", 0.0)),
            finished_at=float(raw.get("finished_at", 0.0)),
            duration_s=float(raw.get("duration_s", 0.0)),
            log=str(raw.get("log", "")),
            error=raw.get("error") if raw.get("error") is None else str(raw.get("error")),
            triggered_by=str(raw.get("triggered_by", "")),
            extras=dict(raw.get("extras") or {}),
        )
    except (TypeError, ValueError):
        return None


def _write_status(status: RetrainStatus) -> None:
    _STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATUS_PATH.write_text(
        json.dumps(status.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def model_age_days() -> float | None:
    """Return ``None`` if the model file is missing, else its age in days."""
    if not _MODEL_PATH.exists():
        return None
    age_s = max(0.0, time.time() - _MODEL_PATH.stat().st_mtime)
    return round(age_s / 86400.0, 3)


def last_status() -> RetrainStatus | None:
    """Return the persisted last-retrain status, or ``None`` if never run."""
    return _read_status()


def reset_status() -> None:
    """Test helper — wipe any persisted retrain status."""
    if _STATUS_PATH.exists():
        with contextlib.suppress(OSError):
            _STATUS_PATH.unlink()


def _default_trainer() -> tuple[bool, str, str | None]:
    """Subprocess-run ``python scripts/train_ml.py`` from the backend root.

    Returns ``(ok, combined_log, error_message)``.
    """

    try:
        proc = subprocess.run(
            [sys.executable, str(_TRAIN_SCRIPT)],
            cwd=str(_BACKEND_ROOT),
            capture_output=True,
            text=True,
            timeout=_RETRAIN_TIMEOUT_S,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout
        partial: str
        if isinstance(raw, bytes):
            partial = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, str):
            partial = raw
        else:
            partial = ""
        return False, partial, f"trainer timed out after {_RETRAIN_TIMEOUT_S}s"
    except FileNotFoundError as exc:
        return False, "", f"trainer script not found: {exc}"
    log_text = (proc.stdout or "") + (((proc.stderr or "").strip() and "\n" + proc.stderr) or "")
    if proc.returncode != 0:
        return False, log_text, f"trainer exited with code {proc.returncode}"
    return True, log_text, None


def run_retrain(
    *,
    triggered_by: str = "",
    trainer: Callable[[], tuple[bool, str, str | None]] | None = None,
) -> RetrainStatus:
    """Run the retrain pipeline (synchronously) and persist the result.

    ``trainer`` defaults to :func:`_default_trainer` (subprocess-runs
    the training script). Tests pass a stub that returns a
    deterministic ``(ok, log, error)`` tuple in microseconds.
    """

    callable_trainer = trainer if trainer is not None else _default_trainer
    started = time.time()
    try:
        ok, log_text, error = callable_trainer()
    except Exception as exc:
        ok = False
        log_text = ""
        error = f"trainer raised: {exc.__class__.__name__}: {exc}"
        log.exception("retrain trainer raised")
    finished = time.time()
    status = RetrainStatus(
        ok=ok,
        started_at=started,
        finished_at=finished,
        duration_s=round(finished - started, 4),
        log=log_text[-8192:],  # cap so the JSON file stays small
        error=error,
        triggered_by=triggered_by,
    )
    _write_status(status)
    return status
