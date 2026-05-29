# recyclarr-config

My [Recyclarr](https://recyclarr.dev) configuration, plus a scheduled **drift check** and **AI custom-format suggestions** that keep me in sync with upstream — the [TRaSH Guides](https://trash-guides.info) and Recyclarr's own config schema.

## What this repo is

This repo holds my `recyclarr.yml` and runs a scheduled GitHub Actions job that watches for *upstream* changes I should know about. It does **not** sync anything and does **not** need access to my Sonarr/Radarr instances — it only inspects the config in this repo against publicly available upstream data. The actual `recyclarr sync` still runs where it always has (my server / locally).

Everything surfaces in **one persistent GitHub issue** labeled **`recyclarr-status`** — a standing dashboard with two sections (must-fix drift + advisory suggestions), updated in place on every run and never auto-closed.

## Repo layout

```
.
├── recyclarr.yml                 # config (root) — starts as a template, replace with yours
├── scripts/
│   ├── check_drift.py            # drift detection (stale trash_ids + new schema props)
│   ├── suggest_cfs.py            # AI custom-format suggestions (gated ~once/day)
│   └── build_status_issue.py     # assembles the status-issue body
├── schema-snapshot.json          # last-seen Recyclarr config schema (auto-committed)
├── suggestions.json              # last AI suggestion set (auto-committed)
├── tests/                        # pytest unit tests (no network)
└── .github/
    └── workflows/
        └── drift-check.yml        # scheduled workflow
```

## What it checks

**Drift** — `scripts/check_drift.py`, every run:

1. **Stale `trash_ids`** — every `trash_id` in `recyclarr.yml` is validated against
   the current TRaSH Guides custom-format list (`recyclarr list custom-formats
   <service> --raw` in the official Docker image, no Sonarr/Radarr connection).
   IDs that no longer exist upstream are flagged.
2. **New schema properties** — fetches Recyclarr's config JSON schema (the
   multi-file `$ref` tree at `schemas.recyclarr.dev/latest`) and diffs the property
   paths against `schema-snapshot.json`. New properties usually mean new config
   features worth adopting.

**Suggestions** — `scripts/suggest_cfs.py`, gated to ~once/day:

Infers your intent from the quality profiles + custom formats already in
`recyclarr.yml`, then asks Claude (Haiku 4.5) which guide CFs you're *not* syncing
would suit that setup — each with a rationale and confidence, ranked. **The AI
never invents or applies scores** (scores always come from the guide on opt-in);
it only judges *fit*. Suggestions are advisory — nothing is ever applied to your
config automatically.

### Status issue

- All findings roll up into **one** issue labeled `recyclarr-status`, created once
  and edited in place. It is **never auto-closed** — it's a standing dashboard.
- Section 1 = must-fix drift (or "✅ No drift detected"). Section 2 = ranked
  suggestions (or a prompt to add a config).

### Auto-committed state (alert-once)

The workflow commits `schema-snapshot.json` and `suggestions.json` back to the repo
when they change. So each new schema property alerts exactly once (next run
compares against the new baseline), and unchanged config + catalog **skips the API
call entirely** (suggestions are reused from the committed file).

## Schedule

- **Every 3 hours** via `cron` (`0 */3 * * *`, UTC) for the drift check — light (a
  docker `list` + a ~15KB schema fetch). On a **private** repo this consumes Actions
  minutes (~8 runs/day); public repos run free.
- The **AI suggestion step is gated to the 06:00 UTC tick** (≈1 API call/day) to keep
  cost down; the other ticks reuse the committed `suggestions.json`.
- A manual **"Run workflow"** button (`workflow_dispatch`) runs both, including a
  fresh suggestion refresh on demand.

## Environment variables

Recyclarr reads service credentials with its `!env_var` YAML tag, which pulls a value from an environment variable at runtime instead of hardcoding it:

```yaml
sonarr:
  main:
    base_url: !env_var SONARR_URL
    api_key: !env_var SONARR_API_KEY
    quality_definition:
      type: series
    custom_formats:
      - trash_ids:
          - 85c61753df5da1fb2aab6f2a47426b09  # BR-DISK

radarr:
  main:
    base_url: !env_var RADARR_URL
    api_key: !env_var RADARR_API_KEY
```

**The CI never needs your Sonarr/Radarr secrets.** The checks are upstream-only —
they parse the config and query TRaSH / the schema; they never connect to Sonarr or
Radarr. The scripts understand the `!env_var` tag and ignore the resolved value, so
**no `*arr` credentials (real or dummy) need to be set in GitHub at all**.

Real `*arr` values are only required where you actually run `recyclarr sync` — your
server or local machine — by exporting the matching environment variables there
(`SONARR_URL`, `SONARR_API_KEY`, `RADARR_URL`, `RADARR_API_KEY`, etc.).

The **one** secret CI does need is **`ANTHROPIC_API_KEY`** (repo → Settings →
Secrets → Actions), used by the daily suggestion step. Without it, the drift check
still runs fine; the suggestion step just logs a warning and skips.

## Getting started

The repo ships a **template `recyclarr.yml`** with the structure and `!env_var`
credentials wired up but no active `trash_ids` (so it raises no drift). To go live:

1. Replace `recyclarr.yml` with your real config (or uncomment + fill the template).
2. Commit and push.
3. Done — the next run validates your `trash_ids` automatically.

While the config has no `trash_ids` referenced, the trash_id check no-ops and the
run stays green (the schema check still runs).

## Permissions

The workflow uses the built-in `GITHUB_TOKEN` — no PAT needed. It requests:

```yaml
permissions:
  contents: write   # commit updated schema-snapshot.json + suggestions.json
  issues: write     # create / update the persistent recyclarr-status issue
```

The only external secret is `ANTHROPIC_API_KEY` (see Environment variables above).
