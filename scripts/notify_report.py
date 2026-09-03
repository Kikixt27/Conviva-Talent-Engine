"""Post Slack (or optional email) with a clickable GitHub report link after push.

Used by Actions *after* `git push` so the report URL already exists on main.

Env:
  SLACK_WEBHOOK_URL — Incoming Webhook (required for Slack)
  GITHUB_REPOSITORY — owner/repo (set automatically in Actions)
  REPORT_DATE — YYYY-MM-DD (default: today UTC)
  REPORT_PATH — local path to HTML (optional; used only for existence check)
  DIGEST_JSON — optional path written by engine with candidate summary
  NOTIFY_EMAIL_TO — optional comma-separated emails (needs RESEND_API_KEY)
  RESEND_API_KEY — optional; if set with NOTIFY_EMAIL_TO, emails HTML report
  RESEND_FROM — optional from address (default: Conviva Signal <onboarding@resend.dev>)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
HTTP_TIMEOUT = 30


def report_github_url(repo: str, date: str) -> str:
    return f"https://github.com/{repo}/blob/main/reports/{date}.html"


def load_digest(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def post_slack(text: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("SLACK_WEBHOOK_URL not set — skip Slack")
        return
    r = requests.post(url, json={"text": text}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    print("Slack report link posted.")


def send_email_resend(*, to_list: list[str], subject: str, html_body: str, report_file: Path | None) -> None:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        print("RESEND_API_KEY not set — skip email")
        return
    from_addr = os.environ.get("RESEND_FROM", "Conviva Signal <onboarding@resend.dev>").strip()
    payload: dict = {
        "from": from_addr,
        "to": to_list,
        "subject": subject,
        "html": html_body,
    }
    # Resend attachments: base64 — keep small reports only
    if report_file and report_file.exists() and report_file.stat().st_size < 900_000:
        import base64

        payload["attachments"] = [
            {
                "filename": report_file.name,
                "content": base64.b64encode(report_file.read_bytes()).decode("ascii"),
            }
        ]
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    if not r.ok:
        print(f"Resend email failed: {r.status_code} {r.text[:300]}")
        r.raise_for_status()
    print(f"Email sent via Resend to {', '.join(to_list)}")


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip() or "Kikixt27/Conviva-Talent-Engine"
    date = os.environ.get("REPORT_DATE", "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = Path(os.environ.get("REPORT_PATH", str(ROOT / "reports" / f"{date}.html")))
    digest = load_digest(Path(os.environ.get("DIGEST_JSON", str(ROOT / "reports" / "latest_digest.json"))))

    url = report_github_url(repo, date)
    n = int(digest.get("count", 0) or 0)
    lines = [
        f":page_facing_up: *Conviva Signal report — {date}*",
        f"<{url}|Open HTML report on GitHub>",
        f":white_check_mark: Ready: *{n}* · :warning: Needs validation: *{digest.get('needs_validation_count', 0)}* "
        f"(score ≥ {digest.get('threshold', '?')} for ready)",
    ]
    if digest.get("top"):
        lines.append("*Ready*")
    for row in (digest.get("top") or [])[:8]:
        name = row.get("name", "?")
        score = row.get("score", "?")
        role = row.get("role", "")
        profile = row.get("profile_url", "")
        flags = row.get("flags") or []
        flag_s = f" · {', '.join(flags)}" if flags else ""
        if profile:
            lines.append(f"• *{score}* <{profile}|{name}> — {role}{flag_s}")
        else:
            lines.append(f"• *{score}* {name} — {role}{flag_s}")
    needs = digest.get("needs_validation") or []
    if needs:
        lines.append("*Needs validation* (school) — resume with validate_agent.py")
        for row in needs[:8]:
            name = row.get("name", "?")
            score = row.get("score", "?")
            key = row.get("dedup_key", "")
            profile = row.get("profile_url", "")
            if profile:
                lines.append(f"• *{score}* <{profile}|{name}> · `{key}`")
            else:
                lines.append(f"• *{score}* {name} · `{key}`")

    if not report_path.exists():
        lines.append("_Note: local report file missing in this runner; link still points at main after push._")

    try:
        post_slack("\n".join(lines))
    except requests.RequestException as exc:
        print(f"Slack failed: {exc}")
        return 1

    email_to = [e.strip() for e in os.environ.get("NOTIFY_EMAIL_TO", "").split(",") if e.strip()]
    if email_to:
        html = (
            f"<h2>Conviva Signal — {date}</h2>"
            f"<p><a href=\"{url}\">Open HTML report on GitHub</a></p>"
            f"<p>{n} new candidate(s).</p><ul>"
            + "".join(
                f"<li><b>{r.get('score')}</b> "
                f"<a href=\"{r.get('profile_url')}\">{r.get('name')}</a> — {r.get('role')}</li>"
                for r in (digest.get("top") or [])[:15]
            )
            + "</ul>"
        )
        try:
            send_email_resend(
                to_list=email_to,
                subject=f"Conviva Signal report {date} ({n} new)",
                html_body=html,
                report_file=report_path if report_path.exists() else None,
            )
        except requests.RequestException as exc:
            print(f"Email failed: {exc}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
