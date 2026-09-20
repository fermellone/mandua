"""Client for TypeSafe AI's Jev System One model API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    """A question with a bounded categorical answer space."""

    options: tuple[str, ...]
    criteria: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "choice", "options": list(self.options)}
        if self.criteria is not None:
            data["criteria"] = self.criteria
        return data


@dataclass(frozen=True, slots=True)
class NoulQuestion:
    """A yes/no question returning a probability between 0.0 and 1.0."""

    criteria: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "noul", "criteria": self.criteria}


@dataclass(frozen=True, slots=True)
class ScoreQuestion:
    """A numerical evaluation question within a bounded range."""

    min_val: float = 0.0
    max_val: float = 10.0
    criteria: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": "score",
            "min": self.min_val,
            "max": self.max_val,
        }
        if self.criteria is not None:
            data["criteria"] = self.criteria
        return data


@dataclass(frozen=True, slots=True)
class JevAnswer:
    """One typed decision returned by Jev."""

    question_id: str
    question_type: str
    value: str | float | bool
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JevResponse:
    """Complete structured response from the System One endpoint."""

    answers: dict[str, JevAnswer]
    raw: dict[str, Any]

    def get_choice(self, question_id: str) -> str:
        answer = self.answers.get(question_id)
        if answer is None:
            raise KeyError(f"Question '{question_id}' not found in Jev response")
        return str(answer.value)

    def get_confidence(self, question_id: str) -> float:
        answer = self.answers.get(question_id)
        if answer is None:
            raise KeyError(f"Question '{question_id}' not found in Jev response")
        return answer.confidence

    def get_noul(self, question_id: str) -> float:
        answer = self.answers.get(question_id)
        if answer is None:
            raise KeyError(f"Question '{question_id}' not found in Jev response")
        return float(answer.value)

    def get_score(self, question_id: str) -> float:
        answer = self.answers.get(question_id)
        if answer is None:
            raise KeyError(f"Question '{question_id}' not found in Jev response")
        return float(answer.value)


class TypeSafeJevClient:
    """Client for the TypeSafe AI System One Jev API with offline mock support."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://api.typesafe.ai/v1/systemone",
        model: str = "jev-latest",
        timeout: float = 10.0,
        mock_mode: bool = False,
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.mock_mode = mock_mode or (self.api_key is None)

    def evaluate(
        self,
        state: Any,
        questions: dict[str, ChoiceQuestion | NoulQuestion | ScoreQuestion],
    ) -> JevResponse:
        """Evaluate typed questions against program state."""
        if self.mock_mode:
            return self._mock_evaluate(state, questions)

        payload = {
            "model": self.model,
            "state": state,
            "questions": {qid: q.to_dict() for qid, q in questions.items()},
        }

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "mandua-jev-adapter/0.1.0",
        }

        req = urllib.request.Request(self.base_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                res_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"TypeSafe API error HTTP {error.code}: {error_body}") from error
        except Exception as error:
            raise RuntimeError(f"Network error communicating with TypeSafe API: {error}") from error

        return self._parse_response(res_data)

    def _parse_response(self, data: dict[str, Any]) -> JevResponse:
        answers: dict[str, JevAnswer] = {}
        raw_answers = data.get("answers", {})

        for qid, ans_dict in raw_answers.items():
            if "choice" in ans_dict:
                q_type = "choice"
                val = ans_dict["choice"]
                confidence = float(ans_dict.get("confidence", 1.0))
                probabilities = ans_dict.get("probabilities", {})
            elif "noul" in ans_dict:
                q_type = "noul"
                val = float(ans_dict["noul"])
                confidence = max(val, 1.0 - val)
                probabilities = {"yes": val, "no": 1.0 - val}
            elif "score" in ans_dict:
                q_type = "score"
                val = float(ans_dict["score"])
                confidence = float(ans_dict.get("confidence", 0.9))
                probabilities = {}
            else:
                q_type = "unknown"
                val = ans_dict.get("value", "")
                confidence = float(ans_dict.get("confidence", 0.5))
                probabilities = {}

            answers[qid] = JevAnswer(
                question_id=qid,
                question_type=q_type,
                value=val,
                confidence=confidence,
                probabilities=probabilities,
                raw=ans_dict,
            )

        return JevResponse(answers=answers, raw=data)

    def _mock_evaluate(
        self,
        state: Any,
        questions: dict[str, ChoiceQuestion | NoulQuestion | ScoreQuestion],
    ) -> JevResponse:
        """Deterministic offline mock evaluator for local development and testing."""
        state_str = (
            json.dumps(state).lower() if isinstance(state, (dict, list)) else str(state).lower()
        )
        answers: dict[str, JevAnswer] = {}

        for qid, question in questions.items():
            if isinstance(question, ChoiceQuestion):
                best_option = question.options[0]
                best_matches = -1
                for option in question.options:
                    opt_keyword = option.lower().replace("-", " ")
                    matches = state_str.count(opt_keyword)
                    if (
                        option == "why"
                        and any(w in state_str for w in ("why", "reason", "because", "blame"))
                        or option == "timeline"
                        and any(w in state_str for w in ("timeline", "history", "chronolog"))
                        or option == "status"
                        and any(
                            w in state_str for w in ("status", "working tree", "clean", "state")
                        )
                        or option == "origin"
                        and any(w in state_str for w in ("origin", "appear", "deleted text"))
                        or option == "compare"
                        and any(
                            w in state_str
                            for w in ("compare", "diff", "versus", "vs", "hypothesis")
                        )
                        or option == "checkpoint"
                        and any(w in state_str for w in ("checkpoint", "commit", "save", "record"))
                        or option == "recover"
                        and any(w in state_str for w in ("recover", "reflog", "lost", "restore"))
                        or option == "decision"
                        and any(w in state_str for w in ("decision", "dec-", "trailer"))
                    ):
                        matches += 3

                    if matches > best_matches:
                        best_matches = matches
                        best_option = option

                conf = 0.95 if best_matches > 0 else 0.65
                answers[qid] = JevAnswer(
                    question_id=qid,
                    question_type="choice",
                    value=best_option,
                    confidence=conf,
                    probabilities={best_option: conf},
                    raw={"choice": best_option, "confidence": conf},
                )
            elif isinstance(question, NoulQuestion):
                crit = question.criteria.lower()
                is_mutation = any(
                    m in state_str
                    for m in ("apply", "commit", "record", "delete", "write", "mutate")
                )
                is_high_risk = any(
                    r in state_str for r in ("force", "hard", "drop", "purge", "dangerous")
                )
                if "human" in crit or "review" in crit or "risk" in crit:
                    prob = 0.85 if is_high_risk else (0.4 if is_mutation else 0.1)
                elif "mutation" in crit or "write" in crit:
                    prob = 0.95 if is_mutation else 0.05
                else:
                    prob = 0.5

                answers[qid] = JevAnswer(
                    question_id=qid,
                    question_type="noul",
                    value=prob,
                    confidence=max(prob, 1.0 - prob),
                    probabilities={"yes": prob, "no": 1.0 - prob},
                    raw={"noul": prob},
                )
            elif isinstance(question, ScoreQuestion):
                score_val = 5.0
                if "risk" in (question.criteria or "").lower():
                    is_risky = any(r in state_str for r in ("delete", "force", "drop"))
                    score_val = 8.0 if is_risky else 2.0
                answers[qid] = JevAnswer(
                    question_id=qid,
                    question_type="score",
                    value=score_val,
                    confidence=0.9,
                    raw={"score": score_val, "confidence": 0.9},
                )

        return JevResponse(
            answers=answers,
            raw={"answers": {k: v.raw for k, v in answers.items()}},
        )
