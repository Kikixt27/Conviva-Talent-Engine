"""Validation Agent — ReAct loop for pedigree / LinkedIn human-in-the-loop.

Architecture (not a flat workflow):
  reason (Thought) → act (Action) → observe → reason … until ACCEPT | REJECT | NEEDS_VALIDATION

Nightly runs pause at REQUEST_SCHOOL (GitHub has no school data) and enqueue the candidate.
You resume with LinkedIn education text; the agent Observes → Re-scores → Accept/Reject.

LangGraph StateGraph when installed; otherwise the same node graph runs via a tiny executor
so the agent contract stays identical.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

log = logging.getLogger("signal.agent")

ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = Path(os.environ.get("VALIDATION_QUEUE_FILE", str(ROOT / "data" / "validation_queue.json")))
TRACE_DIR = ROOT / "data" / "agent_traces"

# Candidates at/above this score with school_unverified enter the validation agent pause.
VALIDATION_INTEREST_SCORE = int(os.environ.get("VALIDATION_INTEREST_SCORE", "55"))

Action = Literal["REQUEST_SCHOOL", "RESCORE", "ACCEPT", "REJECT"]
Status = Literal["running", "needs_validation", "ready", "rejected"]


class ValidationState(TypedDict, total=False):
    run_id: str
    dedup_key: str
    candidate: dict[str, Any]
    role: dict[str, Any]
    profile_blob: str
    thought: str
    action: Action
    observation: str
    human_education: str
    score: int
    school_unverified: bool
    flags: list[str]
    status: Status
    trace: list[dict[str, Any]]
    reasoning: str
    top_signal: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue() -> dict[str, Any]:
    if not QUEUE_FILE.exists():
        return {"pending": {}, "completed": []}
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pending": {}, "completed": []}
    data.setdefault("pending", {})
    data.setdefault("completed", [])
    return data


def save_queue(data: dict[str, Any]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_trace(state: ValidationState, **extra: Any) -> list[dict[str, Any]]:
    trace = list(state.get("trace") or [])
    step = {
        "ts": _utc_now(),
        "thought": state.get("thought", ""),
        "action": state.get("action", ""),
        "observation": state.get("observation", ""),
        **extra,
    }
    trace.append(step)
    return trace


def reason_node(state: ValidationState) -> dict[str, Any]:
    """Thought: decide next action (ReAct reasoner)."""

    score = int(state.get("score") or 0)
    unverified = bool(state.get("school_unverified"))
    human = (state.get("human_education") or "").strip()
    flags = list(state.get("flags") or [])

    if human and state.get("action") != "RESCORE":
        # Just received human observation — next act is rescore.
        thought = (
            "Human provided LinkedIn/education text. "
            "I will merge it into the profile and re-score (Observe → Act)."
        )
        action: Action = "RESCORE"
    elif human and state.get("action") == "RESCORE":
        # After rescore, decide accept/reject.
        still = bool(state.get("school_unverified"))
        if score >= int(os.environ.get("SCORE_THRESHOLD", "65")) and not still:
            thought = f"Re-score={score}; school verified. Accept as ready."
            action = "ACCEPT"
        elif score >= int(os.environ.get("SCORE_THRESHOLD", "65")) and still:
            thought = (
                f"Re-score={score} but school still unverified after human input. "
                "Reject for outreach until stronger pedigree evidence."
            )
            action = "REJECT"
        else:
            thought = f"Re-score={score} below threshold after validation. Reject."
            action = "REJECT"
    elif unverified and score >= VALIDATION_INTEREST_SCORE:
        thought = (
            f"Score={score} is interesting but GitHub/HN has no top-school evidence "
            f"(flags={flags}). Structural gap — pause and request LinkedIn education "
            "(human-in-the-loop validation)."
        )
        action = "REQUEST_SCHOOL"
    elif score >= int(os.environ.get("SCORE_THRESHOLD", "65")) and not unverified:
        thought = f"Score={score} with school evidence present. Accept as ready."
        action = "ACCEPT"
    elif score >= int(os.environ.get("SCORE_THRESHOLD", "65")) and unverified:
        # Below interest? shouldn't happen if interest <= threshold; still pause.
        thought = f"Score={score} but school unverified — must validate before ready."
        action = "REQUEST_SCHOOL"
    else:
        thought = f"Score={score} below interest/threshold. Reject."
        action = "REJECT"

    return {
        "thought": thought,
        "action": action,
        "status": "running",
        "trace": _append_trace({**state, "thought": thought, "action": action, "observation": ""}),
    }


def act_node(state: ValidationState) -> dict[str, Any]:
    """Action: request human validation, rescore, accept, or reject."""

    from pipeline.engine import score_candidate  # local import avoids circular at module load

    action = state.get("action") or "REJECT"
    cand = dict(state.get("candidate") or {})
    role = dict(state.get("role") or {})
    blob = state.get("profile_blob") or ""

    if action == "REQUEST_SCHOOL":
        observation = (
            "Paused. Waiting for TA to paste LinkedIn Education / About text. "
            f"Resume: python scripts/validate_agent.py resume {state.get('dedup_key')} "
            '--education "MIT EECS 2019 …"'
        )
        queue = load_queue()
        key = state.get("dedup_key") or f"unknown:{uuid.uuid4().hex[:8]}"
        queue["pending"][key] = {
            "run_id": state.get("run_id"),
            "dedup_key": key,
            "enqueued_at": _utc_now(),
            "candidate": cand,
            "role_title": role.get("title"),
            "role": role,
            "profile_blob": blob,
            "score": state.get("score"),
            "school_unverified": True,
            "flags": state.get("flags") or [],
            "thought": state.get("thought"),
            "trace": state.get("trace") or [],
            "question": (
                f"Paste LinkedIn Education (and optional Experience) for "
                f"{cand.get('name')} — looking for MIT/Stanford/CMU/Berkeley/UCLA/"
                f"Cornell/UIUC/Michigan/Duke or equivalent."
            ),
        }
        save_queue(queue)
        return {
            "observation": observation,
            "status": "needs_validation",
            "trace": _append_trace(
                {**state, "observation": observation},
                queue_key=key,
            ),
        }

    if action == "RESCORE":
        education = (state.get("human_education") or "").strip()
        enriched = (
            f"{blob}\n\n--- HUMAN VALIDATION (LinkedIn education / pedigree) ---\n{education}\n"
        )
        scored = score_candidate(
            role,
            enriched,
            feedback_tail="",
            location=(cand.get("signals") or {}).get("location") or cand.get("location"),
        ) or {}
        new_score = int(scored.get("score") or 0)
        unverified = bool(scored.get("school_unverified", True))
        flags = list(scored.get("flags") or [])
        observation = (
            f"Re-scored after human education text: score={new_score}, "
            f"school_unverified={unverified}, flags={flags}"
        )
        # Clear human_education consumption marker by keeping it but set action for next reason
        return {
            "profile_blob": enriched,
            "score": new_score,
            "school_unverified": unverified,
            "flags": flags,
            "reasoning": scored.get("reasoning", ""),
            "top_signal": scored.get("top_signal", ""),
            "observation": observation,
            "status": "running",
            "action": "RESCORE",  # signal to reason_node we're post-rescore
            "candidate": {
                **cand,
                "score": new_score,
                "reasoning": scored.get("reasoning", cand.get("reasoning", "")),
                "signals": {
                    **(cand.get("signals") or {}),
                    "school_unverified": unverified,
                    "flags": flags,
                    "top_signal": scored.get("top_signal", ""),
                    "validation": "human_education_applied",
                },
            },
            "trace": _append_trace({**state, "observation": observation, "action": "RESCORE"}),
        }

    if action == "ACCEPT":
        observation = "Accepted → ready for outreach / Slack Ready list."
        cand = {
            **cand,
            "score": int(state.get("score") or cand.get("score") or 0),
            "signals": {
                **(cand.get("signals") or {}),
                "validation_status": "ready",
                "school_unverified": bool(state.get("school_unverified")),
                "flags": list(state.get("flags") or []),
            },
        }
        return {
            "observation": observation,
            "status": "ready",
            "candidate": cand,
            "trace": _append_trace({**state, "observation": observation}),
        }

    observation = "Rejected — below bar or validation failed."
    cand = {
        **cand,
        "signals": {
            **(cand.get("signals") or {}),
            "validation_status": "rejected",
            "school_unverified": bool(state.get("school_unverified")),
            "flags": list(state.get("flags") or []),
        },
    }
    return {
        "observation": observation,
        "status": "rejected",
        "candidate": cand,
        "trace": _append_trace({**state, "observation": observation}),
    }


def route_after_act(state: ValidationState) -> str:
    status = state.get("status")
    action = state.get("action")
    if status in {"needs_validation", "ready", "rejected"}:
        return "end"
    if action == "RESCORE" and status == "running":
        return "reason"  # loop: observe rescore → think again
    return "end"


def build_graph():
    """Compile LangGraph ReAct graph when available."""

    from langgraph.graph import END, StateGraph

    g = StateGraph(ValidationState)
    g.add_node("reason", reason_node)
    g.add_node("act", act_node)
    g.set_entry_point("reason")
    g.add_edge("reason", "act")
    g.add_conditional_edges("act", route_after_act, {"reason": "reason", "end": END})
    return g.compile()


def _run_loop(state: ValidationState, *, max_steps: int = 8) -> ValidationState:
    """Fallback executor with the same ReAct contract (no LangGraph installed)."""

    s: ValidationState = dict(state)  # type: ignore[assignment]
    for _ in range(max_steps):
        s.update(reason_node(s))  # type: ignore[arg-type]
        s.update(act_node(s))  # type: ignore[arg-type]
        nxt = route_after_act(s)
        if nxt == "end":
            break
    return s


def run_validation_agent(
    *,
    candidate: dict[str, Any],
    role: dict[str, Any],
    profile_blob: str,
    human_education: str | None = None,
) -> ValidationState:
    """Run the validation agent to completion or pause (needs_validation)."""

    dedup = f"{candidate.get('source')}:{candidate.get('source_id')}"
    signals = candidate.get("signals") or {}
    init: ValidationState = {
        "run_id": uuid.uuid4().hex[:12],
        "dedup_key": dedup,
        "candidate": candidate,
        "role": role,
        "profile_blob": profile_blob,
        "human_education": (human_education or "").strip(),
        "score": int(candidate.get("score") or 0),
        "school_unverified": bool(signals.get("school_unverified")),
        "flags": list(signals.get("flags") or []),
        "status": "running",
        "trace": [],
        "reasoning": candidate.get("reasoning") or "",
        "top_signal": (signals.get("top_signal") or ""),
    }

    try:
        graph = build_graph()
        final = graph.invoke(init)
        backend = "langgraph"
    except Exception as exc:  # noqa: BLE001 — fall back so nightly never bricks
        log.warning("LangGraph unavailable (%s) — using ReAct loop executor", exc)
        final = _run_loop(init)
        backend = "react_loop"

    final["trace"] = list(final.get("trace") or [])
    final.setdefault("status", "rejected")

    # Persist trace for audit (agent observability lite)
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = TRACE_DIR / f"{final.get('run_id', 'run')}_{final.get('status')}.json"
    trace_path.write_text(
        json.dumps({**final, "backend": backend}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info(
        "Validation agent [%s] %s → %s (score=%s)",
        backend,
        dedup,
        final.get("status"),
        final.get("score"),
    )
    return final  # type: ignore[return-value]


def resume_validation(dedup_key: str, education: str) -> ValidationState:
    """Resume a paused agent with LinkedIn education text (Observe → Re-score)."""

    queue = load_queue()
    item = queue["pending"].get(dedup_key)
    if not item:
        raise KeyError(f"No pending validation for {dedup_key}")

    role = item.get("role") or {"title": item.get("role_title", "Unknown")}
    cand = dict(item.get("candidate") or {})
    final = run_validation_agent(
        candidate=cand,
        role=role,
        profile_blob=item.get("profile_blob") or "",
        human_education=education,
    )

    # Move off pending
    queue = load_queue()
    finished = queue["pending"].pop(dedup_key, item)
    finished["completed_at"] = _utc_now()
    finished["final_status"] = final.get("status")
    finished["final_score"] = final.get("score")
    finished["education_preview"] = education[:240]
    queue["completed"].append(finished)
    queue["completed"] = queue["completed"][-200:]
    save_queue(queue)
    return final


def list_pending() -> list[dict[str, Any]]:
    q = load_queue()
    return list(q.get("pending", {}).values())
