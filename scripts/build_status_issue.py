#!/usr/bin/env python3
"""
build_status_issue.py - Assemble the persistent recyclarr status-issue body.

Runs in GitHub Actions after check_drift.py (and, on the daily run, after
suggest_cfs.py). It is PURE FORMATTING: it reads two already-produced JSON inputs
and emits the full markdown body of the single, always-open `recyclarr-status`
issue. No network, no clock-dependent logic, never applies or invents scores.

Inputs:
  - /tmp/drift_result.json   (written by check_drift.py)
        {"has_drift": bool,
         "stale_sonarr": [...],
         "stale_radarr": [...],
         "new_schema_props": [...],
         ...}
  - suggestions.json (repo root, written by suggest_cfs.py; MAY BE ABSENT on the
    first run or before any AI run)
        {"generated_for_config_hash": "...",
         "sonarr": [{"trash_id", "name", "category", "why_it_fits", "confidence"}, ...],
         "radarr": [ ... ]}

Output:
  - /tmp/status_body.md  (the assembled markdown body, ready for `gh issue edit`)
  - the same body echoed to stdout for the workflow logs.

Body layout (exactly, per design spec section 3):

    ## Must-fix drift
    <stale trash_ids per service + new schema props, or "✅ No drift detected.">

    ## Suggestions (advisory — no scores applied)
    <ranked CF suggestions per service, each line:
        - **<name>** `<trash_id>` — <why_it_fits> _(confidence: <level>)_
     or "Add quality profiles/CFs to recyclarr.yml to get suggestions.">

    _Last drift check: <ts> · Last suggestion refresh: <ts>_

Timestamps are NOT generated here (that would make snapshot tests non-deterministic).
They are passed in via the STATUS_DRIFT_TS / STATUS_SUGGEST_TS env vars and rendered
verbatim (blank if unset).

SAFETY: the suggestion schema carries no score field, and this assembler never
emits one. Suggestions are advisory text only.

Exit code:
  - Always 0 on normal completion. Missing/partial inputs degrade gracefully
    rather than failing the run.
"""

import json
import os
import sys


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUGGESTIONS_PATH = os.path.join(REPO_ROOT, "suggestions.json")

DRIFT_RESULT_PATH = "/tmp/drift_result.json"
STATUS_BODY_PATH = "/tmp/status_body.md"

# Human-facing service labels, in display order.
SERVICES = (("sonarr", "Sonarr"), ("radarr", "Radarr"))

# Confidence ordering for a deterministic, highest-first sort. Anything we don't
# recognise sorts last (but is still shown -- the spec wants ALL suggestions).
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------- #
# Defensive input loading
# --------------------------------------------------------------------------- #

def load_json(path):
    """
    Load a JSON object from `path`. Returns {} for missing/unreadable/invalid
    files so the assembler can run from partial state without crashing.

    Always returns a dict; a JSON document whose top level is not an object
    (e.g. a bare list) is treated as empty rather than mis-keyed below.
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print("WARNING: could not read {}: {}".format(path, exc), file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _as_str_list(value):
    """Coerce a value into a list of non-empty stripped strings (defensive)."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #

def _drift_section(drift):
    """
    Build the '## Must-fix drift' section from a (possibly partial) drift dict.

    Lists stale trash_ids per service and any new schema properties. If none of
    those exist, renders the all-clear line.
    """
    stale_sonarr = _as_str_list(drift.get("stale_sonarr"))
    stale_radarr = _as_str_list(drift.get("stale_radarr"))
    new_props = _as_str_list(drift.get("new_schema_props"))

    lines = ["## Must-fix drift", ""]

    if not (stale_sonarr or stale_radarr or new_props):
        lines.append("✅ No drift detected.")
        return "\n".join(lines)

    if stale_sonarr or stale_radarr:
        lines.append("**Stale `trash_ids`** (referenced in `recyclarr.yml` but gone "
                     "upstream -- update or remove):")
        lines.append("")
        if stale_sonarr:
            lines.append("Sonarr:")
            for tid in stale_sonarr:
                lines.append("- `{}`".format(tid))
            lines.append("")
        if stale_radarr:
            lines.append("Radarr:")
            for tid in stale_radarr:
                lines.append("- `{}`".format(tid))
            lines.append("")

    if new_props:
        lines.append("**New schema properties** (possible new recyclarr features):")
        lines.append("")
        for prop in new_props:
            lines.append("- `{}`".format(prop))
        lines.append("")

    # Trim a trailing blank line for tidy output.
    return "\n".join(lines).rstrip()


