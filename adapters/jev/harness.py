"""Mandu'a harness connecting TypeSafe AI's Jev model with Git-backed memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from adapters.jev.client import (
    ChoiceQuestion,
    JevResponse,
    NoulQuestion,
    ScoreQuestion,
    TypeSafeJevClient,
)
from mandua.memory_service import MemoryService
from mandua.models import (
    AnnotationRequest,
    CheckpointRequest,
    CorrectionRequest,
    IntegrationRequest,
    MemoryResult,
    QueryLimits,
)

SUPPORTED_OPERATIONS = (
    "status",
    "context",
    "timeline",
    "why",
    "origin",
    "evolution",
    "compare",
    "decision",
    "recover",
    "checkpoint",
    "annotate",
    "integrate",
    "correct",
)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The structured decision made by Jev regarding which Mandu'a operation to run."""

    operation: str
    confidence: float
    is_mutation: bool
    risk_score: float
    execution_tier: str  # "AUTO_EXECUTE", "REQUIRE_CONFIRMATION", "ESCALATE_TO_HUMAN"
    reasoning_summary: str
    jev_response: JevResponse


@dataclass(frozen=True, slots=True)
class HarnessResult:
    """End-to-end outcome combining Jev System-1 decision and Mandu'a Git memory result."""

    route: RouteDecision
    memory_result: MemoryResult | None
    executed: bool
    requires_confirmation: bool
    explanation: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": {
                "operation": self.route.operation,
                "confidence": self.route.confidence,
                "is_mutation": self.route.is_mutation,
                "risk_score": self.route.risk_score,
                "execution_tier": self.route.execution_tier,
                "reasoning_summary": self.route.reasoning_summary,
            },
            "executed": self.executed,
            "requires_confirmation": self.requires_confirmation,
            "explanation": self.explanation,
            "memory_result": self.memory_result.to_dict() if self.memory_result else None,
            "details": self.details,
        }


