"""Shared Phase 5h test helpers.

Re-usable across ``test_checker.py`` (Layer A/B/C) and
``test_link_liveness.py`` (Layer D).
"""

from __future__ import annotations

import pytest

_PHASE_5H_KNOBS = {
    "min_claimed_confidence",
    "min_claimed_ml_confidence",
    "require_verify_for_claim",
    "enable_link_liveness",
}


def _force_admin_int(monkeypatch: pytest.MonkeyPatch, knobs: dict[str, int]) -> None:
    """Re-route ``checker._admin_int`` to a fixed mapping for the test.

    All Phase-5h knobs default to ``0`` (no-op floor) unless explicitly
    set by the caller — this isolates one layer at a time.  Non-5h
    knobs fall back to the env-var default.
    """
    from app import checker as checker_mod

    def _fn(key: str) -> int:
        if key in knobs:
            return knobs[key]
        if key in _PHASE_5H_KNOBS:
            return 0
        from app.admin_settings import KNOBS_BY_KEY
        from app.config import settings as _settings

        spec = KNOBS_BY_KEY.get(key)
        if spec is None:
            return int(getattr(_settings, key))
        return int(getattr(_settings, spec.settings_attr))

    monkeypatch.setattr(checker_mod, "_admin_int", _fn)
