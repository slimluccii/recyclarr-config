#!/usr/bin/env python3
"""
recyclarr_patch.py - Round-trip recyclarr.yml editor (comment/format preserving).

This is the SHARED mutation layer the rest of the pipeline builds on. Every
change-record's `new_config` field is produced by calling exactly one of the
mutators here against the CURRENT repo recyclarr.yml text -- so a PR's diff is
always a single, surgical edit and never an incidental reformat. To make callers
trivial and keep each edit isolated, every mutator takes a STRING and returns a
STRING: it parses the YAML internally, applies one change, and re-serializes.

WHY ruamel.yaml (not pyyaml)
----------------------------
check_drift.py / suggest_cfs.py only ever READ the config, so a destructive
safe_load is fine there. Here we WRITE it back, and the config is hand-maintained
with meaningful comments (the template's `# BR-DISK` style trailing notes, the
`# yaml-language-server:` schema hint, section explanations). pyyaml would drop
every comment and rewrite the formatting, producing a noisy, unreviewable diff.
ruamel.yaml in round-trip mode preserves comments, key order, quoting style, and
blank lines, so the diff shows only the line(s) we actually changed.

THE !env_var TAG
----------------
recyclarr.yml carries secrets via the custom `!env_var VAR_NAME` tag, e.g.
    base_url: !env_var SONARR_URL
Unlike the read-only scripts (which resolve it to a throwaway placeholder), we
MUST round-trip it verbatim -- rewriting or dropping those lines would corrupt
the user's config. We register a tiny constructor/representer pair so the tagged
scalar survives a load -> dump cycle byte-for-byte.

THE SCORE PROHIBITION (hard constraint, repeated across the whole feature)
-------------------------------------------------------------------------
Scores come exclusively from the TRaSH Guides on the user's explicit opt-in.
NOTHING in this module ever writes a `score` key. add_custom_format emits
`assign_scores_to: [{name: <profile>}]` with NO score field, by construction --
there is no code path here that can produce one.

Public API (see the SHARED CONTRACTS in the task spec):
  load_config(text)  -> CommentedMap
  dump_config(data)  -> str
  remove_trash_id(text, service, trash_id) -> str
  add_custom_format(text, service, trash_id, comment_name, assign_profile) -> str
  set_setting(text, dotted_path, value) -> str
  config_sha256(text) -> str

Pure: no network, no filesystem, no clock. String in, string out.

Dependency: ruamel.yaml (installed by the workflow alongside pyyaml/anthropic).
"""

import hashlib
import io

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import ScalarString
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
except ImportError:  # pragma: no cover - surfaced clearly in CI logs.
    raise ImportError(
        "ruamel.yaml is required for recyclarr_patch.py (pip install ruamel.yaml)"
    )


# --------------------------------------------------------------------------- #
# The !env_var tag: round-trip it verbatim
# --------------------------------------------------------------------------- #
#
# recyclarr resolves `!env_var FOO` from the environment at sync time. For our
# purposes the VALUE is irrelevant, but the TEXT must survive untouched: we are
# editing a config full of these and rewriting them would break the user's setup.
#
# We model the tagged scalar as a tiny wrapper that remembers its variable name,
# register a constructor so `load` produces one, and a representer so `dump`
# re-emits the exact `!env_var NAME` form (plain scalar, no quoting). This keeps
# `base_url: !env_var SONARR_URL` byte-identical across a load -> dump cycle.

ENV_VAR_TAG = "!env_var"


class EnvVar:
    """A round-trippable stand-in for a `!env_var NAME` scalar.

    Holds only the variable NAME (the part after the tag). We never resolve it --
    this module does not need the secret value, it only needs to preserve the
    line exactly when re-serializing the document.
    """

    __slots__ = ("name",)

    def __init__(self, name):
        # Store as a plain str; the name is the scalar content after the tag.
        self.name = str(name)

    def __repr__(self):
        return "EnvVar({!r})".format(self.name)

    def __eq__(self, other):
        return isinstance(other, EnvVar) and other.name == self.name

    def __hash__(self):
        return hash((EnvVar, self.name))


def _env_var_constructor(constructor, node):
    """Build an EnvVar from a `!env_var NAME` scalar node (round-trip preserving)."""
    # construct_scalar yields the raw scalar text following the tag.
    return EnvVar(constructor.construct_scalar(node))