class ManduaJevHarness:
    """Harness that connects Jev decision routing and guardrails to Mandu'a MemoryService."""

    def __init__(
        self,
        repository_path: Path | str,
        *,
        client: TypeSafeJevClient | None = None,
        high_confidence_threshold: float = 0.80,
        low_confidence_threshold: float = 0.50,
        limits: QueryLimits | None = None,
    ) -> None:
        self.repo_path = Path(repository_path).resolve()
        self.client = client or TypeSafeJevClient()
        self.high_confidence_threshold = high_confidence_threshold
        self.low_confidence_threshold = low_confidence_threshold
        self.service = MemoryService.open(self.repo_path, limits=limits)

    def route_intent(
        self,
        task_or_query: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> RouteDecision:
        """Use Jev to evaluate which Mandu'a memory operation corresponds to the intent."""
        state = {
            "user_intent": task_or_query,
            "context": context or {},
            "repository": str(self.repo_path.name),
        }

        questions: dict[str, ChoiceQuestion | NoulQuestion | ScoreQuestion] = {
            "operation": ChoiceQuestion(
                options=SUPPORTED_OPERATIONS,
                criteria=(
                    "Select the specific Mandu'a memory operation that fulfills the user's intent."
                ),
            ),
            "is_mutation": NoulQuestion(
                criteria=(
                    "Does this intent request a repository modification, checkpoint, "
                    "annotation, or write?"
                ),
            ),
            "requires_human_approval": NoulQuestion(
                criteria=(
                    "Does this action carry high risk, ambiguity, or potential data loss "
                    "requiring human confirmation?"
                ),
            ),
            "risk_score": ScoreQuestion(
                min_val=0.0,
                max_val=10.0,
                criteria=(
                    "Estimate the operational risk from 0 (pure read) to 10 (destructive rewrite)."
                ),
            ),
        }

        res = self.client.evaluate(state, questions)

        operation = res.get_choice("operation")
        confidence = res.get_confidence("operation")
        mutation_prob = res.get_noul("is_mutation")
        is_mutation = mutation_prob >= 0.5
        human_prob = res.get_noul("requires_human_approval")
        risk_score = res.get_score("risk_score")

        # 3-tier confidence & risk policy:
        if human_prob >= 0.70 or confidence < self.low_confidence_threshold or risk_score >= 7.0:
            tier = "ESCALATE_TO_HUMAN"
            summary = (
                f"Jev flagged high risk ({risk_score:.1f}/10) or "
                f"low confidence ({confidence:.2f}); "
                "requires manual operator review before proceeding."
            )
        elif is_mutation and confidence < self.high_confidence_threshold:
            tier = "REQUIRE_CONFIRMATION"
            summary = (
                f"Mutation detected for operation '{operation}' with confidence {confidence:.2f}; "
                "dry-run preview only until confirmed with --apply."
            )
        else:
            tier = "AUTO_EXECUTE"
            summary = (
                f"High confidence ({confidence:.2f}) decision for operation '{operation}'. "
                "Approved for automatic execution."
            )

        return RouteDecision(
            operation=operation,
            confidence=confidence,
            is_mutation=is_mutation,
            risk_score=risk_score,
            execution_tier=tier,
            reasoning_summary=summary,
            jev_response=res,
        )

    def execute_route(
        self,
        route: RouteDecision,
        *,
        apply: bool = False,
        path: str | Path | None = None,
        line: int | None = None,
        text: str | None = None,
        decision_id: str | None = None,
        left: str | None = None,
        right: str | None = None,
        task_id: str | None = None,
        checkpoint_request: CheckpointRequest | None = None,
        annotation_request: AnnotationRequest | None = None,
        correction_request: CorrectionRequest | None = None,
        integration_request: IntegrationRequest | None = None,
        query: str | None = None,
    ) -> HarnessResult:
        """Execute the routed operation using Mandu'a MemoryService."""
        op = route.operation

        # Enforce execution tiers
        if route.execution_tier == "ESCALATE_TO_HUMAN" and not apply:
            return HarnessResult(
                route=route,
                memory_result=None,
                executed=False,
                requires_confirmation=True,
                explanation=route.reasoning_summary,
                details={"blocked_by_policy": True},
            )

        posix_path = PurePosixPath(path) if path else None

        if op == "status":
            mem_res = self.service.status()
        elif op == "context":
            mem_res = self.service.context(task_id=task_id)
        elif op == "timeline":
            mem_res = self.service.timeline(path=posix_path)
        elif op == "why":
            target_line = line if line is not None else 1
            if posix_path is None:
                raise ValueError("Operation 'why' requires a path parameter.")
            mem_res = self.service.why(path=posix_path, line=target_line)
        elif op == "origin":
            if text is None:
                raise ValueError("Operation 'origin' requires a text query.")
            mem_res = self.service.origin(text=text, path=posix_path)
        elif op == "evolution":
            if posix_path is None:
                raise ValueError("Operation 'evolution' requires a path parameter.")
            mem_res = self.service.evolution(path=posix_path)
        elif op == "compare":
            if not left or not right:
                raise ValueError(
                    "Operation 'compare' requires left and right commit/branch references."
                )
            mem_res = self.service.compare(left, right, path=posix_path)
        elif op == "decision":
            if not decision_id:
                raise ValueError("Operation 'decision' requires a decision_id parameter.")
            mem_res = self.service.decision(decision_id=decision_id)
        elif op == "recover":
            mem_res = self.service.recover(query=query, apply=apply)
        elif op == "checkpoint":
            if checkpoint_request is None:
                raise ValueError("Operation 'checkpoint' requires a CheckpointRequest object.")
            mem_res = self.service.checkpoint(checkpoint_request, apply=apply)
        elif op == "annotate":
            if annotation_request is None:
                raise ValueError("Operation 'annotate' requires an AnnotationRequest object.")
            mem_res = self.service.annotate(annotation_request, apply=apply)
        elif op == "correct":
            if correction_request is None:
                raise ValueError("Operation 'correct' requires a CorrectionRequest object.")
            mem_res = self.service.correct(correction_request, apply=apply)
        elif op == "integrate":
            if integration_request is None:
                raise ValueError("Operation 'integrate' requires an IntegrationRequest object.")
            mem_res = self.service.integrate(integration_request, apply=apply)
        else:
            raise ValueError(f"Unsupported Mandu'a operation: {op}")

        return HarnessResult(
            route=route,
            memory_result=mem_res,
            executed=True,
            requires_confirmation=False,
            explanation=f"Executed operation '{op}' successfully through Mandu'a MemoryService.",
            details={"applied": mem_res.applied},
        )

    def evaluate_and_run(
        self,
        task_or_query: str,
        *,
        apply: bool = False,
        path: str | Path | None = None,
        line: int | None = None,
        text: str | None = None,
        decision_id: str | None = None,
        left: str | None = None,
        right: str | None = None,
        task_id: str | None = None,
    ) -> HarnessResult:
        """Route user intent with Jev and execute the matching Mandu'a operation."""
        route = self.route_intent(task_or_query)
        return self.execute_route(
            route,
            apply=apply,
            path=path,
            line=line,
            text=text or task_or_query,
            decision_id=decision_id,
            left=left,
            right=right,
            task_id=task_id,
            query=task_or_query,
        )

    def compare_hypotheses_with_scoring(
        self,
        left: str,
        right: str,
        criteria: str,
        *,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Compare two Git branches/hypotheses and use Jev to score and choose."""
        posix_path = PurePosixPath(path) if path else None
        mem_res = self.service.compare(left, right, path=posix_path)

        state = {
            "comparison_criteria": criteria,
            "left_branch": left,
            "right_branch": right,
            "evidence": [
                e.to_dict() if hasattr(e, "to_dict") else str(e) for e in mem_res.evidence
            ],
            "answer": mem_res.answer,
        }

        questions: dict[str, ChoiceQuestion | NoulQuestion | ScoreQuestion] = {
            "winner": ChoiceQuestion(
                options=(left, right, "neither"),
                criteria=f"Which hypothesis better fulfills the criteria: {criteria}?",
            ),
            "left_score": ScoreQuestion(
                min_val=0.0,
                max_val=10.0,
                criteria=f"Score for {left} against criteria: {criteria}",
            ),
            "right_score": ScoreQuestion(
                min_val=0.0,
                max_val=10.0,
                criteria=f"Score for {right} against criteria: {criteria}",
            ),
        }

        eval_res = self.client.evaluate(state, questions)

        return {
            "left": left,
            "right": right,
            "criteria": criteria,
            "preferred_hypothesis": eval_res.get_choice("winner"),
            "confidence": eval_res.get_confidence("winner"),
            "left_score": eval_res.get_score("left_score"),
            "right_score": eval_res.get_score("right_score"),
            "git_evidence": mem_res.to_dict(),
        }
