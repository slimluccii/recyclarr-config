# recyclarr-config

My [Recyclarr](https://recyclarr.dev) configuration, plus a scheduled **drift check** and **AI custom-format suggestions** that keep me in sync with upstream — the [TRaSH Guides](https://trash-guides.info) and Recyclarr's own config schema.

## What this repo is

This repo holds my `recyclarr.yml` and runs a scheduled GitHub Actions job that watches for *upstream* changes I should know about. It does **not** sync anything and does **not** need access to my Sonarr/Radarr instances — it only inspects the config in this repo against publicly available upstream data. The actual `recyclarr sync` still runs where it always has (my server / locally).

Every actionable finding becomes its **own pull request** — one self-contained, reviewable change per PR — using four labels: **`drift`**, **`suggestion`**, **`settings`**, and **`feature`**. A change I can apply as a single edit to `recyclarr.yml` opens a PR I can merge or close; a change I *can't* express as a patch (a brand-new schema property) opens a labeled **issue** as a heads-up instead.

## Repo layout

```
.
├── recyclarr.yml                 # config (root) — starts as a template, replace with yours
├── scripts/
│   ├── check_drift.py            # drift detection (stale trash_ids + new schema props)
│   ├── suggest_cfs.py            # AI custom-format suggestions (gated ~once/day)
│   ├── recyclarr_patch.py        # round-trip YAML editor (one surgical edit per PR)
│   └── manage_prs.py             # opens/updates/auto-closes per-change PRs + feature issues
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
would suit that setup — each with a rationale and confidence. **The AI never
invents or applies scores** (scores always come from the guide on opt-in); it only
judges *fit*, and the IDs it suggests come strictly from the guide's CF list (it
never invents a `trash_id`). Each fit becomes a suggestion PR (see below).

### Per-change PRs

Every actionable finding is turned into **its own pull request**, built by applying
exactly one surgical edit to the current `recyclarr.yml` via the round-trip editor
(`scripts/recyclarr_patch.py`), so the diff is a single change with all your
comments and `!env_var` lines preserved. `scripts/manage_prs.py` reconciles these
on every run:

- **`drift`** — removes a stale `trash_id` (gone upstream) or adjusts to match.
- **`suggestion`** — adds a guide CF the AI judged a fit. A suggestion PR **always
  opens with a best-guess `assign_scores_to` profile**, and when the AI isn't
  confident the PR is **flagged as uncertain** so you know to double-check the
  profile before merging. **No score is ever written** — `assign_scores_to` carries
  only the profile name; scores come from the guide on opt-in.
- **`settings`** — sets a config value at a dotted path to align with a recyclarr
  template (values come only from those templates, never invented).
- **`feature`** — a brand-new schema property that can't be expressed as a patch.
  This opens a labeled **issue** (a heads-up to review for adoption), not a PR.

**Stable branches + auto-close.** Each change uses a stable branch
(`recyclarr/<type>/<key>`), so a finding maps to the *same* PR run after run rather
than spawning duplicates. When a finding is **no longer relevant** (e.g. the stale
id is gone, or the suggestion now matches your config), its PR is **closed
automatically**.

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
  contents: write        # commit schema-snapshot.json + suggestions.json; push change branches
  pull-requests: write   # open / update / auto-close the per-change PRs
  issues: write          # open / update the `feature` heads-up issues
```

**Required repo setting.** Because the workflow opens PRs with the built-in token,
you must enable **Settings → Actions → General → Workflow permissions → "Allow
GitHub Actions to create and approve pull requests."** Without it, GitHub blocks the
PR-creation step and the `drift` / `suggestion` / `settings` PRs never appear (the
drift check itself still runs).

The only external secret is `ANTHROPIC_API_KEY` (see Environment variables above).