def _suggestion_line(item):
    """
    Render a single suggestion as one markdown bullet. Tolerates missing fields
    (defensive: the AI step or a hand-edit could omit one). NEVER renders a score
    -- there is no score field in the schema and we don't synthesise one.

        - **<name>** `<trash_id>` — <why_it_fits> _(confidence: <level>)_
    """
    if not isinstance(item, dict):
        return None

    name = str(item.get("name") or "").strip() or "(unnamed)"
    trash_id = str(item.get("trash_id") or "").strip()
    why = str(item.get("why_it_fits") or "").strip()
    confidence = str(item.get("confidence") or "").strip() or "unknown"

    line = "- **{}**".format(name)
    if trash_id:
        line += " `{}`".format(trash_id)
    if why:
        line += " — {}".format(why)
    line += " _(confidence: {})_".format(confidence)
    return line


def _sorted_suggestions(items):
    """
    Return suggestions sorted by confidence (high -> medium -> low -> unknown).
    Stable within a confidence tier, so the AI's own ordering is preserved there.
    Non-dict entries are dropped defensively.
    """
    clean = [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []
    return sorted(
        clean,
        key=lambda it: CONFIDENCE_RANK.get(
            str(it.get("confidence") or "").strip().lower(), 99
        ),
    )


def _suggestions_section(suggestions):
    """
    Build the '## Suggestions (advisory — no scores applied)' section.

    Per service, a ranked (confidence-desc) bullet list. If suggestions.json was
    missing/empty (=> {}) or BOTH service lists are empty, render the call-to-action
    placeholder instead.
    """
    heading = "## Suggestions (advisory — no scores applied)"

    per_service = {}
    any_items = False
    for key, _label in SERVICES:
        ranked = _sorted_suggestions(suggestions.get(key))
        per_service[key] = ranked
        if ranked:
            any_items = True

    lines = [heading, ""]

    if not any_items:
        lines.append("Add quality profiles/CFs to recyclarr.yml to get suggestions.")
        return "\n".join(lines)

    for key, label in SERVICES:
        ranked = per_service[key]
        if not ranked:
            continue
        lines.append("### {}".format(label))
        lines.append("")
        for item in ranked:
            rendered = _suggestion_line(item)
            if rendered:
                lines.append(rendered)
        lines.append("")

    return "\n".join(lines).rstrip()


def _footer(drift_ts, suggest_ts):
    """
    Build the small italic footer with the two last-check timestamps. The strings
    are rendered verbatim; empty strings show as a blank slot (kept deterministic
    -- no clock access here).
    """
    return "_Last drift check: {} · Last suggestion refresh: {}_".format(
        drift_ts, suggest_ts
    )


# --------------------------------------------------------------------------- #
# Pure body assembler (snapshot-test friendly)
# --------------------------------------------------------------------------- #

def build_body(drift, suggestions, drift_ts, suggest_ts):
    """
    Assemble the full status-issue markdown body from already-parsed inputs.

    Pure function: no I/O, no network, no clock. `drift` and `suggestions` are
    dicts (use {} for missing/partial); `drift_ts` / `suggest_ts` are pre-formatted
    timestamp strings (use "" when unknown). Returns the body as a single string.
    """
    drift = drift if isinstance(drift, dict) else {}
    suggestions = suggestions if isinstance(suggestions, dict) else {}

    sections = [
        _drift_section(drift),
        _suggestions_section(suggestions),
        _footer(drift_ts, suggest_ts),
    ]
    return "\n\n".join(sections).strip() + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    drift = load_json(DRIFT_RESULT_PATH)
    suggestions = load_json(SUGGESTIONS_PATH)

    # Timestamps come from the workflow (rendered verbatim); blank if unset.
    drift_ts = os.environ.get("STATUS_DRIFT_TS", "")
    suggest_ts = os.environ.get("STATUS_SUGGEST_TS", "")

    body = build_body(drift, suggestions, drift_ts, suggest_ts)

    try:
        with open(STATUS_BODY_PATH, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        print("ERROR: could not write {}: {}".format(STATUS_BODY_PATH, exc),
              file=sys.stderr)
        sys.exit(2)

    # Echo to stdout for the workflow logs.
    print(body, end="")
    print("status body written to {}".format(STATUS_BODY_PATH), file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
