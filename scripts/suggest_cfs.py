#!/usr/bin/env python3
"""
suggest_cfs.py - Recyclarr custom-format (CF) suggestion engine.

Runs in GitHub Actions, gated to ~once/day (see drift-check.yml). Given the
user's recyclarr.yml intent plus the upstream TRaSH guide CF catalog, it asks
Claude for a *relevance judgment* on which guide CFs fit the user's setup but
aren't yet synced -- and writes the ranked result to suggestions.json.

HARD CONSTRAINT (from the design spec, stated by the user):
  The AI must NEVER invent, output, or apply scores. Scores always come from the
  guide on opt-in. The model's only job is a fit/relevance judgment. This script
  enforces that twice: the request never asks for scores, and the response parser
  defensively strips any score-bearing field before it can reach suggestions.json.

Inputs (all produced earlier in the workflow; we reuse, never re-run):
  recyclarr.yml          parsed with a loader that tolerates the !env_var tag.
  /tmp/cf_sonarr.txt      raw `recyclarr list custom-formats sonarr --raw` (TSV).
  /tmp/cf_radarr.txt      raw `recyclarr list custom-formats radarr --raw` (TSV).
        Each TSV line: trash_id <TAB> name <TAB> category.

Processing:
  1. Extract user intent per service: the set of quality-profile trash_ids (and
     'name'-only profiles), and the set of already-referenced CF trash_ids and
     custom_format_groups trash_ids.
  2. Build the candidate set: guide CFs minus already-referenced CF ids, per svc.
  3. O1 (cost optimization): if an existing suggestions.json was generated for the
     SAME recyclarr.yml hash AND the SAME guide-catalog hash, skip the API call
     entirely and leave suggestions.json untouched.
  4. No-intent guard: no profiles AND no referenced CFs -> empty lists, recorded,
     still write suggestions.json, exit 0 (no API call).
  5. O2: one combined Anthropic API call covering BOTH services (claude-haiku-4-5,
     temperature 0, structured JSON via a tool schema with no score field).
  6. Validate + defensively strip scores + sort each service by confidence desc.

Output 1: suggestions.json (committed back by the workflow) shaped as:
  {
    "generated_for_config_hash": "<sha256 of recyclarr.yml bytes>",
    "generated_for_catalog_hash": "<sha256 of combined guide catalog>",
    "sonarr": [{"trash_id","name","category","why_it_fits","confidence"}, ...],
    "radarr": [ ... ]
  }

Output 2: /tmp/ai_changes.json -- the per-change work-units consumed by
manage_prs.py (one PR per change). It carries TWO kinds of change-record (see the
SHARED CONTRACTS in the task spec):
  {"changes": [ ...<"suggestion"> and <"settings"> change-records... ]}

  * "suggestion" (add a custom format): for each fitting candidate CF, the AI also
    picks the best assign_scores_to profile from the user's existing quality
    profiles. Per the locked decision we ALWAYS open the PR with that best guess
    and set uncertain=true (+ explain in the body) when the model isn't confident.
    new_config is produced by recyclarr_patch.add_custom_format (NO score, ever).

  * "settings" (naming/quality/profile alignment): the recommended VALUE is sourced
    ONLY from recyclarr's own config-templates repo -- never invented by the AI. In
    practice the only concrete, score-free value the templates expose
    deterministically is `quality_definition.type` (movie/series), so this script
    proposes setting `<service>.<instance>.quality_definition.type` when the user
    has clear intent for a service but no quality_definition. Anything we cannot
    ground in a fetched template value is SKIPPED with a logged reason -- we degrade
    gracefully rather than guess. new_config is produced by
    recyclarr_patch.set_setting. (See SETTINGS-TEMPLATE SOURCING note below.)

ai_changes.json is ALWAYS written (even as {"changes": []}) so manage_prs.py has a
well-formed input regardless of API/key/network outcome.

SETTINGS-TEMPLATE SOURCING (feasibility, decided after investigation)
---------------------------------------------------------------------
recyclarr's config templates live in the public github.com/recyclarr/config-templates
repo (default branch `main`), indexed by templates.json / includes.json and fetched
as raw YAML. They are fetchable read-only with no auth. BUT almost every template's
"settings" are either (a) custom-format trash_ids carrying SCORES (opt-in only --
forbidden to emit here) or (b) quality-PROFILE definitions that also carry scores.
The repo has NO media-naming templates. The ONLY broadly-applicable, concrete,
score-free value the includes expose is the quality definition TYPE
(`quality_definition: {type: movie|series}`). So settings alignment is implemented
narrowly and honestly: we source `quality_definition.type` from the matching
template include and propose it only when missing. Richer settings alignment
(media naming, format scoring) is intentionally deferred: it cannot be sourced
without emitting scores or inventing values, both of which the spec forbids.

Exit code:
  - Always 0 on normal completion AND on graceful degradation. A missing API key,
    a network/API error, or a malformed response must NOT hard-fail the daily run
    (which would break the rest of the pipeline): we print a warning, leave any
    existing suggestions.json intact, and exit 0.
  - Non-zero ONLY on a genuine internal error writing our own output file.
"""

import hashlib
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

# recyclarr_patch.py is the SHARED round-trip mutation layer. Every change-record's
# new_config is produced by exactly one of its mutators against the CURRENT repo
# recyclarr.yml text, so each PR's diff is a single surgical edit. It lives beside
# this script; import is best-effort so the suggestions.json path (which never needs
# it) still works if ruamel.yaml is unavailable -- in that case we just skip the
# ai_changes.json change-building and still write an empty changes file.
try:
    import recyclarr_patch
except ImportError as _patch_exc:  # pragma: no cover - exercised only on broken envs
    recyclarr_patch = None
    _PATCH_IMPORT_ERROR = _patch_exc
else:
    _PATCH_IMPORT_ERROR = None


