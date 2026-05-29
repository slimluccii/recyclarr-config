#!/usr/bin/env python3
"""
test_patch.py - Tests for the round-trip recyclarr.yml editor
(scripts/recyclarr_patch.py) and the change-record / feature-record contract.

NO NETWORK, NO CLOCK. Every fixture is an inline YAML string; every mutator is a
pure string-in / string-out call. These tests cover exactly the SHARED CONTRACTS
the orchestrator nailed down for recyclarr_patch.py:

  * remove_trash_id removes ONLY the target id and preserves the surrounding
    comments AND the `!env_var` secret lines verbatim,
  * add_custom_format inserts the trash_id (with its inline `# <name>` comment) and
    an assign_scores_to: [{name: <profile>}] block with NO `score` key, ever,
  * set_setting sets a nested value while leaving sibling keys + comments intact,
  * load -> dump round-trips a hand-maintained config byte-for-byte (formatting,
    comments, and `!env_var` lines all survive a no-op edit),
  * the change-record JSON shape never carries a `score` key, and a feature-issue
    record carries no patch (no new_config / branch / type fields).

Style + defensive posture mirror scripts/check_drift.py and the sibling
tests/test_suggestions.py: import the script from scripts/ by path, skip cleanly
if it has not landed yet, and assert on the real current function signatures.

The hard score prohibition (scores come ONLY from the guide on opt-in) is the
single most important invariant of the whole feature, so it is checked from
several angles here -- on the patch output, on the change-record JSON, and on the
absence of any score-bearing key after a custom-format insert.
"""

import importlib
import json
import os
import re
import sys

import pytest


# --------------------------------------------------------------------------- #
# Module loading -- import scripts/recyclarr_patch.py by path
# --------------------------------------------------------------------------- #
#
# scripts/ is not a package and is not on sys.path under pytest. We add it once
# and import the module under test by name (mirrors tests/test_suggestions.py).
# If the script is absent (e.g. run before it lands), skip with an actionable
# message rather than erroring out the whole suite.

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def _load_module(name):
    """Import a scripts/ module by name, skipping cleanly if it isn't present."""
    path = os.path.join(SCRIPTS_DIR, name + ".py")
    if not os.path.isfile(path):
        pytest.skip(
            "scripts/{}.py not present yet (drafted in parallel)".format(name)
        )
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def patch():
    return _load_module("recyclarr_patch")


# --------------------------------------------------------------------------- #
# Inline fixtures -- a hand-maintained recyclarr.yml with comments + !env_var
# --------------------------------------------------------------------------- #
#
# This config deliberately exercises everything the round-trip MUST preserve:
#   * a leading `# yaml-language-server:` schema hint comment,
#   * `!env_var` secret scalars (base_url / api_key),
#   * trailing inline `# <name>` comments on individual trash_id lines (the
#     template's `# BR-DISK` style),
#   * a section comment, blank lines, and an existing assign_scores_to block,
#   * both services, so cross-service edits stay isolated.

CONFIG_YAML = """\
# yaml-language-server: $schema=https://schemas.recyclarr.dev/config-schema.json

sonarr:
  main:
    base_url: !env_var SONARR_URL
    api_key: !env_var SONARR_API_KEY
    quality_definition:
      type: series
    # Custom formats kept in sync with the guide.
    custom_formats:
      - trash_ids:
          - 11111111111111111111111111111111  # WEB Tier 01
          - 22222222222222222222222222222222  # BR-DISK
        assign_scores_to:
          - name: WEB-1080p

radarr:
  main:
    base_url: !env_var RADARR_URL
    api_key: !env_var RADARR_API_KEY
    custom_formats:
      - trash_ids:
          - 33333333333333333333333333333333  # Remux Tier 01
        assign_scores_to:
          - name: Remux
"""


# --------------------------------------------------------------------------- #
# Shared helper: assert nothing in the text reads like an APPLIED score
# --------------------------------------------------------------------------- #

