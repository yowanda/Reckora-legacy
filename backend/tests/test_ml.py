"""Phase 4: ML classifier scoring tests.

These exercise the ONNX runtime end-to-end without re-training. The
bundled ``app/ml/model.onnx`` ships with the repo so CI runs the same
weights as production.
"""
from __future__ import annotations

import pytest

from app import ml
from app.config import settings
from app.sites import load_catalog


@pytest.fixture(scope="module")
def catalog() -> object:
    return load_catalog()


def _site_named(catalog: object, name: str) -> object:
    return catalog.by_name(name)  # type: ignore[attr-defined]


def test_model_is_available() -> None:
    assert ml.is_model_available() is True


def test_score_handles_missing_body(catalog: object) -> None:
    site = catalog.sites[0]  # type: ignore[attr-defined]
    out = ml.score(site, http_status=None, body="", elapsed_ms=None)
    # must always return None or 0..100 (never raise)
    assert out is None or 0 <= out <= 100


def test_score_separates_claimed_and_free_signals(catalog: object) -> None:
    """The model should rank a CLAIMED-shaped probe strictly above a
    FREE-shaped probe on the same site."""
    chosen = None
    for s in catalog.sites:  # type: ignore[attr-defined]
        if s.presense_strs and s.absence_strs:
            chosen = s
            break
    assert chosen is not None, "no site with both presense_strs and absence_strs in catalog"
    # CLAIMED-shaped body: 200, presence hit, no absence, ~8 KB body
    claimed_body = "<html>" + chosen.presense_strs[0] + "</html>" + ("padding " * 1000)
    s_claimed = ml.score(chosen, http_status=200, body=claimed_body, elapsed_ms=600)
    # FREE-shaped body: 404, absence hit, no presence, smaller body
    free_body = "<html>" + chosen.absence_strs[0] + "</html>" + ("padding " * 100)
    s_free = ml.score(chosen, http_status=404, body=free_body, elapsed_ms=400)
    assert s_claimed is not None and s_free is not None
    assert s_claimed > s_free, f"claimed={s_claimed} should outrank free={s_free}"


def test_score_disabled_via_setting(
    catalog: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "enable_ml_confidence", False)
    site = catalog.sites[0]  # type: ignore[attr-defined]
    out = ml.score(site, http_status=200, body="x", elapsed_ms=50)
    assert out is None


def test_feature_vector_shape(catalog: object) -> None:
    site = catalog.sites[0]  # type: ignore[attr-defined]
    fv = ml.build_features(site, http_status=200, body="hello", elapsed_ms=120)
    assert len(fv.to_list()) == 8
