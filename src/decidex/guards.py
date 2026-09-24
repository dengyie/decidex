"""
Guardrails and calibration filters for the DecideX decision engine.
Includes state-settlement debouncing (jev-ultrafast pattern) and
formal temperature scaling with margin checking (Augustus pattern).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from decidex.memory import MemoryHarness
from decidex.types import DecisionVerdict, PrimitiveType

_SENTINEL = object()


class StateSettlementGuard:
    """
    Prevents sampling transient or in-flight animation states.
    Ensures that the environment has reached a settled, stable decision boundary
    before dispatching a decision request (pattern inspired by browser-use/jev-ultrafast).
    """

    def __init__(self, min_stable_frames: int = 2, max_wait_timeout_s: float = 1.0):
        if min_stable_frames < 1:
            raise ValueError("min_stable_frames must be >= 1")
        if max_wait_timeout_s <= 0:
            raise ValueError("max_wait_timeout_s must be > 0.0")

        self.min_stable_frames = min_stable_frames
        self.max_wait_timeout_s = max_wait_timeout_s
        self._last_state_hash: Any = _SENTINEL
        self._baseline_state_hash: Any = _SENTINEL
        self._consecutive_stable_frames: int = 0
        self._first_observation_time: Optional[float] = None

    def mark_action_dispatched(self, pre_action_state_hash: Optional[Any] = None) -> None:
        """
        Signals that an action was just sent to the game/environment.
        If pre_action_state_hash is provided, settlement detection will require the state
        to differentiate from this baseline before counting stable frames,
        eliminating premature ghost settlements on stale pre-action frames.
        """
        self.reset()
        if pre_action_state_hash is not None:
            self._baseline_state_hash = pre_action_state_hash

    def is_state_settled(self, current_state_hash: int, current_time: float) -> bool:
        """
        Evaluates whether the current state has remained identical for min_stable_frames,
        or if max_wait_timeout_s has elapsed.
        When settled, automatically resets state for the next decision cycle.
        """
        if self._first_observation_time is None:
            self._first_observation_time = current_time

        # Force proceed on timeout
        if (current_time - self._first_observation_time) >= self.max_wait_timeout_s:
            self.reset()
            return True

        # If a pre-action baseline hash is active, require transition away before counting stable frames
        if self._baseline_state_hash is not _SENTINEL:
            if current_state_hash == self._baseline_state_hash:
                return False
            # Transition away from baseline observed: clear baseline requirement
            self._baseline_state_hash = _SENTINEL

        if current_state_hash is not None and self._last_state_hash is not _SENTINEL and current_state_hash == self._last_state_hash:
            self._consecutive_stable_frames += 1
        else:
            self._consecutive_stable_frames = 1 if current_state_hash is not None else 0
            self._last_state_hash = current_state_hash

        if self._consecutive_stable_frames >= self.min_stable_frames:
            self.reset()
            return True

        return False

    def reset(self) -> None:
        """Reset state tracking for a new evaluation boundary."""
        self._last_state_hash = _SENTINEL
        self._baseline_state_hash = _SENTINEL
        self._consecutive_stable_frames = 0
        self._first_observation_time = None


class CalibratedDecisionGuard:
    """
    Performs formal confidence temperature scaling, margin differentiation checking,
    legal action boundaries verification, and oscillation loop tripping.
    """

    def __init__(
        self,
        temperature: float = 1.25,
        min_confidence: float = 0.55,
        min_margin: float = 0.15,
        max_consecutive_action: int = 4,
        anti_oscillation: bool = True,
        enforce_legal: bool = True,
        consecutive_unproductive_only: bool = False
    ):
        if temperature <= 0.0:
            raise ValueError(f"Temperature must be strictly positive (> 0.0), got {temperature}")

        self.temperature = temperature
        self.min_confidence = min_confidence
        self.min_margin = min_margin
        self.max_consecutive_action = max_consecutive_action
        self.anti_oscillation = anti_oscillation
        self.enforce_legal = enforce_legal
        self.consecutive_unproductive_only = consecutive_unproductive_only

    def calibrate_distribution(self, raw_dist: Dict[str, float]) -> Tuple[Dict[str, float], float]:
        """
        Applies temperature scaling to the raw probability distribution with logit max-subtraction stabilization:
        z_i = ln(max(p_i, eps)) / T
        z_max = max(z_k)
        p_i_scaled = exp(z_i - z_max) / sum_j(exp(z_j - z_max))
        """
        if not raw_dist:
            return {}, 1.0
        if self.temperature == 1.0:
            top_val = max(raw_dist.values()) if raw_dist else 1.0
            return raw_dist, top_val

        eps = 1e-12
        eff_temp = max(self.temperature, 0.001)
        logits: Dict[str, float] = {}
        for k, v in raw_dist.items():
            prob = max(float(v), eps)
            logits[k] = math.log(prob) / eff_temp

        max_logit = max(logits.values())
        exp_logits = {k: math.exp(z - max_logit) for k, z in logits.items()}
        total_sum = sum(exp_logits.values())

        if total_sum <= 0 or math.isnan(total_sum):
            return raw_dist, 1.0

        calibrated_dist = {k: v / total_sum for k, v in exp_logits.items()}
        top_prob = max(calibrated_dist.values())
        return calibrated_dist, top_prob

    def verify(
        self,
        verdict: DecisionVerdict,
        legal_actions: Optional[List[str]] = None,
        memory: Optional[MemoryHarness] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Audits a decision verdict against safety constraints, confidence thresholds,
        margin separation, and temporal loop patterns.
        """
        # 1. Hard legal action boundary check
        if self.enforce_legal and verdict.primitive == PrimitiveType.CHOICE and legal_actions is not None:
            if str(verdict.selected) not in [str(a) for a in legal_actions]:
                return False, f"ILLEGAL_ACTION: '{verdict.selected}' is not in legal_actions {legal_actions}"

        # 2. Temperature scaling on distribution
        if verdict.distribution:
            calibrated_dist, top_conf = self.calibrate_distribution(verdict.distribution)
            verdict.distribution = calibrated_dist
            selected_key = str(verdict.selected)
            if selected_key in calibrated_dist:
                verdict.calibrated_confidence = calibrated_dist[selected_key]
            elif selected_key.lower() in calibrated_dist:
                verdict.calibrated_confidence = calibrated_dist[selected_key.lower()]
            elif verdict.primitive == PrimitiveType.NOUL:
                noul_key = "true" if verdict.selected else "false"
                verdict.calibrated_confidence = calibrated_dist.get(noul_key, top_conf)
            else:
                verdict.calibrated_confidence = top_conf
        else:
            verdict.calibrated_confidence = verdict.raw_confidence

        # 3. Absolute confidence floor check
        if verdict.calibrated_confidence < self.min_confidence:
            return (
                False,
                f"LOW_CONFIDENCE: calibrated confidence {verdict.calibrated_confidence:.3f} < {self.min_confidence}"
            )

        # 4. Margin separation check (for CHOICE with > 1 option)
        if verdict.primitive == PrimitiveType.CHOICE and len(verdict.distribution) > 1:
            sorted_probs = sorted(verdict.distribution.values(), reverse=True)
            margin = sorted_probs[0] - sorted_probs[1]
            if margin < self.min_margin:
                return (
                    False,
                    f"AMBIGUOUS_MARGIN: top1 - top2 margin {margin:.3f} < {self.min_margin}"
                )

        # 5. Temporal loop checks with memory harness
        if memory is not None:
            selected_str = str(verdict.selected)

            # 5a. Repeated consecutive failure / stuck check (disabled if max_consecutive_action <= 0)
            if self.max_consecutive_action > 0:
                consecutive = memory.count_consecutive_action(
                    selected_str,
                    unproductive_only=self.consecutive_unproductive_only
                )
                if consecutive >= self.max_consecutive_action:
                    return (
                        False,
                        f"STUCK_CONSECUTIVE_LIMIT: action '{verdict.selected}' repeated {consecutive} times"
                    )

            # 5b. Anti-oscillation (A -> B -> A -> B -> ...)
            if self.anti_oscillation:
                period = memory.get_oscillation_period(question_id=verdict.id)
                if period > 0:
                    hist = [
                        e["action"] for e in memory.action_history
                        if verdict.id is None or e.get("question_id") in (verdict.id, None)
                    ]
                    # In period-2 oscillation [..., A, B, A, B], action hist[-2] is 'A'.
                    # Selecting 'A' next continues the oscillation.
                    if period == 2 and len(hist) >= 2 and selected_str == hist[-2]:
                        return False, f"ANTI_OSCILLATION_TRIPPED: action '{verdict.selected}' continues oscillatory pattern"
                    # In period-3 oscillation [..., A, B, C, A, B, C], action hist[-3] is 'A'.
                    if period == 3 and len(hist) >= 3 and selected_str == hist[-3]:
                        return False, f"ANTI_OSCILLATION_TRIPPED: action '{verdict.selected}' continues period-3 oscillatory pattern"

        return True, None