# --------------------------------------------------------------------------- #
# YAML loader that tolerates recyclarr's custom tags
# --------------------------------------------------------------------------- #
#
# Same approach as check_drift.py (duplicated intentionally -- it's tiny and keeps
# this unit independently runnable). recyclarr.yml uses `!env_var VAR_NAME` for
# secrets; a plain yaml.safe_load() raises on the unknown tag. We resolve the tag
# to a harmless placeholder -- we only walk the structure, never need the value.

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
SUGGESTIONS_PATH = os.path.join(REPO_ROOT, "suggestions.json")

CF_SONARR_PATH = "/tmp/cf_sonarr.txt"
CF_RADARR_PATH = "/tmp/cf_radarr.txt"

# Per-change work-units for manage_prs.py (one PR per change). Always written.
AI_CHANGES_PATH = "/tmp/ai_changes.json"

# recyclarr's own public config-templates repo -- the ONLY authoritative source we
# allow for "settings" values (so we never invent one). Raw files are served at
# <RAW_BASE>/<path> with no auth. The quality-definition includes live under
# <service>/includes/quality-definitions/ and contain just `quality_definition:
# {type: ...}` -- a concrete, score-free value we can lift verbatim. We pin to the
# default branch; a 404 / network error simply means we skip the settings change.
CONFIG_TEMPLATES_RAW_BASE = (
    "https://raw.githubusercontent.com/recyclarr/config-templates/main/"
)
# Per service, the include file whose `quality_definition.type` we propose when the
# user has intent but no quality_definition set. These are the standard movie/series
# definitions (the anime ones target a narrower audience, so we don't auto-suggest).
QUALITY_DEFINITION_TEMPLATES = {
    "radarr": "radarr/includes/quality-definitions/radarr-quality-definition-movie.yml",
    "sonarr": "sonarr/includes/quality-definitions/sonarr-quality-definition-series.yml",
}

# claude-haiku-4-5, temperature 0 -- locked decisions for determinism + low cost.
MODEL = "claude-haiku-4-5"
TEMPERATURE = 0
MAX_TOKENS = 4096

# Valid confidence labels and their sort weight (high first).
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------- #
# Guide CF catalog parsing (from `list custom-formats <svc> --raw` TSV)
# --------------------------------------------------------------------------- #

def parse_cf_catalog(text):
    """
    Parse the raw stdout of `recyclarr list custom-formats <service> --raw` into a
    list of {trash_id, name, category} dicts.

    WHY THIS IS DEFENSIVE
    ---------------------
    The --raw form is documented as tab-separated: trash_id <TAB> name <TAB>
    category. We do NOT assume that contract holds perfectly across recyclarr
    versions, so:
      * Blank lines are skipped.
      * Lines that don't split into at least 2 tab-separated fields are skipped
        (e.g. a stray header, a banner, or a diagnostic that leaked onto stdout).
      * A missing category degrades to "" rather than dropping the row.
      * The first field must look like a non-empty token to count as an id.
    Order is preserved; duplicate trash_ids keep their first occurrence so a
    glitchy double-emit can't inflate the candidate set.

    Returns a list of dicts (empty list if text is falsy).
    """
    catalog = []
    seen = set()
    if not text:
        return catalog
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        # Need at least an id and a name; anything less is not a real CF row.
        if len(parts) < 2:
            continue
        trash_id = parts[0].strip().lower()
        name = parts[1].strip()
        category = parts[2].strip() if len(parts) >= 3 else ""
        if not trash_id or not name:
            continue
        if trash_id in seen:
            continue
        seen.add(trash_id)
        catalog.append({"trash_id": trash_id, "name": name, "category": category})
    return catalog


def read_cf_catalog(path):
    """Read + parse a guide CF catalog file; missing/unreadable -> empty list."""
    if not os.path.isfile(path):
        print("WARNING: catalog file not found: {}".format(path), file=sys.stderr)
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return parse_cf_catalog(fh.read())
    except OSError as exc:
        print("WARNING: could not read {}: {}".format(path, exc), file=sys.stderr)
        return []


# --------------------------------------------------------------------------- #
# Intent extraction from recyclarr.yml
# --------------------------------------------------------------------------- #

def extract_intent(config):
    """
    Walk a parsed recyclarr.yml and return, per service:
        {
          "sonarr": {"profile_trash_ids": set, "profile_names": set,
                     "referenced_cf_ids": set, "instance_names": list,
                     "has_quality_definition": bool},
          "radarr": {...},
        }

    Where:
      * profile_trash_ids  = guide-backed quality-profile trash_ids (these carry
                             meaning, e.g. a "Remux + WEB 1080p" profile).
      * profile_names      = 'name'-only quality profiles (user-named, no trash_id;
                             still a signal of intent even without a guide id).
      * referenced_cf_ids  = union of custom_formats[].trash_ids AND
                             custom_format_groups[].trash_ids -- everything the
                             user already syncs, so we can exclude it from
                             candidates.
      * instance_names     = ordered list of instance keys under the service (e.g.
                             ["main"]). Used to build the dotted settings path
                             `<service>.<instance>.quality_definition.type`; the
                             settings change only fires when there is exactly one.
      * has_quality_definition = True if ANY instance already has a
                             `quality_definition` key (so we don't propose one that
                             already exists).

    Expected structure (parsed defensively -- tolerate missing keys, None values,
    and unexpected types; a malformed config must not crash the engine):
        <service>:
          <instance-name>:
            quality_profiles:        # list of {trash_id|name, ...}
              - trash_id: <hash>
              - name: <profile name>
            custom_formats:          # list
              - trash_ids: [<hash>, ...]
            custom_format_groups:    # list
              - trash_ids: [<hash>, ...]
    """
    out = {
        "sonarr": {"profile_trash_ids": set(), "profile_names": set(),
                   "referenced_cf_ids": set(), "instance_names": [],
                   "has_quality_definition": False},
        "radarr": {"profile_trash_ids": set(), "profile_names": set(),
                   "referenced_cf_ids": set(), "instance_names": [],
                   "has_quality_definition": False},
    }
    if not isinstance(config, dict):
        return out

    for service in ("sonarr", "radarr"):
        service_block = config.get(service)
        if not isinstance(service_block, dict):
            continue
        bucket = out[service]

        for instance_name, instance in service_block.items():
            if not isinstance(instance, dict):
                continue

            # Record the instance key so a settings change can address it by its
            # exact dotted path; keep order (recyclarr preserves it) for determinism.
            bucket["instance_names"].append(str(instance_name))

            # Note whether a quality_definition already exists anywhere -- if so we
            # never propose one (the settings change is fill-the-gap only).
            if instance.get("quality_definition") is not None:
                bucket["has_quality_definition"] = True

            # ----- quality profiles (intent signal) ------------------------- #
            profiles = instance.get("quality_profiles")
            if isinstance(profiles, list):
                for entry in profiles:
                    if not isinstance(entry, dict):
                        continue
                    tid = entry.get("trash_id")
                    if tid is not None:
                        bucket["profile_trash_ids"].add(str(tid).strip().lower())
                    name = entry.get("name")
                    if name is not None and str(name).strip():
                        bucket["profile_names"].add(str(name).strip())

            # ----- already-referenced custom_formats ------------------------ #
            custom_formats = instance.get("custom_formats")
            if isinstance(custom_formats, list):
                for cf_entry in custom_formats:
                    if not isinstance(cf_entry, dict):
                        continue
                    trash_ids = cf_entry.get("trash_ids")
                    if not isinstance(trash_ids, list):
                        continue
                    for tid in trash_ids:
                        if tid is None:
                            continue
                        bucket["referenced_cf_ids"].add(str(tid).strip().lower())

            # ----- already-referenced custom_format_groups ------------------ #
            groups = instance.get("custom_format_groups")
            if isinstance(groups, list):
                for group_entry in groups:
                    if not isinstance(group_entry, dict):
                        continue
                    trash_ids = group_entry.get("trash_ids")
                    if not isinstance(trash_ids, list):
                        continue
                    for tid in trash_ids:
                        if tid is None:
                            continue
                        bucket["referenced_cf_ids"].add(str(tid).strip().lower())

    return out


