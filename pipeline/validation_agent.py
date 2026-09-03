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

Action = Literal["ENRICH_IDENTITY", "REQUEST_SCHOOL", "RESCORE", "ACCEPT", "REJECT"]
Status = Literal["running", "needs_validation", "ready", "rejected"]


def github_login_from_candidate(cand: dict[str, Any]) -> str:
    url = (cand.get("profile_url") or "").rstrip("/")
    if "github.com/" in url:
        return url.split("github.com/")[-1].split("/")[0].strip()
    key = str(cand.get("source_id") or "")
    # source_id is numeric for GitHub — login may be in name when name==login
    name = (cand.get("name") or "").strip()
    if name and " " not in name and name.lower() == name:
        return name
    return name or key


def looks_like_nickname(name: str, login: str = "") -> bool:
    """True when the display name is likely a handle, not a legal name."""

    n = (name or "").strip()
    if not n:
        return True
    if " " not in n and len(n) <= 24:
        return True
    if login and n.lower() == login.lower():
        return True
    # Mostly non-letters / leetspeak-ish
    letters = sum(ch.isalpha() for ch in n)
    if letters < max(3, len(n) // 2):
        return True
    return False


def build_identity_search_links(cand: dict[str, Any]) -> dict[str, Any]:
    """Google / LinkedIn X-Ray links for TA when GitHub has no LinkedIn + nickname only."""

    from urllib.parse import quote_plus

    sig = cand.get("signals") or {}
    name = (cand.get("name") or "").strip()
    login = github_login_from_candidate(cand)
    company = (sig.get("company") or cand.get("company") or "").strip()
    location = (sig.get("location") or cand.get("location") or "").strip()
    nickname = looks_like_nickname(name, login)

    queries: list[dict[str, str]] = []

    def add(label: str, q: str) -> None:
        q = " ".join(q.split())
        if not q.strip():
            return
        queries.append(
            {
                "label": label,
                "query": q,
                "url": f"https://www.google.com/search?q={quote_plus(q)}",
            }
        )

    # Prefer handle-based search when name is a nickname
    if login:
        add("LinkedIn X-Ray · GitHub login", f'site:linkedin.com/in "{login}"')
        add("Google · GitHub login", f'"{login}" (engineer OR "software" OR MCP OR LLM)')
    if name and name.lower() != (login or "").lower():
        add("LinkedIn X-Ray · display name", f'site:linkedin.com/in "{name}"')
    if name and company:
        add("LinkedIn X-Ray · name + company", f'site:linkedin.com/in "{name}" "{company}"')
    if login and company:
        add("LinkedIn X-Ray · login + company", f'site:linkedin.com/in "{login}" "{company}"')
    if name and location:
        add(
            "LinkedIn X-Ray · name + location",
            f'site:linkedin.com/in "{name}" "{location}"',
        )
    if company and location:
        add(
            "LinkedIn X-Ray · company + location + AI",
            f'site:linkedin.com/in "{company}" "{location}" ("AI Engineer" OR "Machine Learning" OR MCP)',
        )

    tip = (
        "Display name looks like a nickname/handle — do NOT search legal-name only. "
        "Start with GitHub login X-Ray, then company/location. "
        "If still unfindable, paste: 'LinkedIn not found; nickname-only GitHub; no edu email' "
        "and skip outreach priority."
        if nickname
        else "Open an X-Ray link → LinkedIn Education → paste into: "
        "python scripts/validate_agent.py resume <n>"
    )

    return {
        "github_login": login,
        "nickname_likely": nickname,
        "searches": queries[:8],
        "tip": tip,
    }


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
    enriched: bool
    enrichment: dict[str, Any]
    auto_education: str  # blob from enrich tools for RESCORE


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
    auto_edu = (state.get("auto_education") or "").strip()
    flags = list(state.get("flags") or [])
    enriched = bool(state.get("enriched"))
    enrichment = state.get("enrichment") or {}
    thr = int(os.environ.get("SCORE_THRESHOLD", "65"))

    # --- After a RESCORE step: decide terminal outcome ---
    if state.get("action") == "RESCORE":
        still = bool(state.get("school_unverified"))
        src = "human" if human else "auto-enrichment"
        if score >= thr and not still:
            thought = f"Re-score={score} after {src}; school verified. Accept as ready."
            action: Action = "ACCEPT"
        elif score >= thr and still:
            thought = (
                f"Re-score={score} after {src} but school still unverified. "
                + (
                    "Pause for human LinkedIn — enrichment was weak."
                    if not human
                    else "Reject for outreach until stronger pedigree evidence."
                )
            )
            action = "REQUEST_SCHOOL" if not human else "REJECT"
        else:
            if not human and not still and score >= VALIDATION_INTEREST_SCORE:
                thought = f"Re-score={score} below threshold after enrichment. Reject."
                action = "REJECT"
            elif not human and still:
                thought = (
                    f"Re-score={score} after enrichment; pedigree still weak. "
                    "Pause for human LinkedIn education paste."
                )
                action = "REQUEST_SCHOOL"
            else:
                thought = f"Re-score={score} below threshold after validation. Reject."
                action = "REJECT"

    # --- Human just provided education text ---
    elif human:
        thought = (
            "Human provided LinkedIn/education text. "
            "Merge into profile and re-score (Observe → Act)."
        )
        action = "RESCORE"

    # --- Need pedigree: enrich first (API tools), then maybe rescore / pause ---
    elif unverified and score >= VALIDATION_INTEREST_SCORE and not enriched:
        thought = (
            f"Score={score} interesting but school unverified (flags={flags}). "
            "Before asking a human, ENRICH identity via GitHub/Kaggle/StackOverflow "
            "APIs (blog, twitter, edu email, exact-login probes)."
        )
        action = "ENRICH_IDENTITY"

    elif unverified and score >= VALIDATION_INTEREST_SCORE and enriched:
        if enrichment.get("strong_pedigree_signal") and auto_edu:
            thought = (
                f"Enrichment confidence={enrichment.get('confidence')}: "
                f"schools={enrichment.get('school_hits')} edu_emails={enrichment.get('edu_emails')}. "
                "Auto re-score with enrichment blob before human pause."
            )
            action = "RESCORE"
        else:
            thought = (
                f"Enrichment done (confidence={enrichment.get('confidence')}, "
                f"clues={len(enrichment.get('clues') or [])}) but no strong school signal. "
                "Nickname-only GitHub likely — pause for human LinkedIn / X-Ray."
            )
            action = "REQUEST_SCHOOL"

    elif score >= thr and not unverified:
        thought = f"Score={score} with school evidence present. Accept as ready."
        action = "ACCEPT"

    elif score >= thr and unverified:
        thought = f"Score={score} but school unverified — enrich/validate before ready."
        action = "ENRICH_IDENTITY" if not enriched else "REQUEST_SCHOOL"

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
    """Action: enrich identity, request human validation, rescore, accept, or reject."""

    from pipeline.engine import score_candidate  # local import avoids circular at module load
    from pipeline.identity_enrich import enrich_identity

    action = state.get("action") or "REJECT"
    cand = dict(state.get("candidate") or {})
    role = dict(state.get("role") or {})
    blob = state.get("profile_blob") or ""

    if action == "ENRICH_IDENTITY":
        login = github_login_from_candidate(cand)
        summary = enrich_identity(login)
        observation = (
            f"Enrichment complete for login={login}: confidence={summary.get('confidence')}, "
            f"schools={summary.get('school_hits')}, edu_emails={summary.get('edu_emails')}, "
            f"clues={len(summary.get('clues') or [])}."
        )
        # If edu email / school tokens found, treat as softer school signal for next reason
        strong = bool(summary.get("strong_pedigree_signal"))
        new_flags = list(state.get("flags") or [])
        if strong and "enrichment pedigree hit" not in [f.lower() for f in new_flags]:
            new_flags.append("enrichment pedigree hit")
        return {
            "enriched": True,
            "enrichment": summary,
            "auto_education": summary.get("education_blob") or "",
            # Keep unverified=True until RESCORE; strong signal only chooses next action.
            "school_unverified": True,
            "flags": new_flags,
            "observation": observation,
            "status": "running",
            "action": "ENRICH_IDENTITY",
            "candidate": {
                **cand,
                "signals": {
                    **(cand.get("signals") or {}),
                    "enrichment": {
                        "confidence": summary.get("confidence"),
                        "school_hits": summary.get("school_hits"),
                        "edu_emails": summary.get("edu_emails"),
                        "clues": summary.get("clues"),
                        "links": summary.get("links"),
                        "strong_pedigree_signal": strong,
                    },
                },
            },
            "trace": _append_trace({**state, "observation": observation, "action": "ENRICH_IDENTITY"}),
        }

    if action == "REQUEST_SCHOOL":
        queue = load_queue()
        key = state.get("dedup_key") or f"unknown:{uuid.uuid4().hex[:8]}"
        search = build_identity_search_links(cand)
        enrichment = state.get("enrichment") or (cand.get("signals") or {}).get("enrichment") or {}
        # Attach enrich links into identity_search for the CLI
        enrich_links = list(enrichment.get("links") or [])
        search = {
            **search,
            "enrichment_clues": enrichment.get("clues") or [],
            "enrichment_confidence": enrichment.get("confidence") or "none",
            "enrichment_schools": enrichment.get("school_hits") or [],
            "enrichment_edu_emails": enrichment.get("edu_emails") or [],
            "enrichment_links": enrich_links,
            "tip": (
                (search.get("tip") or "")
                + " Auto-enrichment already ran (GitHub/Kaggle/SO). "
                "Use enrichment links first; LinkedIn X-Ray only if still needed."
            ),
        }
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
            "identity_search": search,
            "enrichment": enrichment,
            "question": (
                f"Auto-enrichment confidence={enrichment.get('confidence', 'n/a')} for "
                f"{cand.get('name')} (login={search.get('github_login')}). "
                "Check enrichment links (blog/Kaggle/SO). If still unknown, LinkedIn X-Ray "
                "by GitHub login. Paste Education text, or: "
                "'LinkedIn not found. Nickname-only GitHub. Enrichment found no school.'"
            ),
        }
        save_queue(queue)
        top_search = (search.get("searches") or [{}])[0]
        observation = (
            "Paused for human identity + school validation after enrichment. "
            f"Enrichment clues: {len(enrichment.get('clues') or [])}. "
            f"Primary X-Ray: {top_search.get('url', 'n/a')}. "
            f"Resume: python scripts/validate_agent.py resume {key}"
        )
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
        auto_edu = (state.get("auto_education") or "").strip()
        evidence = education or auto_edu
        label = "HUMAN VALIDATION (LinkedIn education / pedigree)" if education else (
            "AUTO IDENTITY ENRICHMENT"
        )
        enriched_blob = f"{blob}\n\n--- {label} ---\n{evidence}\n"
        scored = score_candidate(
            role,
            enriched_blob,
            feedback_tail="",
            location=(cand.get("signals") or {}).get("location") or cand.get("location"),
        ) or {}
        new_score = int(scored.get("score") or 0)
        unverified = bool(scored.get("school_unverified", True))
        # If auto enrich had edu email / school hits, force-clear unverified when LLM misses it
        enrichment = state.get("enrichment") or {}
        if not education and enrichment.get("strong_pedigree_signal"):
            unverified = False
            flags = list(scored.get("flags") or [])
            flags = [f for f in flags if "school unverified" not in str(f).lower()]
            if "enrichment pedigree hit" not in [x.lower() for x in flags]:
                flags.append("enrichment pedigree hit")
        else:
            flags = list(scored.get("flags") or [])
        observation = (
            f"Re-scored after {label}: score={new_score}, "
            f"school_unverified={unverified}, flags={flags}"
        )
        return {
            "profile_blob": enriched_blob,
            "score": new_score,
            "school_unverified": unverified,
            "flags": flags,
            "reasoning": scored.get("reasoning", ""),
            "top_signal": scored.get("top_signal", ""),
            "observation": observation,
            "status": "running",
            "action": "RESCORE",
            "candidate": {
                **cand,
                "score": new_score,
                "reasoning": scored.get("reasoning", cand.get("reasoning", "")),
                "signals": {
                    **(cand.get("signals") or {}),
                    "school_unverified": unverified,
                    "flags": flags,
                    "top_signal": scored.get("top_signal", ""),
                    "validation": "human_education_applied" if education else "auto_enrichment_applied",
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
    # Continue ReAct loop after enrichment or rescore observations
    if status == "running" and action in {"RESCORE", "ENRICH_IDENTITY"}:
        return "reason"
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


def _run_loop(state: ValidationState, *, max_steps: int = 12) -> ValidationState:
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
        "enriched": False,
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
