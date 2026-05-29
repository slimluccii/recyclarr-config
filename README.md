# recyclarr-config

My [Recyclarr](https://recyclarr.dev) configuration, plus a daily **drift check** that tells me when my config has fallen out of sync with upstream — the [TRaSH Guides](https://trash-guides.info) and Recyclarr's own config schema.

## What this repo is

This repo holds my `recyclarr.yml` and runs a scheduled GitHub Actions job that watches for *upstream* changes I should know about. It does **not** sync anything and does **not** need access to my Sonarr/Radarr instances — it only inspects the config in this repo against publicly available upstream data. The actual `recyclarr sync` still runs where it always has (my server / locally).

When drift is detected, the workflow opens a single GitHub issue tagged **`recyclarr-drift`**. When everything is back in sync, it closes that issue automatically.

## Repo layout

```
.
├── recyclarr.yml                 # your config (root) — add this yourself
├── scripts/
│   └── check_drift.py            # the drift-check logic
├── schema-snapshot.json          # last-seen Recyclarr config schema (auto-committed)
└── .github/
    └── workflows/
        └── drift-check.yml        # daily scheduled workflow
```

## How the daily check works

`scripts/check_drift.py` runs two independent checks:

1. **Stale `trash_ids`** — every `trash_id` referenced in `recyclarr.yml` is validated
   against the current TRaSH Guides custom-format list (pulled via
   `recyclarr list custom-formats <service> --raw` in the official Docker image,
   no Sonarr/Radarr connection). IDs that no longer exist upstream (renamed,
   removed, restructured) are flagged.
2. **New schema properties** — fetches Recyclarr's current config JSON schema
   (the multi-file `$ref` tree at `schemas.recyclarr.dev/latest`) and diffs the
   set of property paths against `schema-snapshot.json`. New properties usually
   mean new recyclarr config features worth adopting.

### Auto-issue lifecycle

- All findings roll up into **one** issue labeled `recyclarr-drift`.
- If an issue is already open, it's updated in place rather than duplicated.
- When a run finds **no** drift, any open `recyclarr-drift` issue is **closed automatically**. One issue, self-healing.

### Alert-once schema snapshot

After reporting a schema diff, the workflow **commits the updated `schema-snapshot.json` back to the repo**. That means each new schema property alerts exactly once — the next run compares against the new baseline and stays quiet until the *next* upstream change. (The first run just establishes the baseline and raises no alert.)

## Schedule

- **Daily at 06:00 UTC** via `cron`.
- Plus a manual **"Run workflow"** button (`workflow_dispatch`) in the Actions tab.

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

**The CI never needs real secrets.** The drift check is upstream-only — it parses
the config and queries TRaSH / the schema; it never connects to Sonarr or Radarr.
The checker understands the `!env_var` tag and simply ignores the resolved value,
so **no secrets (real or dummy) need to be set in GitHub at all**.

Real values are only required where you actually run `recyclarr sync` — your
server or local machine — by exporting the matching environment variables there
(`SONARR_URL`, `SONARR_API_KEY`, `RADARR_URL`, `RADARR_API_KEY`, etc.).

## Getting started

1. Drop your `recyclarr.yml` in the repo root.
2. Commit and push.
3. Done — the daily check picks it up automatically.

Until `recyclarr.yml` exists, the trash_id check no-ops and the run stays green
(the schema check still runs), so you can set up the repo before the config is ready.

## Permissions

The workflow uses the built-in `GITHUB_TOKEN` — no PAT needed. It requests:

```yaml
permissions:
  contents: write   # commit the updated schema-snapshot.json
  issues: write     # open / update / close the recyclarr-drift issue
```
