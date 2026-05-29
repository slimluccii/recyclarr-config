#!/usr/bin/env python3
"""
manage_prs.py - Per-change PR + feature-issue lifecycle reconciler.

Runs in GitHub Actions (after check_drift.py and suggest_cfs.py). It is the
*write* side of the pipeline: it turns the change-records produced upstream into
GitHub pull requests (one PR per change) and feature issues (one issue per
schema feature that has no automatable patch), and it reconciles away anything
that no longer applies.

It shells out to `git` and `gh` (both preinstalled on ubuntu-latest; `GH_TOKEN`
is provided in the environment). It NEVER imports the GitHub API directly --
`gh` already carries auth, retries, and pagination, and matches how the rest of
the workflow talks to GitHub.

INPUTS (produced earlier in the run; we only read them):
  /tmp/drift_changes.json  written by check_drift.py
        {"changes": [<drift change-records>], "feature_issues": [<feature recs>]}
  /tmp/ai_changes.json     written by suggest_cfs.py
        {"changes": [<suggestion + settings change-records>]}

  A change-record (the unit of work a PR is built from) is:
        {
          "type": "drift" | "suggestion" | "settings",
          "label": "drift" | "suggestion" | "settings",
          "key": "<stable id: trash_id or dotted setting path>",
          "service": "sonarr" | "radarr" | null,
          "branch": "recyclarr/<type>/<sanitized key>",
          "title": "<PR title>",
          "body": "<PR body markdown>",
          "new_config": "<FULL recyclarr.yml with ONLY this one change applied>",
          "confidence": "high" | "medium" | "low" | null,
          "uncertain": bool
        }

  A feature-issue record (no patch possible -> an issue, not a PR) is:
        {"key": "<schema dotted path>", "title": "<...>", "body": "<...>"}

WHAT IT DOES (idempotent; safe to run on every CI tick):
  1. Ensure the four labels exist (gh label create --force): drift, suggestion,
     settings, feature -- each a distinct colour.
  2. For each change-record:
       * If no remote branch exists: create it from origin/main, write
         new_config to recyclarr.yml, commit ("<label>: <title>"), push, and
         `gh pr create` with the matching label/title/body.
       * If a branch/PR already exists: REFRESH. If the recyclarr.yml committed
         on the branch differs from the freshly-rendered new_config, update it
         (commit + push) and `gh pr edit` the body; otherwise leave it untouched
         (no empty commits, no needless edits).
  3. Reconcile STALE PRs: list every open PR carrying one of our four labels;
     for any whose branch is NOT in the current change-set, `gh pr close` it and
     delete the remote branch (with a comment explaining why).
  4. Feature issues: ensure exactly one open `feature`-labeled issue per
     feature-record key (matched via a hidden HTML key-marker in the body);
     create the missing ones, leave the present ones, and close any open feature
     issue whose key is no longer present.
  5. MIGRATION (one-shot): close the legacy open `recyclarr-status` issue once,
     with a comment pointing at the new PR-based flow.

SAFETY / CLEANLINESS:
  * Never force-push, never touch `main` (we only ever read origin/main).
  * Every per-change branch is built in an isolated `git worktree`, so the
    runner's primary checkout is never left dirty between changes and a failure
    on one change cannot corrupt the working tree for the next.
  * Per-item errors are logged and swallowed -- one bad change (or one flaky `gh`
    call) must NOT abort the rest of the reconciliation. We exit non-zero ONLY on
    a catastrophic setup failure (e.g. git identity / origin unavailable).

Exit code:
  - 0 on normal completion, even if individual changes/issues failed (logged).
  - Non-zero ONLY on catastrophic setup failure before any per-item work.
"""

import json
import os
import re
import subprocess
import sys
import tempfile


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_NAME = "recyclarr.yml"   # path relative to a checkout/worktree root

DRIFT_CHANGES_PATH = "/tmp/drift_changes.json"
AI_CHANGES_PATH = "/tmp/ai_changes.json"

BASE_BRANCH = "main"
REMOTE = "origin"

