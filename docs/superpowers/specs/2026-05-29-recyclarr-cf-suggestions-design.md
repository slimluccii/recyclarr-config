# Design: AI custom-format suggestions + persistent status issue

Date: 2026-05-29
Status: Approved (pending spec review)

## Problem

The repo already runs a drift check (stale `trash_ids`, new schema properties).
The user also wants **proactive suggestions**: which TRaSH-guide custom formats
(CFs) would suit their setup but aren't currently synced — e.g. "I run remux
1080p" → suggest the CFs that fit that tier.

Hard constraint, stated explicitly by the user: **the AI must never invent or
overwrite scores.** Scores always come from the guide on opt-in. Hand-picking
scores outside recyclarr's guide-backed system is exactly what causes false
scores. The AI's only job is a *relevance judgment* on which CFs fit — the user
(and recyclarr/the guide) handle scoring when they opt in.

## Key domain facts (grounding)

- recyclarr already auto-syncs the guide's **default** CFs at **correct guide
  scores** when you use guide-backed quality profiles + custom format groups. So
  the suggestion value is concentrated in CFs the user is *not* currently
  syncing — primarily `[Optional]` groups and non-default CFs that suit the
  user's profile intent.
- `recyclarr list custom-formats <service> --raw` returns the full guide CF
  catalog as TSV: `trash_id <TAB> name <TAB> category`. No Sonarr/Radarr
  connection needed. This is the candidate catalog.
- The user's *intent* is inferred from their config: which quality profiles
  (guide-backed `trash_id`s carry meaning, e.g. "Remux + WEB 1080p") and which
  CFs/groups are already referenced.

## Decisions (from brainstorming)

| Topic | Decision |
|-------|----------|
| What to suggest | CFs that fit the user's setup/intent and aren't synced. No scores. |
| Intent source | Inferred from `recyclarr.yml` (profiles + referenced CFs/groups). |
| AI runtime | Direct Anthropic API call, `ANTHROPIC_API_KEY` repo secret. |
| Model | `claude-haiku-4-5`, temperature 0. ~1 call/day. |
| Confidence filter | Show **all** suggestions, sorted by confidence (desc). |
| Delivery | **One always-open status issue**, updated in place, two sections. |
| Cadence | One workflow every 3h; AI step gated to run once/day (UTC hour 06). |

## Architecture

Three concerns, kept as separate, independently-testable units:

### 1. Drift check — `scripts/check_drift.py` (exists, minor change)
Unchanged logic for stale `trash_ids` + new schema props. It already writes
`/tmp/drift_result.json`. **Change:** it no longer drives issue open/close — its
output becomes one section of the status issue (see unit 3).

### 2. Suggestion engine — `scripts/suggest_cfs.py` (new)
Pure-ish unit: given the user config + guide catalog, produce ranked suggestions.

Inputs (gathered by the workflow before calling):
- `recyclarr.yml` (parsed; tolerates `!env_var` via the same custom loader as
  `check_drift.py` — factor that loader into a shared helper or duplicate the
  small constructor).
