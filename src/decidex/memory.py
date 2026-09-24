"""
External memory harness for stateless System One decision models.
Provides sliding-window action tracking, loop detection, and hint buffering.
"""

from __future__ import annotations

from collections import deque
import time
from typing import Any, Deque, Dict, List, Optional


class MemoryHarness:
    """
    Maintains a sliding-window of recent actions, execution outcomes, and domain hints.
    Injected into each stateless decision cycle to provide temporal continuity.
    Uses collections.deque with maxlen for O(1) bounded FIFO eviction.
    """

    def __init__(self, capacity: int = 8, hint_capacity: int = 5):
        self.capacity = capacity
        self.hint_capacity = hint_capacity
        self.action_history: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self.recent_hints: Deque[str] = deque(maxlen=hint_capacity)

    def record_step(
        self,
        action: str,
        outcome: str = "EXECUTED",
        metadata: Optional[Dict[str, Any]] = None,
        question_id: Optional[str] = None
    ) -> None:
        """
        Record an executed action along with its observed environmental outcome.
        Evicts oldest entry in O(1) time when capacity is reached.
        """
        meta = dict(metadata or {})
        if question_id:
            meta["question_id"] = question_id
        entry = {
            "action": action,
            "outcome": outcome,
            "timestamp": time.time(),
            "question_id": question_id or meta.get("question_id"),
            "metadata": meta
        }
        self.action_history.append(entry)

    def record_hint(self, hint: str) -> None:
        """
        Record a situational hint (e.g. NPC dialogue, objective reminder, warning message).
        Deduplicates adjacent duplicate hints.
        """
        if self.recent_hints and self.recent_hints[-1] == hint:
            return
        self.recent_hints.append(hint)

    def get_oscillation_period(self, question_id: Optional[str] = None) -> int:
        """
        Returns the detected oscillation period (2 or 3), or 0 if no oscillation is detected.
        Optionally filters actions belonging to a specific question_id.
        """
        history = [
            e["action"] for e in self.action_history
            if question_id is None or e.get("question_id") in (question_id, None)
        ]

        # Check 2-step oscillation (A -> B -> A -> B) on the most recent 4 steps
        if len(history) >= 4:
            r4 = history[-4:]
            if r4[-4] == r4[-2] and r4[-3] == r4[-1] and r4[-4] != r4[-3]:
                return 2

        # Check 3-step oscillation (A -> B -> C -> A -> B -> C) on the most recent 6 steps
        if len(history) >= 6:
            r6 = history[-6:]
            if r6[-6] == r6[-3] and r6[-5] == r6[-2] and r6[-4] == r6[-1] and len(set(r6[:3])) == 3:
                return 3

        return 0

    def detect_oscillation(self, k: int = 4, question_id: Optional[str] = None) -> bool:
        """
        Detect whether the agent is oscillating between actions.
        k: Window size of recent actions to inspect (default 4).
        """
        return self.get_oscillation_period(question_id=question_id) > 0

    def count_consecutive_action(self, action: str, unproductive_only: bool = False) -> int:
        """
        Returns the number of consecutive recent executions of the specified action.
        If unproductive_only is True, only counts executions whose outcome indicates a stall,
        failure, or lack of progress (e.g. BLOCKED, NOOP, UNCHANGED, STUCK, COLLIDED, FAILED).
        """
        count = 0
        unproductive_outcomes = {"BLOCKED", "NOOP", "UNCHANGED", "STUCK", "COLLIDED", "FAILED", "FALLBACK"}
        for entry in reversed(self.action_history):
            if entry["action"] == action:
                if unproductive_only:
                    outcome = str(entry.get("outcome", "")).upper()
                    if any(u in outcome for u in unproductive_outcomes):
                        count += 1
                    else:
                        break
                else:
                    count += 1
            else:
                break
        return count

    def export_context(self) -> Dict[str, Any]:
        """
        Serializes current memory state into a model-digestible dictionary.
        """
        return {
            "recent_actions": [
                f"{e['action']} ({e['outcome']})" for e in self.action_history
            ],
            "recent_hints": list(self.recent_hints),
            "is_oscillating": self.detect_oscillation(),
            "last_action": self.action_history[-1]["action"] if self.action_history else None,
            "last_outcome": self.action_history[-1]["outcome"] if self.action_history else None,
        }

    def clear(self) -> None:
        """Resets the memory buffer (e.g. on episode restart or new level)."""
        self.action_history.clear()
        self.recent_hints.clear()