# The four labels we own, each with a distinct colour + description. These are
# the ONLY labels we treat as "ours" when reconciling stale PRs/issues.
LABELS = {
    "drift":      ("B60205", "Upstream-alignment fix (stale trash_id / removal)"),
    "suggestion": ("1D76DB", "AI-suggested custom format that fits your setup"),
    "settings":   ("5319E7", "Suggested recyclarr setting change"),
    "feature":    ("0E8A16", "New recyclarr schema feature (no auto-patch)"),
}

# The labels that ride on PRs (feature lives on issues only).
PR_LABELS = ("drift", "suggestion", "settings")

# Legacy issue from the previous (status-dashboard) design. Closed once on first
# run of this reconciler. Matched by its label so we don't hard-code issue #1
# (the number can differ across forks/re-creations).
LEGACY_STATUS_LABEL = "recyclarr-status"

# Hidden marker embedded in a feature issue body so we can find "our" issue for a
# given schema key reliably, even if the human-facing title later changes. It is
# an HTML comment, so it renders invisibly on GitHub.
FEATURE_KEY_MARKER = "<!-- recyclarr-feature-key: {} -->"
FEATURE_KEY_RE = re.compile(r"<!-- recyclarr-feature-key:\s*(.+?)\s*-->")

# Bot identity used for the per-change commits (matches the rest of the workflow).
GIT_USER_NAME = "github-actions[bot]"
GIT_USER_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #
#
# Two thin wrappers around subprocess. `run()` raises on non-zero (used for the
# few setup steps where failure IS catastrophic). `run_soft()` never raises --
# it returns a (ok, stdout, stderr) tuple so per-item callers can log-and-skip
# without aborting the whole reconciliation.

def run(args, cwd=None, input_text=None):
    """
    Run a command and RAISE (CalledProcessError) on failure. Use only for steps
    where a failure should abort the run (git identity, origin fetch).
    Returns stdout (str).
    """
    proc = subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return proc.stdout


def run_soft(args, cwd=None, input_text=None):
    """
    Run a command, NEVER raise. Returns (ok: bool, stdout: str, stderr: str).
    On a non-zero exit we log a concise warning and report ok=False so the caller
    can degrade gracefully (the spec: one bad item must not abort the rest).
    """
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        # e.g. the binary isn't on PATH -- treat as a soft failure here; the
        # caller decides whether it's fatal.
        print("WARNING: could not execute {}: {}".format(args[0], exc),
              file=sys.stderr)
        return False, "", str(exc)

    if proc.returncode != 0:
        # Keep the log readable: show the command (without leaking token args --
        # gh reads GH_TOKEN from env, never argv) and a trimmed stderr.
        print("WARNING: command failed ({}): {}\n  stderr: {}".format(
            proc.returncode, " ".join(args), (proc.stderr or "").strip()),
            file=sys.stderr)
    return proc.returncode == 0, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# Input loading (defensive)
# --------------------------------------------------------------------------- #