- `/tmp/cf_sonarr.txt`, `/tmp/cf_radarr.txt` — raw `list custom-formats --raw`
  output (already produced by the drift step; reuse, don't re-run).

Processing:
1. Extract user intent: per service, the set of quality-profile `trash_id`s and
   the set of already-referenced CF `trash_id`s / group `trash_id`s.
2. Build candidate set: guide CFs (trash_id, name, category) **minus** CFs the
   user already references.
3. Call Anthropic API once per service (or once combined) with:
   - System prompt: role = conservative recyclarr/TRaSH advisor; rules = never
     output scores; only judge fit vs the user's profiles; ground rationale in
     CF name/category; it is fine to return an empty list.
   - User content: the user's profile trash_ids (+ human names if resolvable),
     the already-synced CF list, and the candidate CF catalog.
   - Structured JSON output (tool/JSON mode), temperature 0.
4. Validate + sort results by confidence desc.

Output: writes `suggestions.json` (committed back to the repo) shaped as:
```json
{
  "generated_for_config_hash": "<sha256 of recyclarr.yml>",
  "sonarr": [
    {"trash_id": "...", "name": "...", "category": "...",
     "why_it_fits": "...", "confidence": "high|medium|low"}
  ],
  "radarr": [ ... ]
}
```
`generated_for_config_hash` lets the daily run skip the API call when the config
is unchanged AND the guide catalog is unchanged (optional cost optimization;
v1 may always call — see Open question O1).

No-intent guard: if the config has no quality profiles / referenced CFs, the
engine returns empty lists and the workflow records "no intent to infer."

### 3. Status issue assembler — `scripts/build_status_issue.py` (new) or a step
Builds the full status-issue markdown body from two persisted parts:
- `/tmp/drift_result.json` (fresh every run)
- `suggestions.json` (refreshed daily; read as-is on the 3h drift-only runs)

Body layout:
```
## Must-fix drift
<stale trash_ids + new schema props, or "✅ No drift detected.">

## Suggestions (advisory — no scores applied)
<ranked CF suggestions per service, each: name `trash_id` — why, confidence>
<or "Add quality profiles/CFs to recyclarr.yml to get suggestions.">

<small footer: last drift check + last suggestion refresh timestamps>
```

Issue management (gh CLI, `GITHUB_TOKEN`):
- Single issue labeled `recyclarr-status`. Find by label; **create if missing,
  else edit in place**. Never auto-close. (Migration: the old `recyclarr-drift`
  auto-close behavior is removed; if an open `recyclarr-drift` issue exists from
  earlier runs, the workflow closes it once with a pointer comment.)

## Workflow changes — `.github/workflows/drift-check.yml`

- Keep single cron `0 */3 * * *` + `workflow_dispatch`.
- Steps:
  1. checkout, setup python, `pip install pyyaml` (+ no extra dep for the API
     call — use stdlib `urllib`, or `pip install anthropic`; prefer the SDK for
     correctness — decide in plan, lean SDK).
  2. Dump recyclarr CFs (`--raw`) → `/tmp/cf_*.txt` (already present).
  3. `python scripts/check_drift.py` → `/tmp/drift_result.json` + snapshot.
  4. **AI suggestion step, gated**: run only when `[ "$(date -u +%H)" = "06" ]`
     OR the trigger is `workflow_dispatch`. Runs `scripts/suggest_cfs.py`,
     refreshing `suggestions.json`. Env: `ANTHROPIC_API_KEY`.
  5. Assemble status body + upsert the `recyclarr-status` issue (never close).
  6. Commit back any changed `schema-snapshot.json` **and** `suggestions.json`
     (single commit, only if changed).
- `permissions: contents: write, issues: write` (unchanged).

## Safety guardrails (the "be careful" requirement)

- AI output schema has **no score field**; the assembler never writes scores.
- Suggestions are advisory text only; nothing is ever applied to `recyclarr.yml`
  automatically.
- temperature 0 + structured output for determinism.
- System prompt explicitly permits/encourages an empty list and forbids scores.
- All suggestions carry a confidence label and a guide-grounded rationale so the
  user can judge before opting in.

## Testing

- `suggest_cfs.py`: unit-test intent extraction and candidate-set construction
  with fixture configs + fixture `--raw` catalogs (no network). Mock/stub the
  Anthropic call; assert the request payload excludes scores and the response
  parser rejects any score-bearing field.
- `build_status_issue.py`: snapshot-test body assembly for the four states
  (drift only / suggestions only / both / neither).
- `check_drift.py`: existing behavior preserved (already smoke-tested).
- End-to-end dry run via `workflow_dispatch` on GitHub.

## Open questions

- **O1 (cost optimization):** Should the daily run skip the API call when
  `recyclarr.yml` and the guide catalog are both unchanged since
  `suggestions.json` was generated? Lean **yes** (cheap to implement via the
  stored config hash + a catalog hash), but acceptable to defer to v2.
- **O2 (per-service vs combined call):** One API call with both services, or one
  per service. Lean **combined** (cheaper, model handles the split). Decide in
  plan.

## Out of scope (YAGNI)

- Declared-intent preferences file (user chose infer-from-config).
- Auto-applying suggestions to the config.
- Any score computation or override logic.
- Non-Anthropic model providers.
