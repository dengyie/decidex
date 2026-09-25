"""
Central DecisionEngine orchestrating the five-stage formal decision calculus:
state evidence -> inference -> calibrated gating -> fallback dispatch -> telemetry feedback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union
import uuid

from decidex.guards import CalibratedDecisionGuard
from decidex.memory import MemoryHarness
from decidex.providers.base import BaseInferenceProvider
from decidex.types import (
    DecisionVerdict,
    ObservationPayload,
    PrimitiveType,
    QuestionSpec,
    TurnPhase,
)

logger = logging.getLogger("decidex.engine")


class DecisionEngine:
    """
    Central orchestration engine for System One decision loops.
    """

    def __init__(
        self,
        provider: BaseInferenceProvider,
        guard: Optional[CalibratedDecisionGuard] = None,
        memory: Optional[MemoryHarness] = None,
        journal_path: Optional[str] = None,
        fallback_resolver: Optional[Callable[..., Any]] = None,
        journal_buffer_limit: int = 16,
    ):
        self.provider = provider
        self.guard = guard or CalibratedDecisionGuard()
        self.memory = memory or MemoryHarness()
        self.journal_path = journal_path
        self.fallback_resolver = fallback_resolver
        self.journal_buffer_limit = max(1, journal_buffer_limit)
        self._journal_file = None
        self._journal_buffer: List[str] = []
        self._journal_lock = threading.Lock()

        if self.journal_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.journal_path)), exist_ok=True)
            self._journal_file = open(self.journal_path, "a", encoding="utf-8")

    def __enter__(self) -> DecisionEngine:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    async def __aenter__(self) -> DecisionEngine:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    async def step(
        self,
        domain_state: Any,
        questions: List[QuestionSpec],
        legal_actions: Optional[Union[List[str], Dict[str, List[str]]]] = None,
        phase: Optional[TurnPhase] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, DecisionVerdict]:
        """
        Executes one decision step across one or more QuestionSpecs.
        Returns a dictionary mapping question ID to validated DecisionVerdict.
        """
        # 1. Separate trivial questions (0 or 1 options) from active questions requiring inference
        trivial_verdicts: Dict[str, DecisionVerdict] = {}
        active_questions: List[QuestionSpec] = []

        for q in questions:
            if q.primitive == PrimitiveType.CHOICE and len(q.options or []) <= 1:
                # Deterministic fast-path: 1 option is forced; 0 options is immediate fallback
                if q.options and len(q.options) == 1:
                    opt = q.options[0]
                    trivial_verdicts[q.id] = DecisionVerdict(
                        id=q.id,
                        primitive=q.primitive,
                        selected=opt,
                        raw_confidence=1.0,
                        calibrated_confidence=1.0,
                        distribution={opt: 1.0},
                        latency_ms=0.0,
                        is_fallback=False
                    )
                else:
                    fb_val = self._resolve_fallback(q, [], "EMPTY_OPTIONS_SET", domain_state)
                    trivial_verdicts[q.id] = DecisionVerdict(
                        id=q.id,
                        primitive=q.primitive,
                        selected=fb_val,
                        raw_confidence=0.0,
                        calibrated_confidence=0.0,
                        distribution={},
                        latency_ms=0.0,
                        is_fallback=True,
                        fallback_reason="EMPTY_OPTIONS_SET"
                    )
            else:
                q.validate_spec()
                active_questions.append(q)

        step_id = f"step_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

        # 2. State & Memory Evidence Assembly
        payload = ObservationPayload(
            observation=domain_state,
            memory=self.memory.export_context(),
            phase=phase,
            metadata=metadata or {}
        )

        # 3. Bounded Model Judgment (Inference for active questions only)
        raw_verdicts: List[DecisionVerdict] = []
        inference_error: Optional[str] = None
        if active_questions:
            try:
                raw_verdicts = await self.provider.infer(payload, active_questions)
            except Exception as exc:
                logger.warning(f"Inference provider failure: {exc}")
                inference_error = str(exc)

        # Map by id
        verdicts_map = {v.id: v for v in raw_verdicts}
        final_verdicts: Dict[str, DecisionVerdict] = {}

        # 4. Explicit Policy Gating & Fallback Resolution
        for q in questions:
            # Resolve legal actions specifically for this question head
            if isinstance(legal_actions, dict):
                if q.id in legal_actions:
                    q_legal = legal_actions[q.id]
                else:
                    q_legal = q.options or []
            elif isinstance(legal_actions, list):
                if len(questions) == 1:
                    q_legal = legal_actions
                else:
                    # Multi-head: filter legal_actions by q.options to prevent cross-head leakage
                    filtered = [act for act in legal_actions if q.options and act in q.options]
                    q_legal = filtered if filtered else (q.options or legal_actions)
            else:
                q_legal = q.options or []

            if q.id in trivial_verdicts:
                verdict = trivial_verdicts[q.id]
                # If forced single action violates explicit legal_actions, fallback
                has_explicit_legal = (
                    (isinstance(legal_actions, dict) and q.id in legal_actions)
                    or (isinstance(legal_actions, list))
                )
                if not verdict.is_fallback and q.primitive == PrimitiveType.CHOICE and has_explicit_legal:
                    if str(verdict.selected) not in [str(a) for a in q_legal]:
                        fallback_val = self._resolve_fallback(q, q_legal, "FORCED_OPTION_NOT_LEGAL", domain_state)
                        verdict = DecisionVerdict(
                            id=q.id,
                            primitive=q.primitive,
                            selected=fallback_val,
                            raw_confidence=0.0,
                            calibrated_confidence=0.0,
                            distribution={},
                            latency_ms=0.0,
                            is_fallback=True,
                            fallback_reason="FORCED_OPTION_NOT_LEGAL"
                        )
            else:
                verdict = verdicts_map.get(q.id)

                if verdict is None or inference_error is not None:
                    # Upstream failure -> construct fallback verdict
                    fallback_val = self._resolve_fallback(q, q_legal, inference_error or "UPSTREAM_EMPTY_RESULT", domain_state)
                    verdict = DecisionVerdict(
                        id=q.id,
                        primitive=q.primitive,
                        selected=fallback_val,
                        raw_confidence=0.0,
                        calibrated_confidence=0.0,
                        distribution={},
                        latency_ms=0.0,
                        is_fallback=True,
                        fallback_reason=inference_error or "UPSTREAM_EMPTY_RESULT"
                    )
                else:
                    # Run guard validation
                    is_valid, reject_reason = self.guard.verify(verdict, q_legal, self.memory)
                    if not is_valid:
                        logger.info(f"Guard rejected verdict '{verdict.id}': {reject_reason}")
                        fallback_val = self._resolve_fallback(q, q_legal, reject_reason or "GUARD_REJECTED", domain_state)
                        verdict = DecisionVerdict(
                            id=q.id,
                            primitive=q.primitive,
                            selected=fallback_val,
                            raw_confidence=0.0,
                            calibrated_confidence=0.0,
                            distribution={},
                            latency_ms=verdict.latency_ms,
                            is_fallback=True,
                            fallback_reason=reject_reason
                        )

            final_verdicts[q.id] = verdict

            # 4. Memory Feedback Update (for primary action/choice)
            if q.primitive == PrimitiveType.CHOICE:
                self.memory.record_step(
                    action=str(verdict.selected),
                    outcome="PENDING" if not verdict.is_fallback else f"FALLBACK({verdict.fallback_reason})",
                    metadata={"question_id": q.id, "confidence": verdict.calibrated_confidence, "step_id": step_id},
                    question_id=q.id
                )

            # 5. Telemetry Journaling
            self._journal_decision(step_id, payload, q, verdict)

        return final_verdicts

    def record_feedback(
        self,
        outcome: str,
        metadata: Optional[Dict[str, Any]] = None,
        step_id: Optional[str] = None
    ) -> None:
        """
        Updates the outcome of the recorded step(s) in memory
        (e.g., 'EXECUTED', 'COLLIDED', 'WON', 'LOST') and appends an audit record to the journal.
        """
        target_step_id = step_id
        if self.memory.action_history:
            updated = False
            if step_id:
                for entry in self.memory.action_history:
                    if entry.get("metadata", {}).get("step_id") == step_id:
                        entry["outcome"] = outcome
                        if metadata:
                            entry["metadata"].update(metadata)
                        updated = True
            if not updated:
                self.memory.action_history[-1]["outcome"] = outcome
                if metadata:
                    self.memory.action_history[-1]["metadata"].update(metadata)
                if target_step_id is None:
                    target_step_id = self.memory.action_history[-1].get("metadata", {}).get("step_id")

        if self._journal_file is not None:
            feedback_record = {
                "timestamp": time.time(),
                "event": "feedback",
                "step_id": target_step_id,
                "outcome": outcome,
                "metadata": metadata or {}
            }
            self._write_journal_record(feedback_record, immediate=True)

    def _resolve_fallback(
        self,
        q: QuestionSpec,
        legal_actions: List[str],
        reason: str,
        domain_state: Any = None
    ) -> Any:
        if self.fallback_resolver:
            try:
                import inspect
                sig = inspect.signature(self.fallback_resolver)
                if len(sig.parameters) >= 4:
                    return self.fallback_resolver(q, legal_actions, reason, domain_state)
            except Exception:
                pass
            return self.fallback_resolver(q, legal_actions, reason)

        # Standard safe defaults
        if q.primitive == PrimitiveType.CHOICE:
            if legal_actions:
                return legal_actions[0]
            if legal_actions is not None:
                # Explicit legal_actions provided but empty
                return "noop"
            if q.options:
                return q.options[0]
            return "noop"
        elif q.primitive == PrimitiveType.NOUL:
            return False
        else:
            return q.scale[0]

    def _flush_locked(self) -> None:
        """Internal helper to flush buffered journal lines while holding _journal_lock."""
        if self._journal_file is None or not self._journal_buffer:
            return
        content = "".join(self._journal_buffer)
        self._journal_buffer.clear()
        try:
            import fcntl
            fcntl.flock(self._journal_file.fileno(), fcntl.LOCK_EX)
            try:
                self._journal_file.write(content)
                self._journal_file.flush()
            finally:
                fcntl.flock(self._journal_file.fileno(), fcntl.LOCK_UN)
        except (ImportError, AttributeError, OSError):
            self._journal_file.write(content)
            self._journal_file.flush()

    def flush(self) -> None:
        """Flushes in-memory journal records to disk."""
        with self._journal_lock:
            self._flush_locked()

    async def flush_async(self) -> None:
        """Non-blocking asynchronous journal flush."""
        await asyncio.to_thread(self.flush)

    def _write_journal_record(self, record: Dict[str, Any], immediate: bool = False) -> None:
        """Buffers journal record in memory and writes batch to disk under file lock."""
        with self._journal_lock:
            if self._journal_file is not None:
                line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
                self._journal_buffer.append(line)
                if immediate or len(self._journal_buffer) >= self.journal_buffer_limit:
                    self._flush_locked()

    def _journal_decision(
        self,
        step_id: str,
        payload: ObservationPayload,
        question: QuestionSpec,
        verdict: DecisionVerdict
    ) -> None:
        if self._journal_file is None:
            return

        record = {
            "step_id": step_id,
            "timestamp": time.time(),
            "observation": payload.observation,
            "memory": payload.memory,
            "question": {
                "id": question.id,
                "primitive": question.primitive.value,
                "options": question.options,
                "scale": question.scale
            },
            "verdict": verdict.model_dump(),
        }
        self._write_journal_record(record)

    def close(self) -> None:
        with self._journal_lock:
            self._flush_locked()
            if self._journal_file is not None:
                try:
                    self._journal_file.close()
                finally:
                    self._journal_file = None
