"""Phase 4 ML training: build a tiny LogisticRegression that estimates
the probability a result is CLAIMED, and export it to ONNX.

Methodology
-----------
1. Pull the curated site catalog (``app/data/sites.json``).
2. For each site, *synthesize* a (feature, label) pair from its rule
   set without touching the network:

     - the "claimed" example uses ``presenseStrs`` injected into the
       body, status 200, body length above the median CLAIMED size;
     - the "free" example uses ``absenceStrs``, status 404, smaller body;
     - the "unknown" example mixes signals (status 200, no presence/
       absence hits, very short body) — this teaches the model to be
       cautious about sites that look "alive" but don't carry the
       evidence the rule classifier needs.

   This gives us ~7,500 labelled examples per scan-time class without
   any HTTP traffic, which keeps ``train_ml.py`` deterministic and
   re-runnable in CI.

3. Fit a ``LogisticRegression`` (multinomial, balanced class weights,
   L2). Anything heavier (RF, GBT) overfits this synthetic set without
   any test-time benefit.

4. Export to ONNX with ``skl2onnx``. The class label that maps to
   CLAIMED is recorded in the ONNX metadata so the runtime doesn't
   need to know the column ordering.

Usage
-----
    cd backend
    uv run --extra dev python scripts/train_ml.py

The script writes ``app/ml/model.onnx`` (a ~5 KB binary) and prints a
short cross-validation report.

Reproducibility
---------------
- ``random_state=42`` everywhere.
- Inputs deterministic (synthesized from sites.json) — so re-running
  produces a byte-identical ONNX file modulo skl2onnx version.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from skl2onnx import convert_sklearn
from skl2onnx.common.data_types import FloatTensorType
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

from app.ml import FEATURE_NAMES

# Class-label encoding kept stable across re-trainings so the runtime
# can map the probability column without re-checking metadata.
LABELS = ("free", "unknown", "claimed")
CLAIMED_INDEX = LABELS.index("claimed")

SITES_PATH = ROOT / "app" / "data" / "sites.json"
OUT_PATH = ROOT / "app" / "ml" / "model.onnx"


def _make_features(
    *,
    http_status: int,
    body_len: int,
    presence: int,
    absence: int,
    error_hits: int,
    elapsed_ms: int,
    head_only: bool,
) -> list[float]:
    feats = [
        1.0 if 200 <= http_status < 300 else 0.0,
        1.0 if 400 <= http_status < 500 else 0.0,
        math.log10(1 + body_len),
        float(presence),
        float(absence),
        float(error_hits),
        math.log10(1 + elapsed_ms),
        1.0 if head_only else 0.0,
    ]
    if len(feats) != len(FEATURE_NAMES):  # pragma: no cover
        raise RuntimeError("feature length mismatch — update FEATURE_NAMES")
    return feats


def build_dataset() -> tuple[np.ndarray, np.ndarray]:
    payload = json.loads(SITES_PATH.read_text(encoding="utf-8"))
    raw_sites = payload.get("sites") or payload

    rows: list[list[float]] = []
    labels: list[int] = []

    def add(feats: list[float], label: str) -> None:
        rows.append(feats)
        labels.append(LABELS.index(label))

    for _name, raw in raw_sites.items():
        presence = list(raw.get("presenseStrs") or [])
        absence = list(raw.get("absenceStrs") or [])
        error_strs = list((raw.get("errors") or {}).keys()) if isinstance(raw.get("errors"), dict) else []
        head_only = bool(raw.get("requestHeadOnly"))
        check_type = raw.get("checkType", "status_code")

        # ------------------------------------------------------------------
        # CLAIMED — three flavours:
        #   1. message-style site: 200 + presence hit, full HTML profile
        #   2. status_code-style site: 200 only, no body signal
        #   3. response_url style: 200, body present, no presence (rule
        #      decides via redirect, but we still want ML to lean claimed)
        # ------------------------------------------------------------------
        if presence:
            add(
                _make_features(
                    http_status=200,
                    body_len=12000,
                    presence=len(presence),
                    absence=0,
                    error_hits=0,
                    elapsed_ms=600,
                    head_only=head_only,
                ),
                "claimed",
            )
            # smaller-body variant — some real CLAIMED pages are <2 KB
            add(
                _make_features(
                    http_status=200,
                    body_len=2000,
                    presence=1,
                    absence=0,
                    error_hits=0,
                    elapsed_ms=300,
                    head_only=head_only,
                ),
                "claimed",
            )
        if check_type == "status_code":
            # 200-only CLAIMED — typical for status_code-classified sites
            add(
                _make_features(
                    http_status=200,
                    body_len=4000,
                    presence=0,
                    absence=0,
                    error_hits=0,
                    elapsed_ms=400,
                    head_only=head_only,
                ),
                "claimed",
            )
            # HEAD-only CLAIMED — body length is 0 because we never read it
            if head_only:
                add(
                    _make_features(
                        http_status=200,
                        body_len=0,
                        presence=0,
                        absence=0,
                        error_hits=0,
                        elapsed_ms=350,
                        head_only=True,
                    ),
                    "claimed",
                )

        # ------------------------------------------------------------------
        # FREE — many real-world flavours:
        #   1. 404 + absence hit (classic sherlock-style)
        #   2. 404 + no body signal
        #   3. 200 + absence hit (Telegram, Facebook style: 200 with
        #      "user not found" text)
        #   4. 410 / 451
        # ------------------------------------------------------------------
        if absence:
            add(
                _make_features(
                    http_status=404,
                    body_len=1800,
                    presence=0,
                    absence=len(absence),
                    error_hits=0,
                    elapsed_ms=400,
                    head_only=head_only,
                ),
                "free",
            )
            # 200 + absence hit — the killer FREE signal we missed before
            add(
                _make_features(
                    http_status=200,
                    body_len=10000,
                    presence=0,
                    absence=len(absence),
                    error_hits=0,
                    elapsed_ms=550,
                    head_only=head_only,
                ),
                "free",
            )
        if check_type == "status_code":
            # 404-only FREE — typical for status_code-classified sites
            add(
                _make_features(
                    http_status=404,
                    body_len=900,
                    presence=0,
                    absence=0,
                    error_hits=0,
                    elapsed_ms=350,
                    head_only=head_only,
                ),
                "free",
            )
            # 410 (gone) + tiny body → also FREE-shaped
            add(
                _make_features(
                    http_status=410,
                    body_len=300,
                    presence=0,
                    absence=0,
                    error_hits=0,
                    elapsed_ms=300,
                    head_only=head_only,
                ),
                "free",
            )

        # ------------------------------------------------------------------
        # UNKNOWN — 2xx with no body signal, mid-size body. Also includes
        # 403 / 451 / 429 (block-overrides-free path).
        # ------------------------------------------------------------------
        add(
            _make_features(
                http_status=200,
                body_len=800,
                presence=0,
                absence=0,
                error_hits=0,
                elapsed_ms=1200,
                head_only=head_only,
            ),
            "unknown",
        )
        add(
            _make_features(
                http_status=403,
                body_len=400,
                presence=0,
                absence=0,
                error_hits=0,
                elapsed_ms=600,
                head_only=head_only,
            ),
            "unknown",
        )
        # error-string class hit → UNKNOWN
        if error_strs:
            add(
                _make_features(
                    http_status=200,
                    body_len=4096,
                    presence=0,
                    absence=0,
                    error_hits=len(error_strs),
                    elapsed_ms=800,
                    head_only=head_only,
                ),
                "unknown",
            )

    X = np.array(rows, dtype=np.float32)
    y = np.array(labels, dtype=np.int64)
    return X, y


def main() -> None:
    X, y = build_dataset()
    print(f"dataset shape: X={X.shape} y={y.shape}")
    print(f"class balance: {np.bincount(y).tolist()} (free, unknown, claimed)")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y
    )

    # ``multi_class`` was removed from scikit-learn in 1.5+; the
    # multinomial path is the default for solvers that support it.
    model = LogisticRegression(
        max_iter=2000,
        solver="lbfgs",
        class_weight="balanced",
        random_state=42,
    )
    model.fit(X_train, y_train)

    train_score = model.score(X_train, y_train)
    test_score = model.score(X_test, y_test)
    print(f"accuracy: train={train_score:.3f} test={test_score:.3f}")
    print(classification_report(y_test, model.predict(X_test), target_names=LABELS, zero_division=0))

    # Export to ONNX. ``zipmap`` False means the probability output is a
    # plain (n, n_classes) tensor — easier to consume from runtime than
    # a list-of-dicts.
    initial_types = [("X", FloatTensorType([None, len(FEATURE_NAMES)]))]
    onnx_model = convert_sklearn(
        model,
        initial_types=initial_types,
        options={id(model): {"zipmap": True}},
        target_opset=18,
    )

    # Stash the CLAIMED column index + feature schema as model metadata.
    onnx_model.metadata_props.add(key="claimed_class_index", value=str(CLAIMED_INDEX))
    onnx_model.metadata_props.add(key="features", value=",".join(FEATURE_NAMES))
    onnx_model.metadata_props.add(key="class_labels", value=",".join(LABELS))
    onnx_model.metadata_props.add(key="reckora_phase", value="4")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_bytes(onnx_model.SerializeToString())
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
