#!/usr/bin/env python3
"""
check_drift.py - Recyclarr config drift checker.

Runs in GitHub Actions (ubuntu-latest, python3 preinstalled, single pip dep: pyyaml).

Two independent checks:

  1. trash_id staleness
     - Reads upstream custom-format lists that the workflow already produced by
       running the recyclarr docker image:
           /tmp/cf_sonarr.txt   (raw stdout of: recyclarr list custom-formats sonarr --raw)
           /tmp/cf_radarr.txt   (raw stdout of: recyclarr list custom-formats radarr --raw)
     - Extracts every trash_id referenced in recyclarr.yml (per service/instance).
     - STALE = a trash_id in the config that is NOT present in the corresponding
       service's upstream set. Stale ids usually mean an upstream rename/removal.
     - If recyclarr.yml is absent, this whole check is skipped (schema check still runs).

  2. schema drift
     - Fetches the recyclarr config JSON schema tree from
       https://schemas.recyclarr.dev/latest/ (multi-file, resolves external $refs),
       enumerates all property paths into a sorted set, and diffs against the
       committed baseline schema-snapshot.json (a JSON list of path strings).
     - NEW paths = latest - baseline => possible new recyclarr features.
     - If the baseline is missing this is treated as a first run: no "new" alert,
       we just establish the snapshot.
     - The refreshed path-set is ALWAYS written back to schema-snapshot.json (on a
       successful fetch) so the workflow can commit it.

Output:
  - /tmp/drift_result.json with:
        {"has_drift": bool,
         "stale_sonarr": [...],
         "stale_radarr": [...],
         "new_schema_props": [...],
         "issue_body": "<markdown>"}
    (UNCHANGED -- the status-issue assembler still consumes exactly this shape.)
  - /tmp/drift_changes.json with:
        {"changes": [<drift change-record>, ...],
         "feature_issues": [<feature-issue record>, ...]}
    The PR manager (manage_prs.py) builds one PR per drift change-record and one
    labeled issue per feature-issue record. See the "change-record" docstring on
    build_drift_changes() for the exact field contract. Always written (even when
    both lists are empty) so the manager can reconcile a now-clean state.
  - A human-readable summary to stdout.

Exit code:
  - Always 0 on normal completion (the workflow decides what to do with the result).
  - Non-zero ONLY on genuine internal errors (the schema fetch is best-effort and
    degrades gracefully rather than failing the run).
"""

import json
import os
import re
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

# recyclarr_patch lives alongside this script in scripts/. The workflow invokes
# us as `python scripts/check_drift.py`, so scripts/ is not implicitly importable;
# we add this file's own directory to sys.path before importing the sibling. The
# import is best-effort: if the round-trip editor (or its ruamel.yaml dep) is
# unavailable, drift detection + /tmp/drift_result.json still work; we just can't
# build per-change new_config patches, so the change list degrades to empty.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import recyclarr_patch
except ImportError as exc:  # ruamel.yaml missing, or sibling not present.
    print("WARNING: recyclarr_patch unavailable ({}); drift change-records "
          "will be empty (drift_result.json unaffected)".format(exc),
          file=sys.stderr)
    recyclarr_patch = None


# --------------------------------------------------------------------------- #
# YAML loader that tolerates recyclarr's custom tags
# --------------------------------------------------------------------------- #
#
# recyclarr.yml uses the custom `!env_var VAR_NAME` tag for secrets, e.g.
#     base_url: !env_var SONARR_URL
# A plain yaml.safe_load() raises on the unknown tag and would crash the checker
# on any real config. We register a constructor that resolves the tag to a
# harmless placeholder string -- we never need the real value, we only walk the
# structure looking for trash_ids.

class RecyclarrLoader(yaml.SafeLoader):
    pass


def _env_var_constructor(loader, node):
    # Return a placeholder so the document loads; value itself is irrelevant here.
    return "<env:{}>".format(loader.construct_scalar(node))


RecyclarrLoader.add_constructor("!env_var", _env_var_constructor)


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "recyclarr.yml")
SNAPSHOT_PATH = os.path.join(REPO_ROOT, "schema-snapshot.json")

