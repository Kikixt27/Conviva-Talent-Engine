# Python pipeline (`pipeline/`)

Browser UI (`conviva_signal_v4_2.html`) is unchanged. This layer runs **batch** sourcing with **hard filters** before LLM calls to save cost.

## Entry points

| Command | Role |
| --- | --- |
| `python scripts/source.py` | CI / habit: thin forwarder → `pipeline.engine` |
| `python -m pipeline.scorer` | Same pipeline (imports `pipeline.engine.main`) |
| `python scripts/log_feedback.py …` | Append TA labels |
| `python -m pipeline.calibrate` | Feedback `summary.json` + golden-set placeholder |
| `python -m pipeline.query_refresh` | Query-refresh placeholder |

## Layout

- **`engine.py`** — canonical nightly run (`main()`): search → hard filter → LLM → persist + report.
- `utils.py` — `hard_filter_github` (incl. excluded non-US locations), `hard_filter_hackernews`, GitHub helpers.
- `engine.py` — LLM scoring + deterministic caps (`school_unverified` ≤60/65; non-US ≤30 for US priority roles). Role JSON may include `scoring_notes`.


## Role config

- Preferred: one file per role under `data/roles/*.json` (sorted by filename).
- Fallback: root `roles.json` if `data/roles/` has no `.json` files.

## Run outputs

- Canonical DB: `data/candidates.json` (unchanged for Actions).
- Optional append-only log: `data/candidates/YYYY-MM-DD.jsonl` (new rows from each run).

## Feedback

- Any `feedback/*.jsonl` except `*.example` is merged (sorted by `ts` descending for prompt injection).
- `python -m pipeline.calibrate` writes `feedback/summary.json` (label counts).
