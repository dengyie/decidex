"""
Unit tests for offline calibration metrics and temperature fitting.
"""

import json
from decidex.tools.calibrate import (
    OfflineCalibrator,
    compute_brier_score,
    compute_ece,
)


def test_compute_brier_score_perfect():
    probs = [1.0, 0.0, 1.0, 0.0]
    labels = [1, 0, 1, 0]
    assert compute_brier_score(probs, labels) == 0.0


def test_compute_brier_score_worst():
    probs = [0.0, 1.0]
    labels = [1, 0]
    assert compute_brier_score(probs, labels) == 1.0


def test_compute_ece():
    probs = [0.9, 0.8, 0.85, 0.2, 0.1]
    labels = [1, 1, 1, 0, 0]
    ece = compute_ece(probs, labels, num_bins=5)
    assert 0.0 <= ece <= 1.0


def test_offline_calibrator_fit():
    # Overconfident model predictions
    probs = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60]
    labels = [1, 1, 0, 1, 0, 1, 0, 0]  # Real accuracy ~50%
    margins = [0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]

    report = OfflineCalibrator.fit_from_samples(probs, labels, margins)
    assert report.sample_count == 8
    # Optimal temperature should be > 1.0 to soften overconfidence
    assert report.optimal_temperature >= 1.0
    assert report.calibrated_brier_score <= report.raw_brier_score
    assert report.recommended_min_confidence >= 0.55
    assert report.recommended_min_margin >= 0.10


def test_extract_samples_with_feedback_correlation():
    records = [
        # Decision 1: followed by WON feedback -> label 1
        {
            "step_id": "step_1",
            "verdict": {"raw_confidence": 0.92, "distribution": {"A": 0.92, "B": 0.08}, "is_fallback": False}
        },
        {"event": "feedback", "step_id": "step_1", "outcome": "WON"},
        # Decision 2: followed by DIED feedback -> label 0 (real negative sample!)
        {
            "step_id": "step_2",
            "verdict": {"raw_confidence": 0.88, "distribution": {"A": 0.88, "B": 0.12}, "is_fallback": False}
        },
        {"event": "feedback", "step_id": "step_2", "outcome": "DIED"},
        # Decision 3: fallback verdict -> label 0
        {
            "step_id": "step_3",
            "verdict": {"raw_confidence": 0.0, "distribution": {}, "is_fallback": True}
        }
    ]

    probs, labels, margins = OfflineCalibrator.extract_samples(records)
    assert len(probs) == 3
    assert labels == [1, 0, 0]
    assert probs[0] == 0.92
    assert probs[1] == 0.88


def test_load_records_from_jsonl(tmp_path):
    p = tmp_path / "test_journal.jsonl"
    record = {"observation": {"x": 1}, "verdict": {"raw_confidence": 0.9}}
    p.write_text(json.dumps(record) + "\n", encoding="utf-8")

    records = OfflineCalibrator.load_records_from_jsonl(str(p))
    assert len(records) == 1
    assert records[0]["observation"]["x"] == 1
