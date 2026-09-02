# Conviva Signal

Sourcing automation for the Conviva TA team.

This repository contains:

- `conviva_signal_v4_2.html` — browser-based sourcing & scoring (unchanged; manual / interactive).
- `pipeline/` — **Python batch layer**: `utils.py` (hard filters before LLM), `scorer.py` (CLI alias), `calibrate.py` / `query_refresh.py` (stubs). See `pipeline/README.md`.
- `pipeline/engine.py` — **canonical** nightly engine (GitHub + HN → hard filter → LLM → `data/candidates.json` + daily JSONL + `reports/`).
- `scripts/source.py` — thin forwarder (same as `python -m pipeline.scorer` for CI / local habit).
- `scripts/log_feedback.py` — append TA labels to `feedback/*.jsonl`.
- `.github/workflows/source.yml` — weekday schedule + manual trigger.
- `roles.json` — **legacy** fallback if `data/roles/` has no `.json` files.
- `data/roles/*.json` — **preferred** one JSON file per open role (sorted load order).
- `data/candidates.json` — canonical candidate DB; `data/candidates/YYYY-MM-DD.jsonl` — append-only run log.
- `data/golden_set/` — golden examples for future calibration (`*.example` schema).
- `feedback/` — TA judgement JSONL + optional `summary.json` from `python -m pipeline.calibrate`.
- `reports/` — daily HTML reports.

---

## 1. One-time setup

### 1.1 Create the GitHub repository

1. Visit <https://github.com/new>.
2. Name it `Conviva-Talent-Engine` (or any name you prefer), keep it **Private**, and click **Create repository**.
3. From your local `Conviva Signal` folder, push the existing files:
   ```powershell
   git remote add origin https://github.com/<your-account>/Conviva-Talent-Engine.git
   git branch -M main
   git push -u origin main
   ```

### 1.2 Add API secrets

In your GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Required? | Where to get it |
| --- | --- | --- |
| `CLAUDE_API_KEY` | At least one of these | <https://console.anthropic.com> |
| `DS_API_KEY`     | At least one of these | <https://platform.deepseek.com> |
| `SLACK_WEBHOOK_URL` | Optional | <https://api.slack.com/apps> → Incoming Webhooks |

`GITHUB_TOKEN` is provided automatically — you do **not** need to create one.

### 1.3 Test it once manually

1. Go to the **Actions** tab in your repo.
2. Pick **Conviva Signal — Nightly Sourcing**.
3. Click **Run workflow → Run workflow**.
4. Wait 5–10 minutes, then:
   - Watch the live log.
   - Download the `signal-report` artifact from the run summary to view the HTML report.
   - Check your Slack channel if you wired up the webhook.

---

## 2. Day-to-day usage

### Editing the roles you source for

**Preferred:** edit one file per role under `data/roles/` (e.g. `principal_product_builder.json`). Files are loaded in **sorted filename order** — the nightly job runs **all** `.json` files in that folder (Beijing + Foster City roles together).

**Current pipeline roles** (as of latest config):

| File | Title | Location |
|------|--------|----------|
| `ai_engineer_agent_analytics.json` | AI Engineer, Agent Analytics & Optimization | **California (priority US hire)** |
| `senior_data_scientist.json` | Senior Data Scientist | **California (priority US hire)** |
| `principal_product_builder.json` | Principal Product Builder | China (Beijing) |
| `tech_lead_backend.json` | Staff Software Engineer — Tech Lead, Backend | China (Beijing) |

Nightly focus: US **AI Engineer** and **Senior Data Scientist** use broader California location + 6 GitHub/HN queries each (previously San Francisco was too narrow — near-zero MCP/LangGraph hits). Workflow defaults: `SCORE_THRESHOLD=65`, `MAX_CANDIDATES_PER_QUERY=20`.

**Legacy:** if `data/roles/` contains no `.json` files, the engine falls back to root `roles.json`.

Each role JSON accepts:

```json
{
  "title": "Display name",
  "language": "github primary language filter (optional)",
  "location": "github location filter (optional)",
  "requirements": "free-text description used by the LLM scorer",
  "github_queries": ["query1", "query2"],
  "hn_queries": ["query1"]
}
```

Commit and push the change — the next scheduled run picks it up automatically.

### Tuning thresholds

Edit `.github/workflows/source.yml` and adjust the `env:` block:

- `SCORE_THRESHOLD` — minimum score (0–100) for a candidate to be saved. Default `65` (workflow).
- `MAX_CANDIDATES_PER_QUERY` — search hits to enrich per query. Default `20` (workflow).
- `AI_PROVIDER` — `claude`, `deepseek`, or `auto` (try Claude first, fall back to DeepSeek).
- `CLAUDE_MODEL` — which Anthropic model to use. Default `claude-haiku-4-5` (cheapest, fastest). Other options: `claude-sonnet-4-5` (higher quality, ~5× cost), `claude-opus-4-5` (highest quality, ~25× cost).
- `DEEPSEEK_MODEL` — which DeepSeek model to use. Default `deepseek-chat`. Other option: `deepseek-reasoner` (more thorough, slower).