def _env_var_representer(representer, data):
    """Re-emit an EnvVar as the original `!env_var NAME` plain scalar."""
    return representer.represent_scalar(ENV_VAR_TAG, data.name)


def _make_yaml():
    """
    Construct a fresh round-trip YAML engine.

    A new instance per call keeps the API pure and thread-safe (ruamel's YAML
    object carries mutable state). Settings:
      * typ="rt"            -> round-trip mode: comments, order, anchors preserved.
      * preserve_quotes     -> keep the user's '/"/bare quoting choices intact.
      * mapping/sequence/offset indent -> match recyclarr.yml's 2-space block /
        4-space nested-sequence layout so any keys WE add line up with the file's
        existing style instead of reflowing it.
      * width huge          -> never auto-wrap long scalars (e.g. URLs) mid-value.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    # recyclarr.yml uses 2-space mapping indent with sequences indented 4 and the
    # dash at offset 2 (the standard recyclarr/TRaSH template layout).
    yaml.indent(mapping=2, sequence=4, offset=2)
    # Register the !env_var round-trip on THIS engine's constructor/representer.
    yaml.constructor.add_constructor(ENV_VAR_TAG, _env_var_constructor)
    yaml.representer.add_representer(EnvVar, _env_var_representer)
    return yaml


# --------------------------------------------------------------------------- #
# Core load / dump helpers
# --------------------------------------------------------------------------- #

def load_config(text):
    """
    Parse recyclarr.yml `text` into a ruamel round-trip structure (CommentedMap).

    Tolerant of the `!env_var` tag (round-tripped via the registered constructor).
    An empty / whitespace-only document loads as an empty CommentedMap so callers
    can still add keys to a blank config without special-casing None.
    """
    yaml = _make_yaml()
    data = yaml.load(text if text is not None else "")
    if data is None:
        # Blank or comment-only document: hand back an empty mapping so mutators
        # can create the service/setting scaffolding from scratch.
        return CommentedMap()
    return data


def dump_config(data):
    """
    Serialize a ruamel round-trip structure back to a YAML string, preserving the
    comments and formatting captured at load time.
    """
    yaml = _make_yaml()
    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def config_sha256(text):
    """
    sha256 (hex) of the config TEXT, computed on the UTF-8 bytes.

    Hashing the text (not the parsed object) keeps this identical to the byte-hash
    used for the O1 skip in suggest_cfs.py, so change-records and the suggestion
    engine agree on what "the same config" means.
    """
    return hashlib.sha256((text if text is not None else "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Internal navigation helpers (defensive, mirror the read scripts' tolerance)
# --------------------------------------------------------------------------- #

def _iter_instances(data, service):
    """
    Yield each instance mapping under `data[service]` (e.g. the `main:` block).

    recyclarr groups config as `<service>: { <instance-name>: {...}, ... }`.
    Returns nothing if the service block is absent or not a mapping (the same
    defensive posture as check_drift.extract_config_trash_ids).
    """
    if not isinstance(data, dict):
        return
    service_block = data.get(service)
    if not isinstance(service_block, dict):
        return
    for instance in service_block.values():
        if isinstance(instance, dict):
            yield instance


def _single_instance(data, service):
    """
    Return the SINGLE instance mapping for `service`, creating the scaffolding
    (`<service>: { main: {} }`) if the service block is missing or empty.

    add_custom_format targets "the (single) instance of that service" per the
    contract. If exactly one instance exists we use it. If none exists we create a
    conventional `main:` instance. If MULTIPLE exist we cannot guess which the
    change belongs to, so we raise -- the caller (which knows the service has one
    instance in this repo's config) should never hit this, and a loud failure is
    safer than silently editing the wrong instance.
    """
    if not isinstance(data, dict):
        raise ValueError("config root is not a mapping")

    service_block = data.get(service)
    if not isinstance(service_block, dict):
        # Create `<service>: { main: {} }` from scratch.
        service_block = CommentedMap()
        instance = CommentedMap()
        service_block["main"] = instance
        data[service] = service_block
        return instance

    instances = [v for v in service_block.values() if isinstance(v, dict)]
    if len(instances) == 1:
        return instances[0]
    if len(instances) == 0:
        instance = CommentedMap()
        service_block["main"] = instance
        return instance
    raise ValueError(
        "service '{}' has {} instances; add_custom_format needs exactly one"
        .format(service, len(instances))
    )


# --------------------------------------------------------------------------- #
# Mutator: remove a single trash_id
# --------------------------------------------------------------------------- #

def remove_trash_id(text, service, trash_id):
    """
    Remove ONE `trash_id` from `service`'s custom_formats[].trash_ids[] and return
    the updated config text. No-op-safe / idempotent if the id is absent.

    Behaviour (all defensive -- a malformed config must not crash a change build):
      * Matches case-insensitively (trash_ids are hex; the config may hold any
        case) and removes EVERY occurrence across every instance / custom_formats
        entry, so the id is fully gone.
      * Pruning, to keep the file tidy and avoid leaving invalid empty structures:
          - if removing the id empties a `trash_ids:` list, drop that entry's
            trash_ids key;
          - if that leaves the custom_formats entry with no trash_ids, drop the
            whole entry;
          - if that empties the instance's `custom_formats:` list, drop the
            custom_formats key entirely.
      * If the id is not present anywhere, the text is returned unchanged (we still
        round-trip it through load/dump so callers get a single, consistent code
        path -- ruamel preserves formatting, so an absent id yields no diff).
    """
    data = load_config(text)
    target = str(trash_id).strip().lower()

    for instance in _iter_instances(data, service):
        custom_formats = instance.get("custom_formats")
        if not isinstance(custom_formats, list):
            continue

        # Walk entries; drop the target id from each entry's trash_ids list.
        # `entries_to_drop` collects custom_formats indices that end up empty so we
        # can remove them afterwards (back-to-front, see below).
        entries_to_drop = []
        for cf_idx, cf_entry in enumerate(custom_formats):
            if not isinstance(cf_entry, dict):
                # Preserve anything we don't understand untouched.
                continue

            trash_ids = cf_entry.get("trash_ids")
            if isinstance(trash_ids, list):
                # Delete matching items BY INDEX, back-to-front. ruamel tracks each
                # item's inline comment by position in the CommentedSeq, so a
                # `del seq[i]` correctly re-indexes the survivors' comments (e.g.
                # removing id #0 keeps id #1's trailing `# name` comment). A slice
                # reassignment would orphan those comments, so we must not use it.
                match_idx = [
                    i for i, tid in enumerate(trash_ids)
                    if str(tid).strip().lower() == target
                ]
                for i in reversed(match_idx):
                    del trash_ids[i]
                # Emptied the list -> drop the trash_ids key.
                if not trash_ids:
                    del cf_entry["trash_ids"]

            # An entry with no trash_ids left is meaningless -> mark for removal.
            if "trash_ids" not in cf_entry:
                entries_to_drop.append(cf_idx)

        # Remove emptied entries back-to-front (same comment-reindex reasoning as
        # above, and so earlier indices stay valid while we delete).
        for cf_idx in reversed(entries_to_drop):
            del custom_formats[cf_idx]

        # If custom_formats is now empty, drop the key entirely.
        if isinstance(custom_formats, list) and not custom_formats:
            if "custom_formats" in instance:
                del instance["custom_formats"]

    return dump_config(data)


# --------------------------------------------------------------------------- #
# Mutator: add a custom format (NEVER a score)
# --------------------------------------------------------------------------- #

def add_custom_format(text, service, trash_id, comment_name, assign_profile):
    """
    Append a new custom_formats entry under the single instance of `service` and
    return the updated config text.

    The appended entry is exactly:
        - trash_ids:
            - <trash_id>  # <comment_name>
          assign_scores_to:
            - name: <assign_profile>

    NEVER emits a `score` key -- assign_scores_to carries ONLY the profile name.
    Scores come from the guide on the user's opt-in; this function has no code path
    that can write one.

    Details:
      * The trash_id is written lowercased (canonical hex form) with an inline
        trailing comment of `comment_name` (the human-readable CF name) so the
        diff is self-documenting -- matching the template's `# BR-DISK` style.
      * If the instance has no `custom_formats:` yet, it is created. If it already
        has entries, we append (preserving existing entries + their comments).
      * Idempotency guard: if this exact trash_id is ALREADY referenced anywhere in
        this service's custom_formats, we do nothing and return the text unchanged
        (so re-running a change can't create a duplicate entry).
    """
    data = load_config(text)
    tid = str(trash_id).strip().lower()

    # Idempotency: bail out if the id is already referenced for this service.
    for instance in _iter_instances(data, service):
        existing = instance.get("custom_formats")
        if not isinstance(existing, list):
            continue
        for cf_entry in existing:
            if not isinstance(cf_entry, dict):
                continue
            ids = cf_entry.get("trash_ids")
            if isinstance(ids, list) and any(
                str(x).strip().lower() == tid for x in ids
            ):
                # Already present -> no-op (round-trip keeps formatting/diff clean).
                return dump_config(data)

    instance = _single_instance(data, service)

    custom_formats = instance.get("custom_formats")
    if not isinstance(custom_formats, list):
        custom_formats = CommentedSeq()
        instance["custom_formats"] = custom_formats

    # Build the trash_ids list with the inline comment on the id line.
    trash_ids = CommentedSeq()
    trash_ids.append(tid)
    if comment_name:
        # Attach `# <comment_name>` to the single (index 0) list item. ruamel
        # renders this as a trailing inline comment on that line.
        trash_ids.yaml_add_eol_comment("# {}".format(comment_name), key=0)

    # Build assign_scores_to: [{name: <profile>}] -- NO score key, by construction.
    assign_entry = CommentedMap()
    assign_entry["name"] = assign_profile
    assign_scores_to = CommentedSeq()
    assign_scores_to.append(assign_entry)

    new_entry = CommentedMap()
    new_entry["trash_ids"] = trash_ids
    new_entry["assign_scores_to"] = assign_scores_to

    custom_formats.append(new_entry)

    return dump_config(data)


# --------------------------------------------------------------------------- #
# Mutator: set a scalar / mapping at a dotted path
# --------------------------------------------------------------------------- #

def set_setting(text, dotted_path, value):
    """
    Set `value` at the dotted `dotted_path` (e.g.
    'radarr.movies.media_naming.movie.standard'), creating intermediate mappings
    as needed, and return the updated config text.

    Behaviour:
      * Each path segment is a mapping key. Missing intermediate keys are created
        as empty CommentedMaps so a deep path can be set on a config that doesn't
        have the parents yet.
      * Existing siblings and their comments are preserved -- we only touch the
        final key. (Setting `radarr.movies.x` leaves `radarr.main` untouched.)
      * If an intermediate segment exists but is NOT a mapping (e.g. the user set a
        scalar where we now need to descend), we raise rather than silently
        clobbering their data -- the caller should surface that as an un-appliable
        change rather than produce a destructive edit.
      * `value` is written as-is. Callers pass plain Python scalars / dicts / lists
        (the contract says setting values come only from recyclarr config
        templates, never invented); ruamel serializes them in block style. We do
        NOT special-case scores here -- set_setting is for naming/quality settings,
        and the change-builder never routes a score through it.

    An empty / blank dotted_path is a programming error and raises ValueError.
    """
    if not dotted_path or not str(dotted_path).strip():
        raise ValueError("dotted_path must be a non-empty string")

    parts = [p for p in str(dotted_path).split(".")]
    if any(p == "" for p in parts):
        # Reject 'a..b' or leading/trailing dots -- an ambiguous path.
        raise ValueError("dotted_path has an empty segment: {!r}".format(dotted_path))

    data = load_config(text)

    # Descend/create intermediate mappings for every segment except the last.
    node = data
    for seg in parts[:-1]:
        child = node.get(seg) if isinstance(node, dict) else None
        if child is None:
            child = CommentedMap()
            node[seg] = child
        elif not isinstance(child, dict):
            raise ValueError(
                "cannot descend into non-mapping at segment '{}' of path '{}'"
                .format(seg, dotted_path)
            )
        node = child

    # Set the final key. Wrap bare Python str values so preserve_quotes doesn't
    # interfere; ruamel handles plain str fine, so we assign directly.
    final_key = parts[-1]
    node[final_key] = _to_yaml_value(value)

    return dump_config(data)


def _to_yaml_value(value):
    """
    Convert a plain Python value into something ruamel round-trips cleanly.

    Plain dicts/lists are converted to CommentedMap/CommentedSeq so they serialize
    in the document's block style (rather than flow {} / [] style). Scalars pass
    through unchanged. This keeps a `set_setting(..., {...})` mapping value looking
    like the rest of the hand-written config.
    """
    if isinstance(value, ScalarString):
        return value
    if isinstance(value, dict):
        out = CommentedMap()
        for k, v in value.items():
            out[k] = _to_yaml_value(v)
        return out
    if isinstance(value, (list, tuple)):
        out = CommentedSeq()
        for item in value:
            out.append(_to_yaml_value(item))
        return out
    return value