def build_candidates(catalog, referenced_cf_ids):
    """
    Candidate set = guide CFs MINUS the CFs the user already references.

    recyclarr already auto-syncs the guide's default CFs at correct guide scores,
    so the suggestion value is concentrated in CFs the user is NOT syncing. We
    exclude anything already referenced (directly or via a group's trash_id) to
    avoid suggesting what's already in place.
    """
    return [cf for cf in catalog if cf["trash_id"] not in referenced_cf_ids]


# --------------------------------------------------------------------------- #
# Hashing (O1 skip + provenance recorded in the output)
# --------------------------------------------------------------------------- #

def sha256_bytes(data):
    """sha256 of raw bytes, hex-encoded."""
    return hashlib.sha256(data).hexdigest()


def catalog_hash(sonarr_catalog, radarr_catalog):
    """
    Stable hash of the COMBINED guide catalog (both services).

    We hash a canonical JSON form (sorted-key, no whitespace jitter) so the same
    catalog content always yields the same hash regardless of dict ordering. This
    is what O1 compares against to decide whether the guide changed since the last
    suggestions.json was generated.
    """
    payload = {
        "sonarr": [[cf["trash_id"], cf["name"], cf["category"]] for cf in sonarr_catalog],
        "radarr": [[cf["trash_id"], cf["name"], cf["category"]] for cf in radarr_catalog],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(blob)


def load_existing_suggestions(path):
    """
    Load an existing suggestions.json. Returns the parsed dict or None if it's
    missing / unreadable / not a dict. Never raises (a corrupt file just means we
    can't honour the O1 skip and will regenerate).
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        print("WARNING: could not read existing {}: {}".format(path, exc),
              file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# Anthropic prompt construction
# --------------------------------------------------------------------------- #
#
# SYSTEM PROMPT: a conservative recyclarr/TRaSH advisor. The score prohibition is
# stated unambiguously and repeated, because it is the single hardest constraint
# of the whole feature. The output tool schema (below) also omits any score field,
# so even a non-compliant model cannot smuggle a score through.

SYSTEM_PROMPT = (
    "You are a conservative advisor for recyclarr and the TRaSH Guides. The user "
    "manages Sonarr and Radarr quality via recyclarr. Your ONLY job is a relevance "
    "judgment: given the user's quality profiles and the guide's custom-format "
    "catalog, decide which custom formats (CFs) the user is NOT yet syncing would "
    "genuinely fit their setup.\n"
    "\n"
    "ABSOLUTE RULES:\n"
    "1. NEVER output, invent, suggest, or imply a score for any custom format. "
    "Scores come exclusively from the TRaSH Guides on the user's explicit opt-in. "
    "You judge FIT ONLY, never scoring. If asked or tempted to provide a score, do "
    "not.\n"
    "2. Only judge fit against the user's actual profiles and already-synced CFs. "
    "Do not assume preferences that aren't evidenced by their config.\n"
    "3. Ground every rationale in the CF's name and category from the catalog you "
    "are given. No speculation beyond what the name/category supports.\n"
    "4. Be conservative. It is completely acceptable -- and often correct -- to "
    "return an empty list for a service when nothing clearly fits.\n"
    "5. Use only the candidate CFs provided. Never invent a trash_id, name, or "
    "category, and never suggest a CF the user already syncs.\n"
    "6. For each suggested CF you MUST also pick which of the user's EXISTING "
    "quality profiles the CF should be assigned to (assign_profile). Choose from "
    "the profile names listed for that service; pick the single best fit. NEVER "
    "invent a profile name. If the user has no usable profile names for that "
    "service, or you are genuinely unsure which profile fits best, still pick your "
    "single best guess and set assign_uncertain to true so a human reviews it.\n"
    "\n"
    "For each suggested CF give: a one-sentence why_it_fits grounded in its "
    "name/category and the user's profiles; a confidence of high, medium, or low; "
    "the chosen assign_profile (an existing profile name); and assign_uncertain "
    "(true when you are not confident about the profile choice). Output via the "
    "provided tool only. Remember: no scores, ever."
)

# Tool schema: deliberately NO score field anywhere. Structured JSON output keeps
# the response machine-parseable and temperature 0 keeps it deterministic.
SUGGESTION_TOOL = {
    "name": "report_cf_suggestions",
    "description": (
        "Report the custom-format fit suggestions for each service. Scores are "
        "intentionally not part of this schema and must never be reported."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sonarr": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "trash_id": {"type": "string"},
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                        "why_it_fits": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        # Best-guess existing profile to assign the CF to, plus a
                        # flag the model raises when it isn't confident about that
                        # choice. NEITHER is a score; assign_profile is only ever an
                        # existing profile NAME (validated against the config).
                        "assign_profile": {"type": "string"},
                        "assign_uncertain": {"type": "boolean"},
                    },
                    "required": ["trash_id", "name", "why_it_fits", "confidence"],
                    "additionalProperties": False,
                },
            },
            "radarr": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "trash_id": {"type": "string"},
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                        "why_it_fits": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "assign_profile": {"type": "string"},
                        "assign_uncertain": {"type": "boolean"},
                    },
                    "required": ["trash_id", "name", "why_it_fits", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["sonarr", "radarr"],
        "additionalProperties": False,
    },
}


def _format_profiles(bucket):
    """Render a service's profile intent for the prompt; '(none)' if empty."""
    lines = []
    for tid in sorted(bucket["profile_trash_ids"]):
        lines.append("- trash_id: {}".format(tid))
    for name in sorted(bucket["profile_names"]):
        lines.append("- name: {}".format(name))
    return "\n".join(lines) if lines else "(none)"


def _format_candidates(candidates):
    """Render a candidate catalog as compact `id | name | category` lines."""
    if not candidates:
        return "(none)"
    return "\n".join(
        "{} | {} | {}".format(cf["trash_id"], cf["name"], cf["category"])
        for cf in candidates
    )


def build_user_content(intent, candidates_by_service):
    """
    Build the single combined (O2) user message: per-service profile ids (+names),
    already-synced CF ids, and the candidate CF catalog (id, name, category).
    """
    sections = []
    for service in ("sonarr", "radarr"):
        bucket = intent[service]
        synced = sorted(bucket["referenced_cf_ids"])
        # The assignable profile NAMES the model may pick for assign_profile. Only
        # 'name'-carrying profiles are assignable targets in recyclarr's
        # assign_scores_to; trash_id-only profiles have no stable name to address.
        assignable = sorted(bucket["profile_names"])
        sections.append(
            "=== {svc} ===\n"
            "Quality profiles (the user's intent):\n{profiles}\n\n"
            "Assignable profile names (pick assign_profile from EXACTLY these; "
            "never invent one):\n{assignable}\n\n"
            "Custom formats already synced (exclude these; do not re-suggest):\n"
            "{synced}\n\n"
            "Candidate custom formats (trash_id | name | category) -- judge fit "
            "against the profiles above, and for each fit pick the best "
            "assign_profile:\n{candidates}".format(
                svc=service,
                profiles=_format_profiles(bucket),
                assignable="\n".join("- {}".format(a) for a in assignable)
                if assignable else "(none)",
                synced="\n".join("- {}".format(s) for s in synced) if synced else "(none)",
                candidates=_format_candidates(candidates_by_service[service]),
            )
        )
    intro = (
        "Judge which candidate custom formats fit the user's profiles for each "
        "service. Return suggestions via the report_cf_suggestions tool. Remember: "
        "no scores, ever; an empty list is fine.\n\n"
    )
    return intro + "\n\n".join(sections)


# --------------------------------------------------------------------------- #
# Response validation (defensive score stripping + sorting)
# --------------------------------------------------------------------------- #

def validate_suggestions(raw_list, candidates_by_service_ids, service):
    """
    Turn a raw per-service suggestion list from the model into a clean, validated
    list of {trash_id, name, category, why_it_fits, confidence}.

    Defensive rules (the model is untrusted output):
      * Drop anything that isn't a dict.
      * REJECT/STRIP any score-bearing field: we build each output dict from an
        explicit allow-list of keys, so 'score', 'scores', or anything else the
        model may have added simply never reaches suggestions.json. (Belt and
        suspenders -- the tool schema already omits score, but we re-enforce here.)
      * trash_id must be a non-empty string that exists in the candidate set for
        THIS service. This blocks hallucinated ids and any attempt to re-suggest
        an already-synced CF (those aren't in candidates).
      * confidence must be one of high/medium/low; otherwise default to 'low'
        (conservative: an unparseable confidence is treated as the weakest).
      * name/category fall back to the catalog values we trust, not the model's,
        so the output stays grounded in the real guide data.
    """
    cleaned = []
    seen = set()
    if not isinstance(raw_list, list):
        return cleaned

    catalog_index = candidates_by_service_ids  # {trash_id: {name, category}}

    for item in raw_list:
        if not isinstance(item, dict):
            continue

        tid = item.get("trash_id")
        if not isinstance(tid, str):
            continue
        tid = tid.strip().lower()
        if not tid or tid not in catalog_index:
            # Hallucinated, empty, or already-synced -> reject.
            continue
        if tid in seen:
            continue
        seen.add(tid)

        confidence = item.get("confidence")
        if confidence not in CONFIDENCE_ORDER:
            confidence = "low"

        why = item.get("why_it_fits")
        why = why.strip() if isinstance(why, str) else ""

        # Trust the catalog for name/category; ignore whatever the model echoed.
        trusted = catalog_index[tid]

        # Explicit allow-list build => any score-bearing field is dropped here.
        cleaned.append({
            "trash_id": tid,
            "name": trusted["name"],
            "category": trusted["category"],
            "why_it_fits": why,
            "confidence": confidence,
        })

    # Sort by confidence desc (high > medium > low); stable within a tier so the
    # model's intra-tier ordering is preserved.
    cleaned.sort(key=lambda s: CONFIDENCE_ORDER[s["confidence"]])
    return cleaned


# --------------------------------------------------------------------------- #
# Assignment extraction (assign_profile + uncertainty for CF-add change-records)
# --------------------------------------------------------------------------- #
#
# suggestions.json deliberately stays a pure fit-judgment artifact (no profile, no
# uncertainty) -- its schema is frozen by the assembler + tests. The assign_profile
# the model picked is needed ONLY to BUILD a "suggestion" change-record, so we read
# it from the raw tool output here, keyed by trash_id, rather than threading it
# through validate_suggestions' frozen allow-list output.

def extract_assignments(raw_list, assignable_profile_names):
    """
    From the model's raw per-service list, return {trash_id: {"assign_profile",
    "assign_uncertain"}} for use when building CF-add change-records.

    Defensive + grounded:
      * assign_profile is accepted ONLY if it exactly matches one of the user's
        EXISTING profile names (the model must never invent a profile). An
        unrecognised / missing pick is recorded as None so the caller can fall back
        to the single existing profile and flag uncertainty.
      * assign_uncertain is coerced to a bool; a missing flag defaults to True
        (conservative -- absence of a clear signal means "have a human check").
      * NO score field is read or carried (there is none in the schema).
    """
    out = {}
    if not isinstance(raw_list, list):
        return out
    valid_names = set(assignable_profile_names or [])

    for item in raw_list:
        if not isinstance(item, dict):
            continue
        tid = item.get("trash_id")
        if not isinstance(tid, str):
            continue
        tid = tid.strip().lower()
        if not tid:
            continue

        picked = item.get("assign_profile")
        picked = picked.strip() if isinstance(picked, str) else ""
        # Only honour a pick that names a real existing profile.
        assign_profile = picked if picked in valid_names else None

        uncertain = item.get("assign_uncertain")
        assign_uncertain = bool(uncertain) if isinstance(uncertain, bool) else True

        out[tid] = {
            "assign_profile": assign_profile,
            "assign_uncertain": assign_uncertain,
        }
    return out


# --------------------------------------------------------------------------- #
# Settings-template sourcing (authoritative VALUES only -- never invented)
# --------------------------------------------------------------------------- #
#
# See the SETTINGS-TEMPLATE SOURCING note in the module docstring for the full
# feasibility rationale. The short version: the only concrete, score-free value the
# recyclarr config-templates repo exposes deterministically is the quality
# definition TYPE. We fetch the matching include, parse out
# `quality_definition.type`, and that is the ONLY value we will ever propose via a
# settings change. If the fetch/parse fails we return None and the caller skips the
# change with a logged reason (graceful degradation -- we never guess a value).

def _http_get_text(url, timeout=30):
    """GET a URL and return its decoded text body. Raises on network/HTTP error."""
    req = Request(url, headers={"User-Agent": "recyclarr-suggest-cfs"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_quality_definition_type(service):
    """
    Fetch the recyclarr config-templates quality-definition include for `service`
    and return its `quality_definition.type` value (e.g. "movie" / "series"), or
    None if the template can't be fetched/parsed.

    This is the ONLY settings value we source. We parse with the same !env_var-
    tolerant loader used everywhere here (the template won't contain the tag, but
    reusing the loader keeps parsing uniform and can't choke on one if it appears).
    Any error -- unknown service, network failure, malformed YAML, missing key --
    degrades to None so the caller skips the settings change rather than inventing.
    """
    rel = QUALITY_DEFINITION_TEMPLATES.get(service)
    if not rel:
        return None
    url = CONFIG_TEMPLATES_RAW_BASE + rel
    try:
        text = _http_get_text(url)
    except (URLError, HTTPError, OSError) as exc:
        print("WARNING: could not fetch quality-definition template for {} ({}): {}"
              .format(service, url, exc), file=sys.stderr)
        return None
    try:
        doc = yaml.load(text, Loader=RecyclarrLoader)
    except yaml.YAMLError as exc:
        print("WARNING: could not parse quality-definition template for {}: {}"
              .format(service, exc), file=sys.stderr)
        return None

    qd = doc.get("quality_definition") if isinstance(doc, dict) else None
    qtype = qd.get("type") if isinstance(qd, dict) else None
    if not isinstance(qtype, str) or not qtype.strip():
        print("WARNING: quality-definition template for {} had no usable type; "
              "skipping settings change".format(service), file=sys.stderr)
        return None
    return qtype.strip()


# --------------------------------------------------------------------------- #
# Change-record construction (the per-change work-units for manage_prs.py)
# --------------------------------------------------------------------------- #
#
# Each change-record is a self-contained unit a single PR is built from. new_config
# is ALWAYS the CURRENT repo recyclarr.yml text with EXACTLY this one change applied
# via a recyclarr_patch mutator -- so every PR diff is a single surgical edit and no
# record can ever carry a score (the mutators have no code path that writes one).

def _sanitize_branch_segment(text):
    """
    Make a dotted path / id safe for a git branch name: lowercase, and replace any
    run of non [a-z0-9._-] characters with a single '-'. Keeps dots/underscores
    (they're valid in refs and keep the path readable, e.g.
    radarr.main.quality_definition.type).
    """
    seg = str(text).strip().lower()
    seg = re.sub(r"[^a-z0-9._-]+", "-", seg)
    return seg.strip("-") or "change"


def build_suggestion_change(service, suggestion, assignment, config_text,
                            assignable_profile_names):
    """
    Build a "suggestion" change-record (add a custom format) for one validated
    suggestion, or None if it can't be built (no patch layer, or no profile to
    assign to at all).

    assign_profile selection (locked decision: ALWAYS open with a best guess):
      * Prefer the model's pick (already validated against existing names in
        extract_assignments).
      * Else fall back to the single existing assignable profile name if there is
        exactly one obvious choice.
      * If neither yields a profile, we cannot produce a valid assign_scores_to, so
        we skip this CF (a change with no real profile would be un-appliable).
    uncertain is set true whenever the model flagged uncertainty OR we had to fall
    back to a default pick -- and the body explains why, per the spec.
    """
    if recyclarr_patch is None:
        return None

    trash_id = suggestion["trash_id"]
    name = suggestion["name"]
    confidence = suggestion["confidence"]
    why = suggestion.get("why_it_fits", "")

    model_pick = assignment.get("assign_profile") if assignment else None
    model_uncertain = assignment.get("assign_uncertain", True) if assignment else True

    names = list(assignable_profile_names or [])
    fell_back = False
    if model_pick:
        assign_profile = model_pick
    elif len(names) == 1:
        # Exactly one existing profile -> unambiguous fallback target.
        assign_profile = names[0]
        fell_back = True
    else:
        # No model pick and no single obvious profile: we can't assign safely.
        print("INFO: skipping suggestion change for {} {}: no assignable profile "
              "(model pick unusable, {} candidate profiles)"
              .format(service, trash_id, len(names)), file=sys.stderr)
        return None

    # Uncertain when the model said so OR we defaulted the profile ourselves.
    uncertain = bool(model_uncertain or fell_back or not model_pick)

    try:
        new_config = recyclarr_patch.add_custom_format(
            config_text, service, trash_id, name, assign_profile)
    except Exception as exc:  # noqa: BLE001 -- a bad mutate must skip, not crash run.
        print("WARNING: could not build new_config for suggestion {} {}: {}"
              .format(service, trash_id, exc), file=sys.stderr)
        return None

    source = (
        "https://github.com/recyclarr/config-templates "
        "(scores come from the TRaSH guide on opt-in)"
    )
    body_lines = [
        "**Add custom format** `{}` (`{}`) to **{}**.".format(name, trash_id, service),
        "",
        "**Why it fits:** {}".format(why or "(fits the user's profiles)"),
        "",
        "**Assign to profile:** `{}`".format(assign_profile),
        "",
        "**Confidence:** {}".format(confidence),
    ]
    if uncertain:
        reason = ("the model flagged the profile choice as uncertain"
                  if model_uncertain and not fell_back
                  else "no confident profile pick was available, so the only "
                       "existing profile was used as a best guess")
        body_lines += [
            "",
            "> ⚠️ **Uncertain:** {}. Please confirm the `assign_scores_to` "
            "profile before merging.".format(reason),
        ]
    body_lines += [
        "",
        "_Note: no score is set here. Scores come from the TRaSH guide on opt-in._",
        "",
        "Source: {}".format(source),
    ]

    return {
        "type": "suggestion",
        "label": "suggestion",
        "key": trash_id,
        "service": service,
        "branch": "recyclarr/suggestion/{}-{}".format(service, trash_id),
        "title": "Add custom format: {} ({})".format(name, service),
        "body": "\n".join(body_lines),
        "new_config": new_config,
        "confidence": confidence,
        "uncertain": uncertain,
    }


def build_settings_change(service, intent_bucket, config_text):
    """
    Build a "settings" change-record proposing `quality_definition.type` for a
    service that has clear intent but no quality_definition yet -- with the VALUE
    sourced from recyclarr's config-templates (never invented). Returns the record,
    or None (with a logged reason) when it shouldn't / can't be built.

    Guards (degrade gracefully -- skip + log, never guess):
      * Skip if a quality_definition already exists anywhere for the service.
      * Skip unless there is EXACTLY one instance, so the dotted path
        `<service>.<instance>.quality_definition.type` is unambiguous.
      * Skip if the template value can't be fetched/parsed.
      * Skip if the patch layer is unavailable.
    """
    if recyclarr_patch is None:
        return None
    if intent_bucket.get("has_quality_definition"):
        return None

    instances = intent_bucket.get("instance_names") or []
    if len(instances) != 1:
        print("INFO: skipping settings change for {}: expected exactly one instance, "
              "found {}".format(service, len(instances)), file=sys.stderr)
        return None
    instance = instances[0]

    qtype = fetch_quality_definition_type(service)
    if qtype is None:
        # fetch_quality_definition_type already logged the specific reason.
        print("INFO: skipping settings change for {}: no template-sourced "
              "quality_definition.type available".format(service), file=sys.stderr)
        return None

    dotted_path = "{}.{}.quality_definition.type".format(service, instance)
    try:
        new_config = recyclarr_patch.set_setting(config_text, dotted_path, qtype)
    except Exception as exc:  # noqa: BLE001 -- a bad mutate must skip, not crash run.
        print("WARNING: could not build new_config for settings change {}: {}"
              .format(dotted_path, exc), file=sys.stderr)
        return None

    rel = QUALITY_DEFINITION_TEMPLATES[service]
    source = CONFIG_TEMPLATES_RAW_BASE + rel
    body = "\n".join([
        "**Align quality definition** for **{}**.".format(service),
        "",
        "Set `{}` to `{}`.".format(dotted_path, qtype),
        "",
        "This value is taken verbatim from recyclarr's own config template "
        "(`{}`), not invented. recyclarr uses the quality definition to size each "
        "quality tier; aligning it matches the upstream-recommended baseline for "
        "this service.".format(rel),
        "",
        "_Note: no score is set here -- this only sets the quality definition type. "
        "Scores come from the TRaSH guide on opt-in._",
        "",
        "Source: {}".format(source),
    ])

    return {
        "type": "settings",
        "label": "settings",
        "key": dotted_path,
        "service": service,
        "branch": "recyclarr/settings/{}".format(_sanitize_branch_segment(dotted_path)),
        "title": "Set {} = {}".format(dotted_path, qtype),
        "body": body,
        "new_config": new_config,
        "confidence": None,
        "uncertain": False,
    }


def assemble_changes(config_text, intent, suggestions_by_service,
                     assignments_by_service):
    """
    Build the full ordered list of change-records (suggestion + settings) from a set
    of validated suggestions and the assignment hints.

    Used by BOTH the happy path (fresh suggestions + fresh assignments from the API)
    and the O1-skip path (suggestions reloaded from the committed suggestions.json,
    with empty assignments so the fallback profile logic applies). This keeps
    ai_changes.json STABLE across non-refresh ticks: the same config + suggestions
    deterministically yield the same change-records, so manage_prs.py won't churn
    PRs when nothing actually changed.

    `assignments_by_service` may be {} / partial; missing entries fall back to the
    single-existing-profile heuristic inside build_suggestion_change.
    """
    changes = []
    if recyclarr_patch is None:
        return changes

    for service in ("sonarr", "radarr"):
        bucket = intent.get(service, {})
        assignable = sorted(bucket.get("profile_names") or [])
        assignments = (assignments_by_service or {}).get(service, {})

        # 1) suggestion (add custom format) change-records.
        for suggestion in suggestions_by_service.get(service, []) or []:
            tid = suggestion.get("trash_id")
            assignment = assignments.get(tid) if isinstance(assignments, dict) else None
            change = build_suggestion_change(
                service, suggestion, assignment, config_text, assignable)
            if change is not None:
                changes.append(change)

        # 2) settings (quality_definition.type alignment) change-record.
        settings_change = build_settings_change(service, bucket, config_text)
        if settings_change is not None:
            changes.append(settings_change)

    return changes


def _rebuild_changes_from_committed(config_text, intent):
    """
    Rebuild + write ai_changes.json from the COMMITTED suggestions.json (no API
    call), for the degraded paths (O1 skip, missing key, API failure). Assignments
    aren't persisted in suggestions.json, so we pass none and rely on the
    single-existing-profile fallback (uncertainty flagged). Keeps the change-set
    stable so manage_prs.py doesn't churn PRs when no fresh judgment was produced.
    """
    existing = load_existing_suggestions(SUGGESTIONS_PATH) or {}
    existing_suggestions = {
        "sonarr": existing.get("sonarr") if isinstance(existing.get("sonarr"), list) else [],
        "radarr": existing.get("radarr") if isinstance(existing.get("radarr"), list) else [],
    }
    changes = assemble_changes(config_text, intent, existing_suggestions, {})
    write_ai_changes(changes)
    print("rebuilt {} change-record(s) from committed suggestions.json"
          .format(len(changes)))


def write_ai_changes(changes):
    """
    Write /tmp/ai_changes.json = {"changes": [...]} for manage_prs.py. ALWAYS called
    (even with an empty list) so the reconciler has a well-formed input regardless
    of how the run degraded. An I/O failure here is a genuine internal error -> exit 2.
    """
    try:
        with open(AI_CHANGES_PATH, "w", encoding="utf-8") as fh:
            json.dump({"changes": changes}, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print("ERROR: could not write {}: {}".format(AI_CHANGES_PATH, exc),
              file=sys.stderr)
        sys.exit(2)


# --------------------------------------------------------------------------- #
# Anthropic API call (O2: one combined call)
# --------------------------------------------------------------------------- #

def call_anthropic(api_key, user_content):
    """
    Make the single combined Anthropic call and return the parsed tool input dict
    ({"sonarr": [...], "radarr": [...]}), or None on any failure.

    Failures are swallowed and reported as warnings: the design requires that a
    network/API error never hard-fails the daily run (it must not break the rest
    of the pipeline). The caller leaves any existing suggestions.json intact.
    """
    try:
        import anthropic
    except ImportError:
        print("WARNING: anthropic SDK not installed; skipping suggestion refresh",
              file=sys.stderr)
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=SYSTEM_PROMPT,
            tools=[SUGGESTION_TOOL],
            # Force the model to emit via our score-free tool schema.
            tool_choice={"type": "tool", "name": SUGGESTION_TOOL["name"]},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:  # noqa: BLE001 -- any API/network error must degrade, not crash.
        print("WARNING: Anthropic API call failed: {}".format(exc), file=sys.stderr)
        return None

    # Pull the tool_use block matching our tool; ignore any stray text blocks.
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and \
                getattr(block, "name", None) == SUGGESTION_TOOL["name"]:
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict):
                return tool_input
            print("WARNING: tool_use input was not a dict; ignoring response",
                  file=sys.stderr)
            return None

    print("WARNING: model returned no usable tool_use block; ignoring response",
          file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def write_suggestions(config_hash, cat_hash, sonarr, radarr):
    """Write suggestions.json in the exact spec shape. Internal-error -> exit 2."""
    result = {
        "generated_for_config_hash": config_hash,
        "generated_for_catalog_hash": cat_hash,
        "sonarr": sonarr,
        "radarr": radarr,
    }
    try:
        with open(SUGGESTIONS_PATH, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print("ERROR: could not write {}: {}".format(SUGGESTIONS_PATH, exc),
              file=sys.stderr)
        sys.exit(2)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    # ----- always-write ai_changes.json guarantee --------------------------- #
    # The contract requires /tmp/ai_changes.json to exist after every run, even an
    # early-exit one. Establish an empty, well-formed file up front; the happy path
    # overwrites it with the real change-records at the end. (write_ai_changes is
    # idempotent and cheap.) If a patch-layer import failed, log it once -- the
    # suggestions.json path still works, but no change-records can be built.
    write_ai_changes([])
    if recyclarr_patch is None:
        print("WARNING: recyclarr_patch unavailable ({}); ai_changes.json will stay "
              "empty (no PR change-records this run)".format(_PATCH_IMPORT_ERROR),
              file=sys.stderr)

    # ----- load + hash recyclarr.yml ---------------------------------------- #
    if not os.path.isfile(CONFIG_PATH):
        # No config => nothing to infer intent from. Write empty suggestions so
        # the assembler has a well-formed file, and exit cleanly. ai_changes.json
        # was already written empty above.
        print("no recyclarr.yml; writing empty suggestions (no intent to infer)")
        write_suggestions(sha256_bytes(b""), catalog_hash([], []), [], [])
        sys.exit(0)

    try:
        with open(CONFIG_PATH, "rb") as fh:
            config_bytes = fh.read()
    except OSError as exc:
        print("WARNING: could not read {}: {}; leaving suggestions.json intact"
              .format(CONFIG_PATH, exc), file=sys.stderr)
        sys.exit(0)

    config_hash = sha256_bytes(config_bytes)

    # Decode the raw bytes once for the patch mutators (they take TEXT). errors are
    # replaced so an odd byte can't crash us; the config is normally clean UTF-8.
    config_text = config_bytes.decode("utf-8", errors="replace")

    try:
        config = yaml.load(config_bytes, Loader=RecyclarrLoader)
    except yaml.YAMLError as exc:
        print("WARNING: failed to parse {}: {}; leaving suggestions.json intact"
              .format(CONFIG_PATH, exc), file=sys.stderr)
        sys.exit(0)

    # ----- intent + catalogs + candidates ----------------------------------- #
    intent = extract_intent(config)

    sonarr_catalog = read_cf_catalog(CF_SONARR_PATH)
    radarr_catalog = read_cf_catalog(CF_RADARR_PATH)
    cat_hash = catalog_hash(sonarr_catalog, radarr_catalog)

    candidates = {
        "sonarr": build_candidates(sonarr_catalog, intent["sonarr"]["referenced_cf_ids"]),
        "radarr": build_candidates(radarr_catalog, intent["radarr"]["referenced_cf_ids"]),
    }

    # ----- no-intent guard -------------------------------------------------- #
    # If the user references no profiles and no CFs at all, there is nothing to
    # infer. Record empty lists, still write suggestions.json, exit 0 (no call).
    has_intent = any(
        intent[svc]["profile_trash_ids"] or intent[svc]["profile_names"]
        or intent[svc]["referenced_cf_ids"]
        for svc in ("sonarr", "radarr")
    )
    if not has_intent:
        print("no quality profiles or referenced CFs in recyclarr.yml: "
              "no intent to infer; writing empty suggestions")
        write_suggestions(config_hash, cat_hash, [], [])
        sys.exit(0)

    # ----- O1: skip the API call when nothing changed ----------------------- #
    # If a prior suggestions.json was generated for the SAME config hash AND the
    # SAME catalog hash, the inputs haven't changed -- skip the (paid) API call and
    # leave the existing file exactly as-is.
    existing = load_existing_suggestions(SUGGESTIONS_PATH)
    if existing is not None and \
            existing.get("generated_for_config_hash") == config_hash and \
            existing.get("generated_for_catalog_hash") == cat_hash:
        print("recyclarr.yml and guide catalog unchanged since last generation; "
              "skipping Anthropic API call (suggestions.json left unchanged)")
        # O1 skips the (paid) call, but ai_changes.json must still reflect the
        # CURRENT desired change-set so manage_prs.py doesn't churn PRs on every
        # non-refresh tick. Rebuild deterministically from the committed
        # suggestions (no API call). Deterministic in => deterministic out.
        _rebuild_changes_from_committed(config_text, intent)
        sys.exit(0)

    # ----- API key guard ---------------------------------------------------- #
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set; skipping suggestion refresh "
              "(suggestions.json left unchanged)", file=sys.stderr)
        # No API call possible, but rebuild ai_changes.json from whatever
        # suggestions are already committed (+ settings, which need no API) so the
        # change-set stays stable rather than being wiped to empty.
        _rebuild_changes_from_committed(config_text, intent)
        sys.exit(0)

    # ----- O2: one combined Anthropic call ---------------------------------- #
    user_content = build_user_content(intent, candidates)
    tool_input = call_anthropic(api_key, user_content)
    if tool_input is None:
        # Any API/network/SDK failure: do not hard-fail the pipeline. Leave any
        # existing suggestions.json intact and rebuild ai_changes.json from it (so
        # we don't churn PRs just because one daily call failed).
        print("suggestion refresh skipped due to API issue; "
              "suggestions.json left unchanged")
        _rebuild_changes_from_committed(config_text, intent)
        sys.exit(0)

    # ----- validate + defensively strip scores + sort ----------------------- #
    # Build per-service id->catalog indexes so validation can trust catalog values
    # for name/category and reject any non-candidate (hallucinated/synced) id.
    candidate_index = {
        svc: {cf["trash_id"]: cf for cf in candidates[svc]}
        for svc in ("sonarr", "radarr")
    }
    sonarr_out = validate_suggestions(tool_input.get("sonarr"),
                                      candidate_index["sonarr"], "sonarr")
    radarr_out = validate_suggestions(tool_input.get("radarr"),
                                      candidate_index["radarr"], "radarr")

    write_suggestions(config_hash, cat_hash, sonarr_out, radarr_out)

    # ----- build the per-change work-units (ai_changes.json) ----------------- #
    # The model's assign_profile + uncertainty picks live OUTSIDE suggestions.json
    # (its schema is frozen), so we read them straight from the raw tool output,
    # validated against the user's existing profile names. Then assemble suggestion
    # + settings change-records and overwrite the empty ai_changes.json from earlier.
    assignments = {
        "sonarr": extract_assignments(tool_input.get("sonarr"),
                                      sorted(intent["sonarr"]["profile_names"])),
        "radarr": extract_assignments(tool_input.get("radarr"),
                                      sorted(intent["radarr"]["profile_names"])),
    }
    suggestions_by_service = {"sonarr": sonarr_out, "radarr": radarr_out}
    changes = assemble_changes(config_text, intent, suggestions_by_service, assignments)
    write_ai_changes(changes)

    # ----- human summary ---------------------------------------------------- #
    settings_changes = sum(1 for c in changes if c["type"] == "settings")
    suggestion_changes = sum(1 for c in changes if c["type"] == "suggestion")
    print("")
    print("=== recyclarr CF suggestions ===")
    print("config hash:           {}".format(config_hash[:12]))
    print("catalog hash:          {}".format(cat_hash[:12]))
    print("sonarr candidates:     {}".format(len(candidates["sonarr"])))
    print("radarr candidates:     {}".format(len(candidates["radarr"])))
    print("sonarr suggestions:    {}".format(len(sonarr_out)))
    print("radarr suggestions:    {}".format(len(radarr_out)))
    print("suggestion changes:    {}".format(suggestion_changes))
    print("settings changes:      {}".format(settings_changes))
    print("written to {}".format(SUGGESTIONS_PATH))
    print("changes written to {}".format(AI_CHANGES_PATH))

    sys.exit(0)


if __name__ == "__main__":
    main()