### Reading results

- Latest data lives in `data/candidates.json` (committed by the bot after each run).
- Daily HTML reports live in `reports/YYYY-MM-DD.html`.
- Slack digest gives you the top hit and the count.

---

## 3. Local development (optional)

Run the engine on your laptop without waiting for the cron schedule:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:CLAUDE_API_KEY = "sk-ant-..."
# or: $env:DS_API_KEY = "sk-..."
python scripts/source.py
```

The script writes to the same `data/` and `reports/` folders.

---

## 6. Feedback loop (HTML + Python “agent” path)

**Goal:** your judgements improve the next run **without** fine-tuning a model.

1. **Create** `feedback/entries.jsonl` (one JSON object per line). You can start from the example:
   ```powershell
   copy feedback\entries.jsonl.example feedback\entries.jsonl
   ```
2. **After** you review a daily report or `data/candidates.json`, log a label:
   ```powershell
   python scripts/log_feedback.py --source github --source-id 68322456 `
     --role "Principal Product Builder" --label reject --reason "Activity volume only, not PM ownership"
   ```
   - **source**: `github` | `hackernews` (must match `pipeline.engine` / `data/candidates.json` keys).
   - **source_id**: GitHub user **numeric** `id` (see `data/candidates.json` → `source_id`), or HN **username**.
   - **role**: must **exactly match** the `title` field in `data/roles/*.json` (or legacy `roles.json`) for role-specific calibration.
   - **label**: `reject` | `bad_fit` | `no` | `strong_fit` | `maybe`.
3. **Run** `python scripts/source.py` or `python -m pipeline.scorer` again. The scorer will receive:
   - Recent labels for **that role** + recent **rejections** across roles (few-shot style text).
   - Optional hard skip for rejected identities (saves API cost).

### Environment variables (feedback)

| Variable | Default | Meaning |
| --- | --- | --- |
| `FEEDBACK_FILE` | `feedback/entries.jsonl` | Path to your JSONL log. |
| `SKIP_REJECTED_CANDIDATES` | `0` | Set to `1` to **not score** candidates whose `source:source_id` was labelled `reject`, `bad_fit`, or `no`. They can still appear in search results; this avoids paying for another bad score. **Note:** removing a row from `data/candidates.json` alone does not block them — use this flag + feedback, or tighten queries. |

### Suggested workflow (solo TA)

| Step | Where |
| --- | --- |
| Morning triage | Open latest `reports/YYYY-MM-DD.html` or Slack digest |
| Label bad fits | `python scripts/log_feedback.py ...` |
| Commit feedback | `git add feedback/entries.jsonl && git commit -m "ta: feedback"` (private repo) |
| Nightly run | Actions runs `scripts/source.py` → `pipeline.engine` with your new calibration text |

### Layout (option 2 recap)

```
Conviva Signal/
├── conviva_signal_v4_2.html   # interactive copilot
├── pipeline/
│   ├── engine.py              # canonical: sourcing + scoring
│   ├── scorer.py              # CLI → engine.main()
│   ├── utils.py               # hard filters, GitHub helpers
│   └── …
├── scripts/
│   ├── source.py              # forwarder → pipeline.engine
│   └── log_feedback.py      # append TA labels
├── roles.json                 # legacy fallback
├── data/
│   ├── roles/*.json
│   ├── candidates.json
│   └── candidates/*.jsonl
├── feedback/
│   ├── entries.jsonl
│   └── entries.jsonl.example
└── reports/
```

If feedback reasons may contain **PII**, keep `feedback/entries.jsonl` local only (add that filename to `.gitignore`) and do not push it; the engine still works with an empty/missing file.

---

## 7. What this tool deliberately does NOT do

- **LinkedIn / Boss直聘 / 脉脉 / 小红书** — these platforms block automation and require login + CAPTCHA. Use `conviva_signal_v4_2.html` in your browser for those (paste profiles in, get scoring out).
- **Resume parsing from job-board APIs** — Naukri, Liepin, etc. do not expose candidate data publicly.
- **Final hiring decisions** — scoring is a triage signal, not a substitute for human judgement.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Workflow fails with `Set CLAUDE_API_KEY or DS_API_KEY` | Secret not added | Add the secret in repo Settings. |
| GitHub search returns 0 results | Query too specific or rate-limited | Loosen the query or wait an hour. |
| No Slack message | `SLACK_WEBHOOK_URL` missing or wrong | Re-create the webhook and update the secret. |
| LLM scoring keeps failing | Wrong key, expired billing, model name changed | Check provider dashboard; swap `AI_PROVIDER` to the working one. |