def load_json(path):
    """
    Load a JSON object from `path`. Returns {} for missing/unreadable/invalid
    files so a missing upstream artifact degrades to "no changes" rather than a
    crash. Always returns a dict.
    """
    if not os.path.isfile(path):
        print("note: {} not present; treating as no changes".format(path))
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("WARNING: could not read {}: {}".format(path, exc), file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _as_list(value):
    """Coerce a value into a list (defensive); non-lists -> empty list."""
    return value if isinstance(value, list) else []


def valid_change(rec):
    """
    A change-record is usable only if it carries the fields we need to act on:
    a branch, a label we own (one of PR_LABELS), a title, and the rendered
    new_config text. Anything missing -> skip (logged by the caller).
    """
    if not isinstance(rec, dict):
        return False
    if rec.get("label") not in PR_LABELS:
        return False
    for field in ("branch", "title", "new_config"):
        val = rec.get(field)
        if not isinstance(val, str) or not val.strip():
            return False
    # A branch must look like one of ours so reconciliation can recognise it.
    if not rec["branch"].startswith("recyclarr/"):
        return False
    return True


def valid_feature(rec):
    """A feature-issue record needs a key, a title, and a body."""
    if not isinstance(rec, dict):
        return False
    for field in ("key", "title", "body"):
        val = rec.get(field)
        if not isinstance(val, str) or not val.strip():
            return False
    return True


def collect_changes():
    """
    Combine change-records from both inputs into one de-duplicated list, plus the
    feature-issue records. De-dup is by branch name (the stable per-change id):
    if two records somehow target the same branch, the first wins and the dup is
    logged -- this keeps us from racing two writes against one branch.

    Also returns `reconcilable_labels`: the set of PR labels whose producer
    actually ran this tick, so stale-PR reconciliation only closes categories we
    truly re-evaluated. This is critical: the AI step is gated (skipped on most 3h
    ticks) and on a skip the workflow writes {"changes":[], "evaluated": false}.
    Without this guard, every skipped tick would close all open suggestion/settings
    PRs (empty active set) and 06:00 would reopen them -- endless churn. drift runs
    every tick, so `drift` is reconcilable whenever drift_changes.json is present.
    """
    drift = load_json(DRIFT_CHANGES_PATH)
    ai = load_json(AI_CHANGES_PATH)

    # A source counts as "evaluated" when its file is present (non-empty dict).
    # ai additionally honors an explicit "evaluated": false written by the gated
    # workflow skip branch; a present ai file WITHOUT the flag defaults to True
    # (suggest_cfs.py ran and just doesn't set it).
    drift_evaluated = bool(drift)
    ai_evaluated = bool(ai) and ai.get("evaluated", True)
    reconcilable_labels = set()
    if drift_evaluated:
        reconcilable_labels.add("drift")
    if ai_evaluated:
        reconcilable_labels.update(("suggestion", "settings"))

    raw_changes = _as_list(drift.get("changes")) + _as_list(ai.get("changes"))
    raw_features = _as_list(drift.get("feature_issues")) + _as_list(ai.get("feature_issues"))

    changes = []
    seen_branches = set()
    for rec in raw_changes:
        if not valid_change(rec):
            print("WARNING: skipping malformed change-record: {!r}".format(rec)[:200],
                  file=sys.stderr)
            continue
        branch = rec["branch"]
        if branch in seen_branches:
            print("WARNING: duplicate change for branch {}; keeping first"
                  .format(branch), file=sys.stderr)
            continue
        seen_branches.add(branch)
        changes.append(rec)

    features = []
    seen_keys = set()
    for rec in raw_features:
        if not valid_feature(rec):
            print("WARNING: skipping malformed feature-record: {!r}".format(rec)[:200],
                  file=sys.stderr)
            continue
        key = rec["key"]
        if key in seen_keys:
            print("WARNING: duplicate feature for key {}; keeping first"
                  .format(key), file=sys.stderr)
            continue
        seen_keys.add(key)
        features.append(rec)

    return changes, features, reconcilable_labels


# --------------------------------------------------------------------------- #
# Git / worktree setup
# --------------------------------------------------------------------------- #

def ensure_git_identity():
    """
    Configure the bot identity locally (commits made in worktrees inherit the
    repo config). Catastrophic on failure -- we cannot author commits without it.
    """
    run(["git", "config", "user.name", GIT_USER_NAME], cwd=REPO_ROOT)
    run(["git", "config", "user.email", GIT_USER_EMAIL], cwd=REPO_ROOT)


def fetch_base():
    """
    Fetch the base branch so origin/main is current. Catastrophic on failure --
    every per-change branch is cut from origin/main and a stale base would build
    PRs against the wrong tree.
    """
    run(["git", "fetch", REMOTE, BASE_BRANCH], cwd=REPO_ROOT)


def remote_branch_exists(branch):
    """True if `branch` exists on the remote. Soft -- a lookup failure -> False."""
    ok, out, _ = run_soft(
        ["git", "ls-remote", "--heads", REMOTE, branch], cwd=REPO_ROOT)
    return ok and bool(out.strip())


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

def ensure_labels():
    """
    Ensure all four labels exist with their colours/descriptions. `--force`
    makes this idempotent (creates or updates). Soft: a label hiccup must not
    abort the run (a missing label only means a PR is created unlabeled, which
    reconciliation tolerates because it ALSO matches our branch prefix).
    """
    for name, (color, desc) in LABELS.items():
        run_soft([
            "gh", "label", "create", name,
            "--color", color,
            "--description", desc,
            "--force",
        ], cwd=REPO_ROOT)


# --------------------------------------------------------------------------- #
# Per-change PR creation / refresh (each in an isolated worktree)
# --------------------------------------------------------------------------- #

def _write_config(worktree, text):
    """Write the rendered new_config into the worktree's recyclarr.yml verbatim."""
    path = os.path.join(worktree, CONFIG_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _config_on_branch_matches(worktree, desired_text):
    """
    True if the recyclarr.yml currently committed on this branch's worktree is
    byte-identical to the freshly-rendered desired_text. Used to decide whether a
    refresh needs a new commit (avoids empty commits / churn).
    """
    path = os.path.join(worktree, CONFIG_NAME)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read() == desired_text
    except OSError:
        return False


def process_change(rec, worktree_root):
    """
    Create or refresh the PR for a single change-record, inside an isolated
    worktree. Every failure here is SOFT: logged, then we move on to the next
    change (the spec: one bad change must not abort the rest).

    Flow:
      * Add a worktree for the change branch.
          - new branch:    `git worktree add -b <branch> <dir> origin/main`
          - existing branch: fetch it, then `git worktree add <dir> <branch>`
      * Write new_config, and:
          - new branch:      always commit + push, then `gh pr create`.
          - existing branch: commit + push ONLY if the file changed, then
                             `gh pr edit` the body to keep it current.
      * Always remove the worktree afterwards (in a finally) so the runner stays
        clean even if a step fails midway.
    """
    branch = rec["branch"]
    label = rec["label"]
    title = rec["title"]
    body = rec["body"] if isinstance(rec.get("body"), str) else ""
    new_config = rec["new_config"]

    exists = remote_branch_exists(branch)
    # A filesystem-safe worktree dir name derived from the branch.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", branch)
    worktree = os.path.join(worktree_root, safe)

    # --- materialise the worktree ------------------------------------------- #
    if exists:
        # Make the remote branch available locally, then check it out into a
        # detached/ tracking worktree. We fetch into a local ref of the same name.
        ok, _, _ = run_soft(
            ["git", "fetch", REMOTE, "{0}:{0}".format(branch)], cwd=REPO_ROOT)
        if not ok:
            # Could be that the local ref already exists; try a plain fetch then
            # add the worktree from the remote-tracking ref instead.
            run_soft(["git", "fetch", REMOTE, branch], cwd=REPO_ROOT)
        added, _, _ = run_soft(
            ["git", "worktree", "add", worktree, branch], cwd=REPO_ROOT)
        if not added:
            # Fall back to the remote-tracking ref (origin/<branch>) if the local
            # branch ref couldn't be checked out for any reason.
            added, _, _ = run_soft(
                ["git", "worktree", "add", "-B", branch, worktree,
                 "{}/{}".format(REMOTE, branch)], cwd=REPO_ROOT)
        if not added:
            print("WARNING: could not create worktree for existing branch {}; "
                  "skipping".format(branch), file=sys.stderr)
            return
    else:
        added, _, _ = run_soft(
            ["git", "worktree", "add", "-b", branch, worktree,
             "{}/{}".format(REMOTE, BASE_BRANCH)], cwd=REPO_ROOT)
        if not added:
            print("WARNING: could not create worktree for new branch {}; "
                  "skipping".format(branch), file=sys.stderr)
            return

    try:
        if exists:
            # ----- refresh path ----------------------------------------- #
            # Only commit when the rendered config actually differs (no churn).
            if _config_on_branch_matches(worktree, new_config):
                print("branch {} already up to date; leaving untouched"
                      .format(branch))
            else:
                _write_config(worktree, new_config)
                ok, _, _ = run_soft(["git", "add", CONFIG_NAME], cwd=worktree)
                # If somehow nothing is staged (identical after add), skip commit.
                staged, _, _ = run_soft(
                    ["git", "diff", "--staged", "--quiet"], cwd=worktree)
                # `git diff --quiet` returns 0 (ok=True) when there is NO diff.
                if staged:
                    print("branch {}: no staged diff after write; skipping commit"
                          .format(branch))
                else:
                    run_soft(["git", "commit", "-m",
                              "{}: {}".format(label, title)], cwd=worktree)
                    pushed, _, _ = run_soft(
                        ["git", "push", REMOTE, branch], cwd=worktree)
                    if not pushed:
                        print("WARNING: push failed for {}; skipping PR edit"
                              .format(branch), file=sys.stderr)
                        return
                    print("branch {}: updated config + pushed".format(branch))

            # Keep the PR body current regardless (the rationale text can change
            # even when the config bytes don't, e.g. confidence wording).
            edited, _, _ = run_soft(
                ["gh", "pr", "edit", branch, "--body", body], cwd=REPO_ROOT)
            if not edited:
                # The branch may exist without an open PR (e.g. it was closed but
                # not deleted). Try to (re)create the PR so the change resurfaces.
                _create_pr(branch, label, title, body)

        else:
            # ----- create path ------------------------------------------ #
            _write_config(worktree, new_config)
            run_soft(["git", "add", CONFIG_NAME], cwd=worktree)
            staged, _, _ = run_soft(
                ["git", "diff", "--staged", "--quiet"], cwd=worktree)
            if staged:
                # new_config identical to base -> a no-op change; don't open an
                # empty PR. (Shouldn't happen for a real change, but be safe.)
                print("branch {}: new_config matches base; nothing to PR"
                      .format(branch))
                return
            run_soft(["git", "commit", "-m", "{}: {}".format(label, title)],
                     cwd=worktree)
            pushed, _, _ = run_soft(
                ["git", "push", "-u", REMOTE, branch], cwd=worktree)
            if not pushed:
                print("WARNING: push failed for new branch {}; skipping PR create"
                      .format(branch), file=sys.stderr)
                return
            _create_pr(branch, label, title, body)
            print("branch {}: created + PR opened".format(branch))
    finally:
        # ALWAYS tidy the worktree so the runner's checkout stays clean between
        # changes, even on a mid-flow failure. --force handles a dirty worktree.
        run_soft(["git", "worktree", "remove", "--force", worktree], cwd=REPO_ROOT)


def _create_pr(branch, label, title, body):
    """
    Open a PR for an already-pushed branch. Soft: a failure (e.g. the repo
    setting that lets Actions create PRs is off) is logged with an actionable
    hint and does not abort the run. Label is applied if present.
    """
    args = [
        "gh", "pr", "create",
        "--base", BASE_BRANCH,
        "--head", branch,
        "--title", title,
        "--body", body,
    ]
    if label in LABELS:
        args += ["--label", label]
    ok, _, err = run_soft(args, cwd=REPO_ROOT)
    if not ok:
        # The single most common cause in CI is the repo/org setting being off.
        if "not allowed" in (err or "").lower() or "permission" in (err or "").lower():
            print("HINT: enable Settings -> Actions -> General -> 'Allow GitHub "
                  "Actions to create and approve pull requests' for PR creation "
                  "to work.", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Stale PR reconciliation
# --------------------------------------------------------------------------- #

def list_our_open_prs(labels):
    """
    Return a list of {"number", "headRefName"} for every open PR carrying one of
    the given labels. Soft: on failure return [] (we simply skip reconciliation
    this run rather than risk closing things blindly).

    We query per-label and union the results (a single --label query is AND, not
    OR, across labels; we want the OR set).
    """
    found = {}
    for label in labels:
        ok, out, _ = run_soft([
            "gh", "pr", "list",
            "--state", "open",
            "--label", label,
            "--limit", "200",
            "--json", "number,headRefName",
        ], cwd=REPO_ROOT)
        if not ok:
            continue
        try:
            rows = json.loads(out) if out.strip() else []
        except ValueError:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            num = row.get("number")
            head = row.get("headRefName")
            if isinstance(num, int) and isinstance(head, str):
                found[num] = head
    return [{"number": n, "headRefName": h} for n, h in found.items()]


def reconcile_stale_prs(active_branches, reconcilable_labels):
    """
    Close any open PR (carrying a RECONCILABLE label) whose branch is no longer in
    the current change-set, and delete its remote branch. `active_branches` is the
    set of branches we just created/refreshed; `reconcilable_labels` restricts
    closing to categories actually evaluated this run (so a skipped AI tick can't
    wipe suggestion/settings PRs). Each close is independent and soft.
    """
    if not reconcilable_labels:
        print("no categories evaluated this run; skipping stale-PR reconciliation")
        return
    for pr in list_our_open_prs(reconcilable_labels):
        branch = pr["headRefName"]
        # Only ever touch branches that look like ours (defensive: never close a
        # human's PR that happens to share a label).
        if not branch.startswith("recyclarr/"):
            continue
        if branch in active_branches:
            continue  # still wanted -- leave it open.

        print("reconciling away stale PR #{} (branch {})".format(
            pr["number"], branch))
        run_soft([
            "gh", "pr", "close", str(pr["number"]),
            "--comment", "Closing automatically: the upstream condition that "
                         "produced this change no longer applies (the trash_id "
                         "is back, the suggestion no longer fits, or the setting "
                         "already matches). It will reopen on its own if it "
                         "becomes relevant again.",
            "--delete-branch",
        ], cwd=REPO_ROOT)
        # `--delete-branch` removes the remote branch; if that flag was ignored
        # (older gh) fall back to an explicit delete. Soft either way.
        if remote_branch_exists(branch):
            run_soft(["git", "push", REMOTE, "--delete", branch], cwd=REPO_ROOT)


# --------------------------------------------------------------------------- #
# Feature issues
# --------------------------------------------------------------------------- #

def list_our_feature_issues():
    """
    Return {key: number} for every OPEN issue labeled `feature` that carries our
    hidden key-marker in its body. Soft: on failure return {} (we skip feature
    reconciliation this run). Issues without a parseable marker are ignored (we
    only manage the ones we created).
    """
    ok, out, _ = run_soft([
        "gh", "issue", "list",
        "--state", "open",
        "--label", "feature",
        "--limit", "200",
        "--json", "number,body",
    ], cwd=REPO_ROOT)
    if not ok:
        return {}
    try:
        rows = json.loads(out) if out.strip() else []
    except ValueError:
        rows = []

    by_key = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        num = row.get("number")
        body = row.get("body")
        if not isinstance(num, int) or not isinstance(body, str):
            continue
        match = FEATURE_KEY_RE.search(body)
        if match:
            by_key[match.group(1).strip()] = num
    return by_key


def reconcile_feature_issues(features):
    """
    Ensure one open `feature` issue per feature-record key, and close any of our
    open feature issues whose key is no longer present.

    Matching is by the hidden key-marker embedded in the body (robust to title
    edits). Each create/close is independent and soft.
    """
    existing = list_our_feature_issues()          # {key: issue_number}
    wanted_keys = {rec["key"] for rec in features}

    # ----- create the missing ones ----------------------------------------- #
    for rec in features:
        key = rec["key"]
        if key in existing:
            print("feature issue for {} already open; leaving as-is".format(key))
            continue
        # Embed the hidden marker so future runs can find this exact issue.
        body = rec["body"].rstrip() + "\n\n" + FEATURE_KEY_MARKER.format(key) + "\n"
        ok, _, _ = run_soft([
            "gh", "issue", "create",
            "--title", rec["title"],
            "--label", "feature",
            "--body", body,
        ], cwd=REPO_ROOT)
        if ok:
            print("opened feature issue for {}".format(key))

    # ----- close the ones no longer wanted --------------------------------- #
    for key, number in existing.items():
        if key in wanted_keys:
            continue
        print("closing stale feature issue #{} (key {})".format(number, key))
        run_soft([
            "gh", "issue", "close", str(number),
            "--comment", "Closing automatically: this schema property is no "
                         "longer reported as new (it has been adopted, or the "
                         "schema baseline already covers it).",
            "--reason", "completed",
        ], cwd=REPO_ROOT)


# --------------------------------------------------------------------------- #
# One-shot migration: retire the legacy status issue
# --------------------------------------------------------------------------- #

def migrate_legacy_status_issue():
    """
    Close the legacy persistent `recyclarr-status` issue once. The pipeline moved
    from a single status dashboard to per-change PRs + feature issues, so the old
    standing issue is obsolete. Idempotent: if it's already closed/absent this is
    a no-op. Soft -- a failure here never aborts the run.
    """
    ok, out, _ = run_soft([
        "gh", "issue", "list",
        "--state", "open",
        "--label", LEGACY_STATUS_LABEL,
        "--limit", "1",
        "--json", "number",
    ], cwd=REPO_ROOT)
    if not ok:
        return
    try:
        rows = json.loads(out) if out.strip() else []
    except ValueError:
        rows = []
    if not rows or not isinstance(rows[0], dict):
        return
    number = rows[0].get("number")
    if not isinstance(number, int):
        return

    print("migrating: closing legacy status issue #{}".format(number))
    run_soft([
        "gh", "issue", "close", str(number),
        "--comment", "Superseded by the new per-change flow: drift and "
                     "suggestion changes now arrive as individual pull requests "
                     "(labels `drift` / `suggestion` / `settings`), and new "
                     "schema features as `feature`-labeled issues. This standing "
                     "status issue is no longer updated.",
        "--reason", "completed",
    ], cwd=REPO_ROOT)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    # ----- catastrophic-failure-only setup ---------------------------------- #
    # These three steps MUST succeed; without them no per-item work is possible,
    # so a failure here exits non-zero (the only catastrophic exit path).
    try:
        ensure_git_identity()
        fetch_base()
    except subprocess.CalledProcessError as exc:
        print("FATAL: git setup failed: {}\n  stderr: {}".format(
            exc, (exc.stderr or "").strip()), file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print("FATAL: git not available: {}".format(exc), file=sys.stderr)
        sys.exit(1)

    # Labels are best-effort (soft inside ensure_labels); a label hiccup must not
    # be catastrophic.
    ensure_labels()

    # ----- load the unit-of-work ------------------------------------------- #
    changes, features, reconcilable_labels = collect_changes()
    active_branches = {rec["branch"] for rec in changes}

    print("")
    print("=== recyclarr PR reconciler ===")
    print("change-records:   {}".format(len(changes)))
    print("feature-records:  {}".format(len(features)))
    print("reconcilable:     {}".format(sorted(reconcilable_labels) or "(none)"))

    # ----- per-change PR create/refresh (isolated worktrees) ---------------- #
    # All worktrees live under one temp dir we remove wholesale at the end, so
    # even a worktree that failed to be removed individually can't linger.
    worktree_root = tempfile.mkdtemp(prefix="recyclarr-wt-")
    try:
        for rec in changes:
            try:
                process_change(rec, worktree_root)
            except Exception as exc:  # noqa: BLE001 -- one bad change must not abort the rest.
                print("WARNING: unhandled error processing change {}: {}".format(
                    rec.get("branch"), exc), file=sys.stderr)
    finally:
        # Best-effort prune of any leftover worktree registrations + the temp dir.
        run_soft(["git", "worktree", "prune"], cwd=REPO_ROOT)
        run_soft(["rm", "-rf", worktree_root])

    # ----- reconcile stale PRs + feature issues ----------------------------- #
    try:
        reconcile_stale_prs(active_branches, reconcilable_labels)
    except Exception as exc:  # noqa: BLE001
        print("WARNING: stale-PR reconciliation error: {}".format(exc),
              file=sys.stderr)

    try:
        reconcile_feature_issues(features)
    except Exception as exc:  # noqa: BLE001
        print("WARNING: feature-issue reconciliation error: {}".format(exc),
              file=sys.stderr)

    # ----- one-shot legacy migration ---------------------------------------- #
    try:
        migrate_legacy_status_issue()
    except Exception as exc:  # noqa: BLE001
        print("WARNING: legacy migration error: {}".format(exc), file=sys.stderr)

    print("reconciliation complete")
    # Exit 0 on normal completion even if individual items failed (all logged).
    sys.exit(0)


if __name__ == "__main__":
    main()
