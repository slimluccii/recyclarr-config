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

Output: suggestions.json (committed back by the workflow) shaped as:
  {
    "generated_for_config_hash": "<sha256 of recyclarr.yml bytes>",
    "generated_for_catalog_hash": "<sha256 of combined guide catalog>",
    "sonarr": [{"trash_id","name","category","why_it_fits","confidence"}, ...],
    "radarr": [ ... ]
  }

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
import sys

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


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
                     "referenced_cf_ids": set},
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
                   "referenced_cf_ids": set()},
        "radarr": {"profile_trash_ids": set(), "profile_names": set(),
                   "referenced_cf_ids": set()},
    }
    if not isinstance(config, dict):
        return out

    for service in ("sonarr", "radarr"):
        service_block = config.get(service)
        if not isinstance(service_block, dict):
            continue
        bucket = out[service]

        for instance in service_block.values():
            if not isinstance(instance, dict):
                continue

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
    "\n"
    "For each suggested CF give a one-sentence why_it_fits grounded in its "
    "name/category and the user's profiles, and a confidence of high, medium, or "
    "low. Output via the provided tool only."
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
        sections.append(
            "=== {svc} ===\n"
            "Quality profiles (the user's intent):\n{profiles}\n\n"
            "Custom formats already synced (exclude these; do not re-suggest):\n"
            "{synced}\n\n"
            "Candidate custom formats (trash_id | name | category) -- judge fit "
            "against the profiles above:\n{candidates}".format(
                svc=service,
                profiles=_format_profiles(bucket),
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
    # ----- load + hash recyclarr.yml ---------------------------------------- #
    if not os.path.isfile(CONFIG_PATH):
        # No config => nothing to infer intent from. Write empty suggestions so
        # the assembler has a well-formed file, and exit cleanly.
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
        sys.exit(0)

    # ----- API key guard ---------------------------------------------------- #
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set; skipping suggestion refresh "
              "(suggestions.json left unchanged)", file=sys.stderr)
        sys.exit(0)

    # ----- O2: one combined Anthropic call ---------------------------------- #
    user_content = build_user_content(intent, candidates)
    tool_input = call_anthropic(api_key, user_content)
    if tool_input is None:
        # Any API/network/SDK failure: do not hard-fail the pipeline. Leave any
        # existing suggestions.json intact and exit 0.
        print("suggestion refresh skipped due to API issue; "
              "suggestions.json left unchanged")
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

    # ----- human summary ---------------------------------------------------- #
    print("")
    print("=== recyclarr CF suggestions ===")
    print("config hash:           {}".format(config_hash[:12]))
    print("catalog hash:          {}".format(cat_hash[:12]))
    print("sonarr candidates:     {}".format(len(candidates["sonarr"])))
    print("radarr candidates:     {}".format(len(candidates["radarr"])))
    print("sonarr suggestions:    {}".format(len(sonarr_out)))
    print("radarr suggestions:    {}".format(len(radarr_out)))
    print("written to {}".format(SUGGESTIONS_PATH))

    sys.exit(0)


if __name__ == "__main__":
    main()