def _assert_no_score_key(text):
    """
    Fail if the rendered YAML carries a `score` mapping key anywhere.

    A recyclarr score is a `score: <int>` mapping entry (or the `scores:` plural).
    We forbid both as YAML KEYS. The substring "score" inside `assign_scores_to`
    is legitimate (it's the structural key that holds profile NAMES, not scores),
    so we match on a real key shape -- `score:` / `scores:` preceded only by
    whitespace/dash -- to avoid a false positive on `assign_scores_to:`.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        # A YAML key is `<key>:` possibly preceded by a list dash.
        m = re.match(r"^-?\s*([A-Za-z_][\w-]*)\s*:", line)
        if not m:
            continue
        key = m.group(1).lower()
        assert key not in ("score", "scores", "suggested_score"), (
            "a score key leaked into the config:\n" + raw_line
        )


# =========================================================================== #
# load / dump round-trip stability
# =========================================================================== #

class TestRoundTrip:
    """
    The whole reason recyclarr_patch.py uses ruamel (not pyyaml): a load -> dump
    cycle must preserve comments, key order, quoting, blank lines, AND the
    `!env_var` tagged scalars byte-for-byte, so a per-change PR's diff is only the
    line(s) we actually changed.
    """

    def test_load_dump_is_byte_stable(self, patch):
        # A pure load -> dump round-trip must reproduce the input exactly. This is
        # the contract that guarantees a no-op edit produces an empty diff.
        out = patch.dump_config(patch.load_config(CONFIG_YAML))
        assert out == CONFIG_YAML

    def test_round_trip_preserves_env_var_lines(self, patch):
        # The !env_var secret lines must survive verbatim (rewriting them would
        # corrupt the user's config).
        out = patch.dump_config(patch.load_config(CONFIG_YAML))
        assert "base_url: !env_var SONARR_URL" in out
        assert "api_key: !env_var SONARR_API_KEY" in out
        assert "base_url: !env_var RADARR_URL" in out

    def test_round_trip_preserves_comments(self, patch):
        # Schema hint, the section comment, and the inline trash_id comments all
        # survive the round-trip.
        out = patch.dump_config(patch.load_config(CONFIG_YAML))
        assert "# yaml-language-server:" in out
        assert "# Custom formats kept in sync with the guide." in out
        assert "# BR-DISK" in out
        assert "# WEB Tier 01" in out

    def test_empty_document_loads_as_mapping(self, patch):
        # A blank/comment-only doc loads as an empty mapping so mutators can build
        # scaffolding from scratch without special-casing None.
        data = patch.load_config("")
        assert hasattr(data, "get")  # CommentedMap behaves like a dict
        assert dict(data) == {}

    def test_config_sha256_matches_byte_hash(self, patch):
        # config_sha256 hashes the TEXT bytes, identical to suggest_cfs' O1 hash so
        # both agree on what "the same config" means.
        import hashlib
        expected = hashlib.sha256(CONFIG_YAML.encode("utf-8")).hexdigest()
        assert patch.config_sha256(CONFIG_YAML) == expected
        # Sensitive: any byte change flips the hash.
        assert patch.config_sha256(CONFIG_YAML + "\n# x\n") != expected


# =========================================================================== #
# remove_trash_id
# =========================================================================== #

class TestRemoveTrashId:
    """
    remove_trash_id deletes EXACTLY the target id and nothing else, keeps the
    survivors' inline comments aligned, preserves !env_var lines, and is
    no-op-safe when the id is absent.
    """

    def test_removes_only_the_target_id(self, patch):
        out = patch.remove_trash_id(
            CONFIG_YAML, "sonarr", "11111111111111111111111111111111")
        # Target id gone.
        assert "11111111111111111111111111111111" not in out
        # Sibling id in the SAME entry survives.
        assert "22222222222222222222222222222222" in out
        # The other service is untouched.
        assert "33333333333333333333333333333333" in out

    def test_preserves_surviving_inline_comment(self, patch):
        # Removing id #0 (WEB Tier 01) must keep id #1's trailing `# BR-DISK`
        # comment correctly attached to the surviving line -- the comment-reindex
        # behaviour the mutator relies on.
        out = patch.remove_trash_id(
            CONFIG_YAML, "sonarr", "11111111111111111111111111111111")
        # The BR-DISK comment is still on the line of the id that remains.
        assert re.search(
            r"22222222222222222222222222222222\s+# BR-DISK", out
        ), "surviving id must keep its inline comment:\n" + out
        # The removed id's comment must be gone too.
        assert "# WEB Tier 01" not in out

    def test_preserves_env_var_and_other_service(self, patch):
        out = patch.remove_trash_id(
            CONFIG_YAML, "sonarr", "11111111111111111111111111111111")
        assert "base_url: !env_var SONARR_URL" in out
        assert "api_key: !env_var SONARR_API_KEY" in out
        # Radarr block intact, comment included.
        assert "# Remux Tier 01" in out

    def test_absent_id_is_noop(self, patch):
        # An id not present anywhere returns the text unchanged (round-tripped, so
        # formatting is identical -> empty diff).
        out = patch.remove_trash_id(
            CONFIG_YAML, "sonarr", "ffffffffffffffffffffffffffffffff")
        assert out == CONFIG_YAML

    def test_case_insensitive_match(self, patch):
        # trash_ids are hex; a config could hold any case. An uppercase target must
        # still match the lowercase id in the config.
        out = patch.remove_trash_id(
            CONFIG_YAML, "sonarr", "11111111111111111111111111111111".upper())
        assert "11111111111111111111111111111111" not in out

    def test_removing_last_id_prunes_empty_structures(self, patch):
        # Removing the only id in radarr's single entry should prune the now-empty
        # trash_ids list, the empty custom_formats entry, and the custom_formats
        # key -- leaving no dangling empty structures.
        out = patch.remove_trash_id(
            CONFIG_YAML, "radarr", "33333333333333333333333333333333")
        assert "33333333333333333333333333333333" not in out
        # The whole radarr custom_formats block should be gone (its only id left).
        reloaded = patch.load_config(out)
        radarr_main = reloaded["radarr"]["main"]
        assert "custom_formats" not in radarr_main
        # Sonarr is untouched.
        assert "11111111111111111111111111111111" in out

    def test_output_has_no_score_key(self, patch):
        out = patch.remove_trash_id(
            CONFIG_YAML, "sonarr", "11111111111111111111111111111111")
        _assert_no_score_key(out)


# =========================================================================== #
# add_custom_format -- inserts id + assign_scores_to, NEVER a score
# =========================================================================== #

class TestAddCustomFormat:
    """
    add_custom_format appends a custom_formats entry with the trash_id (carrying an
    inline `# <comment_name>` note) and an assign_scores_to: [{name: <profile>}]
    block -- and provably NO score key, by construction. It preserves existing
    entries + comments and is idempotent on a re-add.
    """

    def test_inserts_trash_id_with_comment(self, patch):
        out = patch.add_custom_format(
            CONFIG_YAML, "sonarr",
            "44444444444444444444444444444444", "Remux Tier 02", "WEB-1080p")
        # New id present with its inline name comment. ruamel may render a
        # hex-looking scalar quoted ('4444...'), so allow an optional trailing
        # quote between the id and its inline comment.
        assert "44444444444444444444444444444444" in out
        assert re.search(
            r"44444444444444444444444444444444'?\s+# Remux Tier 02", out
        ), "new id must carry its inline comment:\n" + out

    def test_inserts_assign_scores_to_with_profile_only(self, patch):
        out = patch.add_custom_format(
            CONFIG_YAML, "sonarr",
            "44444444444444444444444444444444", "Remux Tier 02", "WEB-1080p")
        reloaded = patch.load_config(out)
        cfs = reloaded["sonarr"]["main"]["custom_formats"]
        # Find the entry we appended.
        new_entry = next(
            cf for cf in cfs
            if any("44444444444444444444444444444444" == str(t).lower()
                   for t in cf.get("trash_ids", []))
        )
        assign = new_entry["assign_scores_to"]
        assert len(assign) == 1
        assert assign[0]["name"] == "WEB-1080p"
        # The assign_scores_to entry carries ONLY a name -- no score key.
        assert set(assign[0].keys()) == {"name"}

    def test_never_emits_a_score_key(self, patch):
        out = patch.add_custom_format(
            CONFIG_YAML, "sonarr",
            "44444444444444444444444444444444", "Remux Tier 02", "WEB-1080p")
        _assert_no_score_key(out)
        # Belt-and-suspenders: the literal `score:` key shape must be absent.
        assert not re.search(r"(^|\n)\s*-?\s*scores?\s*:", out)

    def test_preserves_existing_entries_and_comments(self, patch):
        out = patch.add_custom_format(
            CONFIG_YAML, "sonarr",
            "44444444444444444444444444444444", "Remux Tier 02", "WEB-1080p")
        # Pre-existing ids + their comments survive.
        assert "11111111111111111111111111111111" in out
        assert "# BR-DISK" in out
        assert "base_url: !env_var SONARR_URL" in out
        # Other service untouched.
        assert "33333333333333333333333333333333" in out

    def test_creates_custom_formats_when_absent(self, patch):
        # A service with an instance but no custom_formats yet must get one created.
        minimal = (
            "radarr:\n"
            "  main:\n"
            "    base_url: !env_var RADARR_URL\n"
        )
        out = patch.add_custom_format(
            minimal, "radarr",
            "66666666666666666666666666666666", "IMAX Enhanced", "HD Bluray + WEB")
        reloaded = patch.load_config(out)
        cfs = reloaded["radarr"]["main"]["custom_formats"]
        assert len(cfs) == 1
        assert "66666666666666666666666666666666" in [
            str(t).lower() for t in cfs[0]["trash_ids"]
        ]
        assert cfs[0]["assign_scores_to"][0]["name"] == "HD Bluray + WEB"
        _assert_no_score_key(out)

    def test_idempotent_on_existing_id(self, patch):
        # Re-adding an already-referenced id is a no-op (no duplicate entry); the
        # round-trip keeps formatting identical.
        out = patch.add_custom_format(
            CONFIG_YAML, "sonarr",
            "11111111111111111111111111111111", "WEB Tier 01", "WEB-1080p")
        assert out == CONFIG_YAML


# =========================================================================== #
# set_setting -- nested set, siblings + comments preserved
# =========================================================================== #

class TestSetSetting:
    """
    set_setting writes a value at a dotted path, creating intermediate mappings as
    needed, while leaving sibling keys and their comments intact. It never special-
    cases (or emits) scores.
    """

    def test_sets_nested_value_creating_intermediates(self, patch):
        out = patch.set_setting(
            CONFIG_YAML, "radarr.main.media_naming.movie.standard",
            "{Movie CleanTitle} {(Release Year)}")
        reloaded = patch.load_config(out)
        standard = reloaded["radarr"]["main"]["media_naming"]["movie"]["standard"]
        assert standard == "{Movie CleanTitle} {(Release Year)}"

    def test_preserves_sibling_keys_and_comments(self, patch):
        out = patch.set_setting(
            CONFIG_YAML, "radarr.main.media_naming.movie.standard",
            "{Movie CleanTitle}")
        # The radarr base_url sibling + sonarr block + comments are untouched.
        assert "base_url: !env_var RADARR_URL" in out
        assert "api_key: !env_var RADARR_API_KEY" in out
        assert "# BR-DISK" in out
        assert "33333333333333333333333333333333" in out
        # The sonarr side is byte-identical (we only touched radarr.main).
        assert "quality_definition:" in out

    def test_overwrites_existing_scalar(self, patch):
        # Setting a key that already exists replaces just that value.
        out = patch.set_setting(
            CONFIG_YAML, "sonarr.main.quality_definition.type", "anime")
        reloaded = patch.load_config(out)
        assert reloaded["sonarr"]["main"]["quality_definition"]["type"] == "anime"
        # The original 'series' value is gone for that key.
        assert "type: anime" in out

    def test_sets_mapping_value_in_block_style(self, patch):
        # A dict value serializes in the document's block style, not flow {} style.
        out = patch.set_setting(
            CONFIG_YAML, "radarr.main.media_naming",
            {"movie": {"standard": "{Movie CleanTitle}"}})
        assert "media_naming:" in out
        assert "movie:" in out
        # Block style => no inline braces for the value we set.
        assert "{movie:" not in out

    def test_blank_path_raises(self, patch):
        with pytest.raises(ValueError):
            patch.set_setting(CONFIG_YAML, "", "x")
        with pytest.raises(ValueError):
            patch.set_setting(CONFIG_YAML, "a..b", "x")

    def test_output_has_no_score_key(self, patch):
        out = patch.set_setting(
            CONFIG_YAML, "radarr.main.media_naming.movie.standard",
            "{Movie CleanTitle}")
        _assert_no_score_key(out)


# =========================================================================== #
# Change-record / feature-record JSON contract
# =========================================================================== #
#
# These assert the SHARED CONTRACTS the orchestrator locked, exercised against
# representative records built with the real mutators. They do not require any
# producer script (manage_prs.py is built by a sibling agent); they pin the SHAPE
# the whole pipeline agrees on:
#   * a change-record (drift / suggestion / settings) NEVER carries a `score` key,
#     and its new_config is a single-change edit of the current config that itself
#     contains no score key;
#   * a feature-issue record carries NO patch (no new_config / branch / type) --
#     it becomes a labeled issue, not a PR.

class TestChangeRecordContract:

    def _drift_record(self, patch):
        """A drift change-record built by removing a stale trash_id."""
        new_config = patch.remove_trash_id(
            CONFIG_YAML, "sonarr", "11111111111111111111111111111111")
        return {
            "type": "drift",
            "label": "drift",
            "key": "11111111111111111111111111111111",
            "service": "sonarr",
            "branch": "recyclarr/drift/11111111111111111111111111111111",
            "title": "Remove stale custom format 1111...",
            "body": "This trash_id is gone upstream. Source: TRaSH Guides.",
            "new_config": new_config,
            "confidence": None,
            "uncertain": False,
        }

    def _suggestion_record(self, patch):
        """A suggestion change-record built by adding a guide CF (best-guess profile)."""
        new_config = patch.add_custom_format(
            CONFIG_YAML, "radarr",
            "66666666666666666666666666666666", "IMAX Enhanced", "HD Bluray + WEB")
        return {
            "type": "suggestion",
            "label": "suggestion",
            "key": "66666666666666666666666666666666",
            "service": "radarr",
            "branch": "recyclarr/suggestion/66666666666666666666666666666666",
            "title": "Suggest custom format IMAX Enhanced",
            "body": ("Fits your 4K profile. Source: TRaSH Guides. "
                     "Confidence: low. Uncertain best-guess profile. "
                     "Scores come from the guide on opt-in."),
            "new_config": new_config,
            "confidence": "low",
            "uncertain": True,
        }

    def _settings_record(self, patch):
        """A settings change-record built by set_setting on a dotted path."""
        new_config = patch.set_setting(
            CONFIG_YAML, "radarr.main.media_naming.movie.standard",
            "{Movie CleanTitle}")
        return {
            "type": "settings",
            "label": "settings",
            "key": "radarr.main.media_naming.movie.standard",
            "service": "radarr",
            "branch": "recyclarr/settings/radarr-main-media_naming-movie-standard",
            "title": "Set radarr movie naming",
            "body": "Aligns naming with the recyclarr template. Source: recyclarr docs.",
            "new_config": new_config,
            "confidence": None,
            "uncertain": False,
        }

    def _feature_record(self):
        """A feature-issue record -- no patch possible, becomes an issue not a PR."""
        return {
            "key": "radarr.*.quality_profiles.upgrade.until_score",
            "title": "New schema property: until_score",
            "body": ("A new property appeared in the recyclarr schema. "
                     "See the recyclarr/schema docs."),
        }

    def _records(self, patch):
        return [
            self._drift_record(patch),
            self._suggestion_record(patch),
            self._settings_record(patch),
        ]

    def test_change_records_have_no_score_key(self, patch):
        # The change-record dict itself must never carry a 'score' key, and its
        # serialized form must not contain a "score": ... entry anywhere.
        for rec in self._records(patch):
            assert "score" not in rec
            assert "scores" not in rec
            blob = json.dumps(rec)
            assert '"score"' not in blob, "change-record JSON leaked a score key"
            assert '"scores"' not in blob

    def test_change_record_new_config_is_score_free(self, patch):
        # The new_config produced for every change-record type must itself contain
        # no score key (the patch layer can't write one).
        for rec in self._records(patch):
            _assert_no_score_key(rec["new_config"])

    def test_change_record_has_required_fields(self, patch):
        required = {
            "type", "label", "key", "service", "branch", "title", "body",
            "new_config", "confidence", "uncertain",
        }
        for rec in self._records(patch):
            assert required <= set(rec)
            assert rec["type"] in ("drift", "suggestion", "settings")
            assert rec["label"] == rec["type"]
            # Branch is the stable per-change identifier.
            assert rec["branch"].startswith("recyclarr/{}/".format(rec["type"]))
            assert isinstance(rec["uncertain"], bool)

    def test_suggestion_record_flags_uncertainty(self, patch):
        # A suggestion PR ALWAYS opens with a best-guess profile; when confidence
        # is not high it must be flagged uncertain and say so in the body.
        rec = self._suggestion_record(patch)
        assert rec["type"] == "suggestion"
        assert rec["uncertain"] is True
        assert rec["confidence"] in ("low", "medium")
        low = rec["body"].lower()
        assert "scores come from the guide" in low
        # The best-guess profile is actually present in the produced config.
        assert "HD Bluray + WEB" in rec["new_config"]

    def test_feature_record_carries_no_patch(self):
        # A feature-issue record becomes a labeled issue, not a PR: it has only
        # key/title/body and NONE of the PR-building fields.
        rec = self._feature_record()
        assert set(rec) == {"key", "title", "body"}
        for forbidden in ("new_config", "branch", "type", "label",
                          "confidence", "uncertain", "service"):
            assert forbidden not in rec, (
                "feature record must not carry the PR field {!r}".format(forbidden)
            )
        # And, like everything else, no score.
        assert '"score"' not in json.dumps(rec)
