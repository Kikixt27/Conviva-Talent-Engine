# Conviva Signal

Sourcing automation for the Conviva China TA team.

This repository contains:

- `conviva_signal_v4_2.html` — the browser-based sourcing & scoring tool (manual / interactive use).
- `scripts/source.py` — the nightly automation engine that searches GitHub, HackerNews, and Product Hunt, scores candidates with Claude or DeepSeek, and posts a Slack digest.
- `.github/workflows/source.yml` — the GitHub Actions schedule (08:00 Beijing on weekdays) plus manual trigger.
- `roles.json` — your editable list of roles and search queries.
- `data/candidates.json` — persistent candidate database (auto-updated by Actions).
- `reports/` — daily HTML reports (auto-generated; also uploaded as artifacts).

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

Open `roles.json` in any editor and add / change entries. Each role accepts:

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

- `SCORE_THRESHOLD` — minimum score (0–100) for a candidate to be saved. Default `70`.
- `MAX_CANDIDATES_PER_QUERY` — search hits to enrich per query. Default `15`.
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

## 4. What this tool deliberately does NOT do

- **LinkedIn / Boss直聘 / 脉脉 / 小红书** — these platforms block automation and require login + CAPTCHA. Use `conviva_signal_v4_2.html` in your browser for those (paste profiles in, get scoring out).
- **Resume parsing from job-board APIs** — Naukri, Liepin, etc. do not expose candidate data publicly.
- **Final hiring decisions** — scoring is a triage signal, not a substitute for human judgement.

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Workflow fails with `Set CLAUDE_API_KEY or DS_API_KEY` | Secret not added | Add the secret in repo Settings. |
| GitHub search returns 0 results | Query too specific or rate-limited | Loosen the query or wait an hour. |
| No Slack message | `SLACK_WEBHOOK_URL` missing or wrong | Re-create the webhook and update the secret. |
| LLM scoring keeps failing | Wrong key, expired billing, model name changed | Check provider dashboard; swap `AI_PROVIDER` to the working one. |
