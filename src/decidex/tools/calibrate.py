"""
Offline calibration tool for DecisionEngine telemetry logs.
Calculates Brier Score and Expected Calibration Error (ECE),
and fits optimal temperature scaling (T) and confidence thresholds.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import sys
from typing import Any, Dict, List, Optional, Tuple


def compute_brier_score(probabilities: List[float], labels: List[int]) -> float:
    """
    Computes Brier Score: Mean squared error of probability forecasts.
    Range [0.0, 1.0], where 0.0 indicates perfect calibration.
    """
    if not probabilities or len(probabilities) != len(labels):
        return 0.0
    total = sum((p - y) ** 2 for p, y in zip(probabilities, labels))
    return total / len(probabilities)


def compute_ece(probabilities: List[float], labels: List[int], num_bins: int = 10) -> float:
    """
    Computes Expected Calibration Error (ECE) across uniform confidence bins.
    """
    if not probabilities or len(probabilities) != len(labels):
        return 0.0

    n = len(probabilities)
    bin_boundaries = [i / num_bins for i in range(num_bins + 1)]
    ece = 0.0

    for i in range(num_bins):
        low, high = bin_boundaries[i], bin_boundaries[i + 1]
        bin_indices = [
            idx for idx, p in enumerate(probabilities)
            if (low <= p < high) or (i == num_bins - 1 and low <= p <= high)
        ]

        if not bin_indices:
            continue

        bin_size = len(bin_indices)
        bin_conf = sum(probabilities[idx] for idx in bin_indices) / bin_size
        bin_acc = sum(labels[idx] for idx in bin_indices) / bin_size
        ece += (bin_size / n) * abs(bin_acc - bin_conf)

    return ece


def apply_temperature_to_prob(prob: float, temperature: float) -> float:
    """Calibrates binary probability using temperature scaling."""
    eps = 1e-9
    p = min(max(prob, eps), 1.0 - eps)
    logit = math.log(p / (1.0 - p))
    scaled_logit = logit / max(temperature, 0.01)
    return 1.0 / (1.0 + math.exp(-scaled_logit))


@dataclass
class CalibrationReport:
    sample_count: int
    raw_brier_score: float
    raw_ece: float
    optimal_temperature: float
    calibrated_brier_score: float
    calibrated_ece: float
    recommended_min_confidence: float
    recommended_min_margin: float


class OfflineCalibrator:
    """
    Scans decision telemetry logs (JSONL) and solves for optimal temperature
    and guardrail thresholds to maximize real-world precision.
    """

    @classmethod
    def load_records_from_jsonl(cls, file_path: str) -> List[Dict[str, Any]]:
        records = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records

    @classmethod
    def extract_samples(
        cls,
        records: List[Dict[str, Any]]
    ) -> Tuple[List[float], List[int], List[float]]:
        """
        Correlates decision records with asynchronous feedback records by step_id,
        extracting probabilities, ground truth binary labels, and margin values.
        """
        feedback_map: Dict[str, str] = {}
        decision_records: List[Dict[str, Any]] = []

        for r in records:
            if r.get("event") == "feedback" and "step_id" in r:
                feedback_map[r["step_id"]] = r.get("outcome", "EXECUTED")
            elif "verdict" in r:
                decision_records.append(r)

        probabilities: List[float] = []
        labels: List[int] = []
        margins: List[float] = []

        for r in decision_records:
            step_id = r.get("step_id")
            verdict = r.get("verdict", {})
            conf = float(verdict.get("raw_confidence", 1.0))
            dist = verdict.get("distribution", {})

            # Match feedback if available
            outcome = feedback_map.get(step_id) if step_id else None
            if outcome is None:
                outcome = r.get("memory", {}).get("last_outcome", "EXECUTED")

            # Determine ground-truth label
            # Negative outcomes: LOST, DIED, FAILED, COLLIDED, or fallback verdicts
            if outcome in ("LOST", "DIED", "FAILED", "COLLIDED", "DAMAGED") or verdict.get("is_fallback"):
                is_success = 0
            else:
                is_success = 1

            probabilities.append(conf)
            labels.append(is_success)

            if len(dist) > 1:
                sorted_v = sorted(dist.values(), reverse=True)
                margins.append(sorted_v[0] - sorted_v[1])

        return probabilities, labels, margins

    @classmethod
    def fit_from_samples(
        cls,
        probabilities: List[float],
        labels: List[int],
        margins: Optional[List[float]] = None
    ) -> CalibrationReport:
        if not probabilities or len(probabilities) != len(labels):
            return CalibrationReport(
                sample_count=0,
                raw_brier_score=0.0,
                raw_ece=0.0,
                optimal_temperature=1.0,
                calibrated_brier_score=0.0,
                calibrated_ece=0.0,
                recommended_min_confidence=0.60,
                recommended_min_margin=0.15
            )

        raw_brier = compute_brier_score(probabilities, labels)
        raw_ece = compute_ece(probabilities, labels)

        # 1. Grid search for optimal temperature minimizing Brier Score
        best_t = 1.0
        best_brier = raw_brier

        for t_int in range(50, 251, 5):  # 0.50 to 2.50
            t = t_int / 100.0
            scaled_probs = [apply_temperature_to_prob(p, t) for p in probabilities]
            brier = compute_brier_score(scaled_probs, labels)
            if brier < best_brier:
                best_brier = brier
                best_t = t

        cal_probs = [apply_temperature_to_prob(p, best_t) for p in probabilities]
        cal_ece = compute_ece(cal_probs, labels)

        # 2. Derive recommended min_confidence: 75th percentile of incorrect predictions
        incorrect_confs = [
            cal_probs[i] for i, y in enumerate(labels) if y == 0
        ]
        if incorrect_confs:
            incorrect_confs.sort()
            idx = int(len(incorrect_confs) * 0.75)
            rec_conf = round(max(0.55, min(0.85, incorrect_confs[idx])), 2)
        else:
            rec_conf = 0.60

        # 3. Derive recommended min_margin
        if margins:
            margins_sorted = sorted(margins)
            idx_m = int(len(margins_sorted) * 0.25)
            rec_margin = round(max(0.10, min(0.30, margins_sorted[idx_m])), 2)
        else:
            rec_margin = 0.15

        return CalibrationReport(
            sample_count=len(probabilities),
            raw_brier_score=round(raw_brier, 4),
            raw_ece=round(raw_ece, 4),
            optimal_temperature=round(best_t, 2),
            calibrated_brier_score=round(best_brier, 4),
            calibrated_ece=round(cal_ece, 4),
            recommended_min_confidence=rec_conf,
            recommended_min_margin=rec_margin
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="DecideX Offline Calibration Tool")
    parser.add_argument("journal_file", help="Path to decision_journal.jsonl")
    args = parser.parse_args()

    records = OfflineCalibrator.load_records_from_jsonl(args.journal_file)
    if not records:
        print(f"No valid records found in {args.journal_file}")
        sys.exit(1)

    probabilities, labels, margins = OfflineCalibrator.extract_samples(records)
    report = OfflineCalibrator.fit_from_samples(probabilities, labels, margins)

    print("\n=======================================================")
    print("           DecideX Calibration Analysis Report         ")
    print("=======================================================")
    print(f" Samples Evaluated       : {report.sample_count}")
    print(f" Raw Brier Score         : {report.raw_brier_score:.4f}")
    print(f" Raw ECE                 : {report.raw_ece:.4f}")
    print("-------------------------------------------------------")
    print(f" Optimal Temperature (T) : {report.optimal_temperature:.2f}")
    print(f" Calibrated Brier Score  : {report.calibrated_brier_score:.4f}")
    print(f" Calibrated ECE          : {report.calibrated_ece:.4f}")
    print("-------------------------------------------------------")
    print(" Recommended CalibratedDecisionGuard Parameters:")
    print(f"  - temperature          = {report.optimal_temperature:.2f}")
    print(f"  - min_confidence       = {report.recommended_min_confidence:.2f}")
    print(f"  - min_margin           = {report.recommended_min_margin:.2f}")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
