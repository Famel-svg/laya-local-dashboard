"""Validated, shadow-only Laya dependency recommendation."""
from __future__ import annotations

from typing import Any, Callable


def build_question(candidates: Any) -> tuple[list[str], dict[str, Any]]:
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 5:
        raise ValueError("candidates must contain 2 to 5 options")
    criteria: dict[str, str] = {}
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"candidates[{index}] must be an object")
        candidate_id = candidate.get("id")
        capability = candidate.get("capability")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValueError(f"candidates[{index}].id must be a non-empty string")
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError(f"candidates[{index}].capability must be a non-empty string")
        if len(candidate_id) > 160 or len(capability) > 500:
            raise ValueError("candidate id/capability exceeds length limit")
        if candidate_id in criteria:
            raise ValueError("candidate IDs must be unique")
        effects = candidate.get("effects", [])
        if not isinstance(effects, list) or any(not isinstance(x, str) for x in effects):
            raise ValueError(f"candidates[{index}].effects must be a list of strings")
        cost = candidate.get("cost_class", "unknown")
        if not isinstance(cost, str) or len(cost) > 40:
            raise ValueError(f"candidates[{index}].cost_class must be a short string")
        criteria[candidate_id] = (
            f"Capability: {capability}. Effects: {', '.join(effects[:6]) or 'unspecified'}. "
            f"Cost class: {cost}."
        )
    ids = list(criteria)
    question = {
        "dependency": {
            "type": "choice",
            "instructions": (
                "Choose the single best available capability for the task. "
                "Choose only a supplied option ID. Prefer deterministic/no-tool option "
                "when it fully handles task; never infer permission or execution authority."
            ),
            "criteria": criteria,
        }
    }
    return ids, question


def select_dependency(
    state: Any,
    candidates: Any,
    predict: Callable[..., dict],
    router: Any,
    prediction_result: dict | None = None,
) -> dict[str, Any]:
    if not isinstance(state, dict) or not state:
        raise ValueError("state must be a non-empty object")
    ids, questions = build_question(candidates)
    result = prediction_result or predict(state=state, questions=questions, model="auto", router=router)
    answers = result.get("answers") if isinstance(result, dict) else None
    decision = answers.get("dependency") if isinstance(answers, dict) else None
    selected = decision.get("choice") if isinstance(decision, dict) else None
    if selected not in ids:
        raise ValueError("Laya returned an option outside the supplied catalog")
    probabilities = decision.get("probabilities")
    if not isinstance(probabilities, dict):
        probabilities = {}
    confidence = decision.get("answer_confidence", decision.get("confidence"))
    return {
        "mode": "shadow",
        "recommendation": selected,
        "candidate_ids": ids,
        "probabilities": {key: value for key, value in probabilities.items() if key in ids},
        "confidence": confidence if isinstance(confidence, (float, int)) else None,
        "routing": result.get("routing"),
        "latency_ms": result.get("latency_ms"),
        "dispatch": "not_performed",
        "guardrail": "Host validates availability, policy, permissions, user authorization, and side effects.",
    }


def count_laya_input_tokens(state: dict, questions: dict, result: dict, router: Any) -> int | None:
    """Best-effort exact encoded input count, matching existing dashboard semantics."""
    try:
        from laya.mcp.tools import validate_questions

        model = (result.get("routing") or {}).get("model")
        agent = router._agents.get(model) if model else None
        if agent is None:
            return None
        cleaned = validate_questions(questions)
        ids = list(cleaned)
        internal = {qid: agent._to_internal(cleaned[qid]) for qid in ids}
        encoded = agent._encode_state(state, ids, internal)
        return sum(len(item["ids"]) for item in encoded)
    except Exception:
        return None