CF_SONARR_PATH = "/tmp/cf_sonarr.txt"
CF_RADARR_PATH = "/tmp/cf_radarr.txt"
RESULT_PATH = "/tmp/drift_result.json"
CHANGES_PATH = "/tmp/drift_changes.json"

SCHEMA_BASE = "https://schemas.recyclarr.dev/latest/"
SCHEMA_ROOT = "config-schema.json"

# Upstream docs we link to in change-records / feature-issues so a human can
# verify the finding. The TRaSH guide custom-format index is the source of truth
# for which trash_ids exist; the recyclarr schema docs explain new config props.
TRASH_CF_DOCS = {
    "sonarr": "https://trash-guides.info/Sonarr/sonarr-collection-of-custom-formats/",
    "radarr": "https://trash-guides.info/Radarr/radarr-collection-of-custom-formats/",
}
RECYCLARR_SCHEMA_DOCS = "https://recyclarr.dev/wiki/yaml/config-reference/"

# A trash_id is a 32-char hex hash, e.g. 496f355514737f7d83bf7aa4d24f8169.
# We anchor on word boundaries so we don't slice a longer hex token in half.
TRASH_ID_RE = re.compile(r"\b[0-9a-fA-F]{32}\b")


# --------------------------------------------------------------------------- #
# trash_id parsing from upstream `recyclarr list custom-formats` output
# --------------------------------------------------------------------------- #

def parse_trash_ids(text):
    """
    Extract the set of upstream trash_ids from the raw stdout of
    `recyclarr list custom-formats <service>` (with or without --raw).

    WHY THIS IS DEFENSIVE
    ---------------------
    The exact layout of the `list custom-formats` output is not contractually
    stable across recyclarr versions:
      * Default output is a human-readable table grouped by category (borders,
        ANSI styling, headers possible).
      * `--raw` produces TSV (Trash ID <tab> Name <tab> Category).
    Rather than parse columns positionally (fragile), we scan every line for
    tokens matching the trash_id shape (a standalone 32-char hex hash). Custom
    format *names* and category labels are human words and won't match the
    32-hex pattern, so false positives are extremely unlikely. This works for
    both the pretty table and the TSV form, and survives ANSI codes / borders.

    Returns a set of lowercased trash_id strings (empty set if text is falsy).
    """
    ids = set()
    if not text:
        return ids
    for raw_line in text.splitlines():
        # Strip ANSI escape sequences so they can't interfere with \b boundaries.
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw_line)
        for match in TRASH_ID_RE.findall(line):
            ids.add(match.lower())
    return ids


def read_upstream_ids(path):
    """Read and parse upstream trash_ids from a file; missing/unreadable -> empty set."""
    if not os.path.isfile(path):
        return set(), False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return parse_trash_ids(fh.read()), True
    except OSError as exc:
        print("WARNING: could not read {}: {}".format(path, exc), file=sys.stderr)
        return set(), False


# --------------------------------------------------------------------------- #
# trash_id extraction from recyclarr.yml
# --------------------------------------------------------------------------- #

def extract_config_trash_ids(config):
    """
    Walk a parsed recyclarr.yml and return:
        {"sonarr": set(...), "radarr": set(...)}
    of every trash_id referenced under each service.

    Expected structure:
        <service>:                # "sonarr" or "radarr"
          <instance-name>:        # arbitrary key
            custom_formats:       # list
              - trash_ids:        # list of hashes
                  - <hash>
                assign_scores_to: # optional, ignored here

    Parsed defensively: tolerate missing keys, None values, and unexpected types
    (a malformed config should not crash the checker).
    """
    out = {"sonarr": set(), "radarr": set()}
    if not isinstance(config, dict):
        return out

    for service in ("sonarr", "radarr"):
        service_block = config.get(service)
        if not isinstance(service_block, dict):
            continue
        for instance in service_block.values():
            if not isinstance(instance, dict):
                continue
            custom_formats = instance.get("custom_formats")
            if not isinstance(custom_formats, list):
                continue
            for cf_entry in custom_formats:
                if not isinstance(cf_entry, dict):
                    continue
                trash_ids = cf_entry.get("trash_ids")
                if not isinstance(trash_ids, list):
                    continue
                for tid in trash_ids:
                    if tid is None:
                        continue
                    out[service].add(str(tid).strip().lower())
    return out


# --------------------------------------------------------------------------- #
# Schema fetch + path enumeration (multi-file, resolves external $refs)
# --------------------------------------------------------------------------- #

def _http_get_json(url, timeout=30):
    req = Request(url, headers={"User-Agent": "recyclarr-drift-check"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _iter_refs(node):
    """Yield every $ref string anywhere in a JSON-schema node."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from _iter_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_refs(value)


def fetch_schema_docs(base=SCHEMA_BASE, root=SCHEMA_ROOT):
    """
    Fetch the root schema plus every transitively-referenced external file.

    The recyclarr config schema is split across ~10 files that $ref each other by
    relative path, so a single-file fetch would miss ~95% of the surface. We BFS
    the $ref graph, resolving each external ref relative to the file that contains
    it. Returns {relative_uri: parsed_doc}.
    """
    docs = {}
    queue = [root]
    while queue:
        rel = queue.pop()
        if rel in docs:
            continue
        doc = _http_get_json(base + rel)
        docs[rel] = doc
        for ref in _iter_refs(doc):
            base_part = ref.split("#", 1)[0]
            if base_part:  # external file ref (not a same-file #/... fragment)
                target = os.path.normpath(
                    os.path.join(os.path.dirname(rel), base_part)
                )
                queue.append(target)
    return docs


def _resolve_pointer(doc, fragment):
    """Resolve a JSON pointer fragment (e.g. '#/$defs/foo') within a doc."""
    node = doc
    for part in fragment.lstrip("#/").split("/"):
        if part == "":
            continue
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def enumerate_schema_paths(docs, root=SCHEMA_ROOT):
    """
    Walk the resolved multi-file schema and return a sorted list of dotted property
    paths (e.g. 'radarr.*.quality_profiles.upgrade.until_score').

    Key behaviours:
      * patternProperties (arbitrary instance names) collapse to a '*' segment so
        user-chosen instance names don't create diff noise.
      * Descends items / additionalProperties / contains and allOf / anyOf / oneOf.
      * Cross-file and same-file $refs are resolved; a (uri, fragment) visited-set
        guards against cycles (shared profile-reference.json, root self-refs).
    """
    paths = set()

    def walk(node, uri, path, seen):
        if not isinstance(node, dict):
            return

        if "$ref" in node and isinstance(node["$ref"], str):
            base_part, _, fragment = node["$ref"].partition("#")
            target = uri if not base_part else os.path.normpath(
                os.path.join(os.path.dirname(uri), base_part)
            )
            key = (target, fragment)
            if key in seen:
                return
            if target not in docs:
                return  # ref to a file we failed to fetch; skip gracefully
            sub = _resolve_pointer(docs[target], fragment) if fragment else docs[target]
            walk(sub, target, path, seen | {key})
            return

        for prop, sub in (node.get("properties") or {}).items():
            child = "{}.{}".format(path, prop) if path else prop
            paths.add(child)
            walk(sub, uri, child, seen)

        for sub in (node.get("patternProperties") or {}).values():
            child = "{}.*".format(path) if path else "*"
            paths.add(child)
            walk(sub, uri, child, seen)

        for keyword in ("items", "additionalProperties", "contains"):
            child_node = node.get(keyword)
            if isinstance(child_node, dict):
                walk(child_node, uri, path, seen)

        for keyword in ("allOf", "anyOf", "oneOf"):
            for sub in node.get(keyword, []) or []:
                walk(sub, uri, path, seen)

    walk(docs[root], root, "", frozenset())
    return sorted(paths)


def load_baseline_snapshot(path):
    """
    Load the committed baseline path-set (JSON list of strings).
    Returns (set_or_None, existed_bool). None means missing/invalid -> first run.
    """
    if not os.path.isfile(path):
        return None, False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return set(str(x) for x in data), True
    except (OSError, ValueError) as exc:
        print("WARNING: could not read baseline {}: {}".format(path, exc),
              file=sys.stderr)
    return None, False


def write_snapshot(path, paths_sorted):
    """Write the refreshed path-set so the workflow can commit it."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(paths_sorted, fh, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Markdown issue body
# --------------------------------------------------------------------------- #

def build_issue_body(stale_sonarr, stale_radarr, new_props):
    """Build a clean markdown issue body. Only include sections that have content."""
    sections = []

    if stale_sonarr or stale_radarr:
        lines = ["## Stale trash_ids",
                 "",
                 "These trash_ids are referenced in `recyclarr.yml` but are no longer "
                 "present in the upstream TRaSH Guides custom-format list. They were "
                 "likely renamed or removed upstream and should be updated or removed.",
                 ""]
        if stale_sonarr:
            lines.append("**Sonarr**")
            lines.append("")
            for tid in stale_sonarr:
                lines.append("- `{}`".format(tid))
            lines.append("")
        if stale_radarr:
            lines.append("**Radarr**")
            lines.append("")
            for tid in stale_radarr:
                lines.append("- `{}`".format(tid))
            lines.append("")
        sections.append("\n".join(lines).rstrip())

    if new_props:
        lines = ["## New schema properties",
                 "",
                 "New properties appeared in the recyclarr config schema since the last "
                 "snapshot. These usually correspond to new recyclarr features worth "
                 "reviewing for adoption.",
                 ""]
        for prop in new_props:
            lines.append("- `{}`".format(prop))
        sections.append("\n".join(lines).rstrip())

    return "\n\n".join(sections).strip()


# --------------------------------------------------------------------------- #
# Change-records (drift PRs) + feature-issues (schema-prop issues)
# --------------------------------------------------------------------------- #
#
# These feed manage_prs.py, which turns each "drift" change-record into a PR and
# each feature-issue into a labeled issue. The unit of work is ONE change apply
# per record (decision: per-change granularity), so each record carries the FULL
# recyclarr.yml text with ONLY that single change applied.

def _sanitize_branch_part(value):
    """
    Make a string safe for use in a git branch name. Lowercase, keep [a-z0-9-],
    collapse every other run of characters to a single '-', and trim leading /
    trailing '-'. trash_ids are already hex so this is mostly a no-op for them,
    but it keeps branch names well-formed for any future key shape.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower())
    return slug.strip("-")


def _read_config_text():
    """
    Read recyclarr.yml as text for the round-trip patcher. Returns the raw string,
    or None if the file is absent/unreadable (callers then emit no drift changes).
    """
    if not os.path.isfile(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        print("WARNING: could not read {} for change-records: {}"
              .format(CONFIG_PATH, exc), file=sys.stderr)
        return None


def _cf_name_from_config(config_text, trash_id):
    """
    Best-effort lookup of the human name a stale trash_id was annotated with in the
    user's config, e.g. `- deadbeef...  # BR-DISK` -> "BR-DISK". The id is gone
    upstream (that's why it's stale), so the only available name is the inline
    comment the config author wrote. Returns the comment text, or "" if none.
    """
    if not config_text or not trash_id:
        return ""
    # Match a line containing the id followed by an inline `# comment`.
    pattern = re.compile(
        r"{}\s*#\s*(.+?)\s*$".format(re.escape(trash_id)),
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(config_text)
    return m.group(1).strip() if m else ""


def _drift_change_record(config_text, service, trash_id):
    """
    Build a single "drift" change-record per the shared contract:

        {"type": "drift", "label": "drift",
         "key": <trash_id>, "service": <service>,
         "branch": "recyclarr/drift/<service>-<trash_id>",
         "title": ..., "body": <markdown>,
         "new_config": <FULL recyclarr.yml with ONLY this id removed>,
         "confidence": null, "uncertain": false}

    new_config is produced by recyclarr_patch.remove_trash_id (a no-op-safe,
    comment-preserving round-trip edit). Returns None if the patcher is
    unavailable or the edit raises -- a single un-patchable id must not abort the
    whole change list.
    """
    try:
        new_config = recyclarr_patch.remove_trash_id(config_text, service, trash_id)
    except Exception as exc:  # noqa: BLE001 -- one bad id must not sink the batch.
        print("WARNING: could not build drift patch for {} {}: {}"
              .format(service, trash_id, exc), file=sys.stderr)
        return None

    docs_url = TRASH_CF_DOCS.get(service, "https://trash-guides.info/")
    service_title = service.capitalize()
    name = _cf_name_from_config(config_text, trash_id) or "(unknown)"

    title = "drift: remove stale {} CF {}".format(service_title, name)
    body = "\n".join([
        "## 🗑️ Remove stale custom format",
        "",
        "| | |",
        "|---|---|",
        "| **Name** | {} |".format(name),
        "| **trash_id** | `{}` |".format(trash_id),
        "| **Service** | {} |".format(service_title),
        "",
        "### Why",
        "No longer present in the upstream TRaSH Guides custom-format list — "
        "renamed or removed upstream. recyclarr would warn/skip it on sync.",
        "",
        "### What this PR does",
        "Removes this one `trash_id` from `recyclarr.yml`.",
        "",
        "### Before merging",
        "If it was **renamed** (not removed) upstream, replace it with the new id "
        "from the guide instead of merging this as-is.",
        "",
        "### Source",
        "[TRaSH Guides — {} custom formats]({})".format(service_title, docs_url),
    ])

    return {
        "type": "drift",
        "label": "drift",
        "key": trash_id,
        "service": service,
        "branch": "recyclarr/drift/{}-{}".format(service, _sanitize_branch_part(trash_id)),
        "title": title,
        "body": body,
        "new_config": new_config,
        "confidence": None,
        "uncertain": False,
    }


def build_drift_changes(stale_sonarr, stale_radarr):
    """
    Build the list of "drift" change-records for every stale trash_id.

    Returns [] when recyclarr.yml is absent (no text to patch) or the patcher is
    unavailable -- drift detection (and drift_result.json) is unaffected, only the
    PR-building side degrades. Each record is a single-change patch of the CURRENT
    repo recyclarr.yml.
    """
    if recyclarr_patch is None:
        return []
    config_text = _read_config_text()
    if config_text is None:
        return []

    changes = []
    for service, stale in (("sonarr", stale_sonarr), ("radarr", stale_radarr)):
        for trash_id in stale:
            record = _drift_change_record(config_text, service, trash_id)
            if record is not None:
                changes.append(record)
    return changes


def build_feature_issues(new_props):
    """
    Build feature-issue records for new schema properties. These are NOT PRs:
    a new schema prop has no value we can safely synthesize (the AI never invents
    setting values), so the manager opens a labeled issue for human review.

    Contract: {"key": <dotted schema path>, "title": ..., "body": <markdown>}.
    """
    issues = []
    for prop in new_props:
        title = "feature: new recyclarr schema property `{}`".format(prop)
        body = (
            "A new property appeared in the recyclarr config JSON schema since the "
            "last snapshot:\n"
            "\n"
            "    {prop}\n"
            "\n"
            "New schema properties usually correspond to a new recyclarr config "
            "feature worth reviewing for adoption. No change is proposed "
            "automatically: a setting value can't be safely inferred from the "
            "schema alone, so this is filed for manual review.\n"
            "\n"
            "Schema: {schema}\n"
            "Config reference: {docs}".format(
                prop=prop,
                schema=SCHEMA_BASE + SCHEMA_ROOT,
                docs=RECYCLARR_SCHEMA_DOCS,
            )
        )
        issues.append({"key": prop, "title": title, "body": body})
    return issues


def write_changes(changes, feature_issues):
    """
    Write /tmp/drift_changes.json in the shared shape. Always called (even when
    both lists are empty) so manage_prs.py can reconcile / close superseded PRs.
    Internal write error -> exit 2 (consistent with the drift_result.json writer).
    """
    payload = {"changes": changes, "feature_issues": feature_issues}
    try:
        with open(CHANGES_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print("ERROR: could not write {}: {}".format(CHANGES_PATH, exc),
              file=sys.stderr)
        sys.exit(2)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    stale_sonarr = []
    stale_radarr = []
    new_props = []

    # ----- trash_id staleness check ----------------------------------------- #
    config_exists = os.path.isfile(CONFIG_PATH)
    if not config_exists:
        print("no config yet, skipping trash_id check")
    else:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                config = yaml.load(fh, Loader=RecyclarrLoader)
        except (OSError, yaml.YAMLError) as exc:
            print("ERROR: failed to read/parse {}: {}".format(CONFIG_PATH, exc),
                  file=sys.stderr)
            sys.exit(2)

        config_ids = extract_config_trash_ids(config)

        upstream_sonarr, sonarr_ok = read_upstream_ids(CF_SONARR_PATH)
        upstream_radarr, radarr_ok = read_upstream_ids(CF_RADARR_PATH)

        # Only flag staleness when we actually have an upstream set to compare
        # against; otherwise a missing /tmp file would mark everything stale.
        if sonarr_ok and upstream_sonarr:
            stale_sonarr = sorted(config_ids["sonarr"] - upstream_sonarr)
        elif config_ids["sonarr"]:
            print("WARNING: no upstream Sonarr custom-format list available; "
                  "skipping Sonarr staleness check", file=sys.stderr)

        if radarr_ok and upstream_radarr:
            stale_radarr = sorted(config_ids["radarr"] - upstream_radarr)
        elif config_ids["radarr"]:
            print("WARNING: no upstream Radarr custom-format list available; "
                  "skipping Radarr staleness check", file=sys.stderr)

    # ----- schema drift check ----------------------------------------------- #
    schema_fetched = False
    latest_paths = None
    try:
        docs = fetch_schema_docs()
        latest_paths = enumerate_schema_paths(docs)
        schema_fetched = True
    except (URLError, HTTPError, ValueError, KeyError) as exc:
        # Best-effort: a transient network/schema error must not fail the workflow.
        print("WARNING: schema fetch/enumerate failed: {}".format(exc),
              file=sys.stderr)

    if schema_fetched:
        baseline, baseline_existed = load_baseline_snapshot(SNAPSHOT_PATH)
        if baseline_existed and baseline is not None:
            new_props = sorted(set(latest_paths) - baseline)
        else:
            # First run: establish the baseline, do not alert on "new" props.
            print("schema baseline not found; establishing first snapshot "
                  "(no new-property alert)")
        # Refresh the snapshot (only on a successful fetch) so the workflow commits it.
        write_snapshot(SNAPSHOT_PATH, latest_paths)
    else:
        print("schema check skipped (fetch failed); snapshot left unchanged")

    # ----- assemble result -------------------------------------------------- #
    has_drift = bool(stale_sonarr or stale_radarr or new_props)
    issue_body = build_issue_body(stale_sonarr, stale_radarr, new_props)

    result = {
        "has_drift": has_drift,
        "stale_sonarr": stale_sonarr,
        "stale_radarr": stale_radarr,
        "new_schema_props": new_props,
        "issue_body": issue_body,
    }

    try:
        with open(RESULT_PATH, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print("ERROR: could not write {}: {}".format(RESULT_PATH, exc),
              file=sys.stderr)
        sys.exit(2)

    # ----- change-records + feature-issues (PR/issue manager input) --------- #
    # Per-change granularity: one drift change-record per stale trash_id (each a
    # full recyclarr.yml with ONLY that id removed) and one feature-issue per new
    # schema property. Written even when empty so the manager can reconcile a
    # now-clean state (e.g. close PRs whose drift has been resolved).
    drift_changes = build_drift_changes(stale_sonarr, stale_radarr)
    feature_issues = build_feature_issues(new_props)
    write_changes(drift_changes, feature_issues)

    # ----- human summary ---------------------------------------------------- #
    print("")
    print("=== recyclarr drift check ===")
    print("config present:         {}".format(config_exists))
    print("schema check ran:       {}".format(schema_fetched))
    print("stale sonarr trash_ids: {}".format(len(stale_sonarr)))
    print("stale radarr trash_ids: {}".format(len(stale_radarr)))
    print("new schema properties:  {}".format(len(new_props)))
    print("drift detected:         {}".format(has_drift))
    if stale_sonarr:
        print("  sonarr: " + ", ".join(stale_sonarr))
    if stale_radarr:
        print("  radarr: " + ", ".join(stale_radarr))
    if new_props:
        print("  new schema props:")
        for prop in new_props:
            print("    " + prop)
    print("drift change-records:   {}".format(len(drift_changes)))
    print("feature issues:         {}".format(len(feature_issues)))
    print("result written to {}".format(RESULT_PATH))
    print("changes written to {}".format(CHANGES_PATH))

    # Exit 0 always on normal completion; the workflow acts on drift_result.json.
    sys.exit(0)


if __name__ == "__main__":
    main()
