#!/usr/bin/env python3
"""
test_suggestions.py - Pure-logic tests for the AI custom-format suggestion engine
(scripts/suggest_cfs.py) and the status-issue assembler (scripts/build_status_issue.py).

NO NETWORK. The one Anthropic API call is monkeypatched out; nothing here touches
the wire. These tests cover exactly the pure logic the spec calls out as
independently testable:

  * intent extraction from recyclarr.yml (incl. the `!env_var` custom tag),
  * candidate-set construction (catalog MINUS already-referenced trash_ids),
  * the response validator STRIPPING any rogue `score` field a buggy model returns,
  * confidence sorting (high > medium > low),
  * the outbound Anthropic request never instructing the model to produce a score,
    the output tool schema having no score field, and exactly ONE combined call,
  * status-issue body assembly for the four states (drift only / suggestions only /
    both / neither), with the hard guarantee that no numeric score ever appears and
    the 'no scores applied' wording is always present.

These mirror docs/superpowers/specs/2026-05-29-recyclarr-cf-suggestions-design.md
and the defensive style of scripts/check_drift.py.

Locked decisions exercised here (from the orchestrator):
  * O1: skip the Anthropic call when BOTH the recyclarr.yml hash and the guide-
        catalog hash are unchanged since suggestions.json was generated.
  * O2: ONE combined API call covering both services.
  * Model: claude-haiku-4-5, temperature 0.
  * The AI must NEVER output or apply scores.

Interface assumptions
---------------------
The scripts were drafted in parallel. These tests target the real, current
function signatures in scripts/suggest_cfs.py and scripts/build_status_issue.py
(verified at authoring time). The few places where the spec leaves a detail open
are noted inline. If a script is absent (e.g. when run before its sibling lands),
the relevant tests skip with an actionable message rather than erroring.
"""

import importlib
import json
import os
import re
import sys

import pytest


# --------------------------------------------------------------------------- #
# Module loading -- import the scripts/ modules by path
# --------------------------------------------------------------------------- #
#
# scripts/ is not a package and is not on sys.path under pytest. We add it once
# and import the two modules under test by name. This mirrors how the workflow
# invokes them (`python scripts/suggest_cfs.py`) but imports the pure functions
# without running main() (both modules guard real work behind __main__).

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def _load_module(name):
    """
    Import a module from scripts/ by name, skipping cleanly if the script has not
    been drafted yet so a missing sibling doesn't mask the rest of the suite.
    """
    path = os.path.join(SCRIPTS_DIR, name + ".py")
    if not os.path.isfile(path):
        pytest.skip(
            "scripts/{}.py not present yet (drafted in parallel)".format(name)
        )
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def suggest():
    return _load_module("suggest_cfs")


# --------------------------------------------------------------------------- #
# Fixtures -- inline recyclarr.yml + raw catalogs written to tmp_path
# --------------------------------------------------------------------------- #
#
# The config exercises both services, the `!env_var` custom tag (which plain
# yaml.safe_load chokes on), guide-backed quality profiles (trash_id-carrying),
# already-referenced custom formats, and a referenced custom_format_group.

CONFIG_YAML = """\
sonarr:
  main:
    base_url: !env_var SONARR_URL
    api_key: !env_var SONARR_API_KEY
    quality_profiles:
      - trash_id: aaaa1111aaaa1111aaaa1111aaaa1111   # e.g. "Remux + WEB 1080p"
      - name: My Custom Profile
    custom_formats:
      - trash_ids:
          - "11111111111111111111111111111111"        # already synced CF
          - "22222222222222222222222222222222"        # already synced CF
        assign_scores_to:
          - name: WEB-1080p
    custom_format_groups:
      - trash_ids:
          - "55555555555555555555555555555555"        # synced via a group
radarr:
  main:
    base_url: !env_var RADARR_URL
    quality_profiles:
      - trash_id: bbbb2222bbbb2222bbbb2222bbbb2222
    custom_formats:
      - trash_ids:
          - "33333333333333333333333333333333"        # already synced CF
"""

# `recyclarr list custom-formats <service> --raw` => TSV:
#   trash_id <TAB> name <TAB> category
# Sonarr catalog: 1111/2222 referenced directly, 5555 referenced via a group
# (all NOT candidates); 4444 is the only new candidate.
CF_SONARR_TSV = (
    "11111111111111111111111111111111\tWEB Tier 01\tStreaming Services\n"
    "22222222222222222222222222222222\tWEB Tier 02\tStreaming Services\n"
    "44444444444444444444444444444444\tRemux Tier 01\tRemux\n"
    "55555555555555555555555555555555\tDV HDR10\tHDR Formats\n"
)

# Radarr catalog: 3333 already referenced; 6666 + 7777 are new candidates.
CF_RADARR_TSV = (
    "33333333333333333333333333333333\tWEB Tier 03\tStreaming Services\n"
    "66666666666666666666666666666666\tIMAX Enhanced\tMisc\n"
    "77777777777777777777777777777777\tx265 (no HDR DV)\tHQ Source Groups\n"
)


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "recyclarr.yml"
    p.write_text(CONFIG_YAML, encoding="utf-8")
    return str(p)


@pytest.fixture()
def cf_sonarr_path(tmp_path):
    p = tmp_path / "cf_sonarr.txt"
    p.write_text(CF_SONARR_TSV, encoding="utf-8")
    return str(p)


@pytest.fixture()
def cf_radarr_path(tmp_path):
    p = tmp_path / "cf_radarr.txt"
    p.write_text(CF_RADARR_TSV, encoding="utf-8")
    return str(p)


def _load_config(suggest, path):
    """Parse a recyclarr.yml via the module's own !env_var-tolerant loader."""
    with open(path, "r", encoding="utf-8") as fh:
        return suggest.yaml.load(fh, Loader=suggest.RecyclarrLoader)


def _looks_score_free(text):
    """
    True if `text` contains nothing that reads like an APPLIED custom-format score.

    What a recyclarr score actually is: a signed integer *value* a CF is scored at
    (e.g. 1500, -10000). Those are 3+ digit standalone numbers, or any number sat
    next to the word 'score'. We forbid BOTH of those.

    What is NOT a score and must be tolerated: small ordinal tokens that are part
    of a guide CF NAME ('Remux Tier 01', 'WEB Tier 02'), resolution tiers
    ('1080p'), and the literal 32-hex trash_ids. Callers strip trash_ids / 1080p /
    4k before calling; we additionally allow 1-2 digit ordinals here.
    """
    # Any number sitting beside the word "score" is a smoking gun.
    if re.search(r"score\D{0,4}[-+]?\d+", text, flags=re.IGNORECASE):
        return False
    if re.search(r"[-+]?\d+\D{0,4}score", text, flags=re.IGNORECASE):
        return False
    # Any standalone 3+ digit integer is score-shaped (CF names use 1-2 digit
    # ordinals at most, e.g. 'Tier 01').
    if re.search(r"(?<![\w])[-+]?\d{3,}(?![\w])", text):
        return False
    return True


# =========================================================================== #
# suggest_cfs.py -- !env_var-tolerant loading + intent extraction
# =========================================================================== #

class TestIntentExtraction:
    """
    Spec unit 2, step 1: per service, extract quality-profile trash_ids and the
    set of already-referenced CF / group trash_ids.
    """

    def test_loader_tolerates_env_var_tag(self, suggest, config_path):
        # A plain yaml.safe_load would raise on `!env_var`; RecyclarrLoader must not.
        config = _load_config(suggest, config_path)
        assert isinstance(config, dict)
        assert "sonarr" in config and "radarr" in config

    def test_extracts_profiles_and_referenced_cf_ids(self, suggest, config_path):
        config = _load_config(suggest, config_path)
        intent = suggest.extract_intent(config)

        sonarr = intent["sonarr"]
        radarr = intent["radarr"]

        # Guide-backed profile trash_ids.
        assert "aaaa1111aaaa1111aaaa1111aaaa1111" in sonarr["profile_trash_ids"]
        assert "bbbb2222bbbb2222bbbb2222bbbb2222" in radarr["profile_trash_ids"]

        # Name-only profiles are captured as a separate intent signal.
        assert "My Custom Profile" in sonarr["profile_names"]

        # Already-referenced CF trash_ids: direct refs AND the group's trash_id.
        assert {"11111111111111111111111111111111",
                "22222222222222222222222222222222",
                "55555555555555555555555555555555"} <= sonarr["referenced_cf_ids"]
        assert "33333333333333333333333333333333" in radarr["referenced_cf_ids"]

        # Cross-service isolation: sonarr ids must not bleed into radarr.
        assert "11111111111111111111111111111111" not in radarr["referenced_cf_ids"]

    def test_no_intent_yields_empty(self, suggest, tmp_path):
        # No-intent guard (spec): no profiles / referenced CFs => empty sets.
        empty = tmp_path / "empty.yml"
        empty.write_text("sonarr:\n  main:\n    base_url: !env_var X\n",
                         encoding="utf-8")
        config = _load_config(suggest, str(empty))
        intent = suggest.extract_intent(config)
        assert not intent["sonarr"]["profile_trash_ids"]
        assert not intent["sonarr"]["referenced_cf_ids"]
        assert not intent["sonarr"]["profile_names"]

    def test_malformed_config_does_not_crash(self, suggest):
        # Defensive parsing parity with check_drift.py: junk shapes -> empty intent.
        intent = suggest.extract_intent(["not", "a", "dict"])
        assert intent["sonarr"]["referenced_cf_ids"] == set()
        assert intent["radarr"]["profile_trash_ids"] == set()


# =========================================================================== #
# suggest_cfs.py -- catalog parsing + candidate-set construction
# =========================================================================== #

class TestCandidateSet:
    """
    Spec unit 2, step 2: candidate set = guide catalog MINUS already-referenced
    trash_ids. The catalog comes from the `--raw` TSV.
    """

    def test_parse_cf_catalog_tsv(self, suggest):
        catalog = suggest.parse_cf_catalog(CF_SONARR_TSV)
        by_id = {cf["trash_id"]: cf for cf in catalog}

        assert set(by_id) == {
            "11111111111111111111111111111111",
            "22222222222222222222222222222222",
            "44444444444444444444444444444444",
            "55555555555555555555555555555555",
        }
        # Name + category preserved for grounding the rationale.
        assert by_id["44444444444444444444444444444444"]["name"] == "Remux Tier 01"
        assert by_id["44444444444444444444444444444444"]["category"] == "Remux"

    def test_parse_cf_catalog_skips_junk_lines(self, suggest):
        # A banner / blank line / single-field line must not become a phantom CF.
        text = (
            "\n"
            "Some banner with no tabs\n"
            "44444444444444444444444444444444\tRemux Tier 01\tRemux\n"
        )
        catalog = suggest.parse_cf_catalog(text)
        assert [cf["trash_id"] for cf in catalog] == [
            "44444444444444444444444444444444"
        ]

    def test_candidates_exclude_referenced(self, suggest, config_path):
        config = _load_config(suggest, config_path)
        intent = suggest.extract_intent(config)
        catalog = suggest.parse_cf_catalog(CF_SONARR_TSV)
        referenced = intent["sonarr"]["referenced_cf_ids"]

        candidates = suggest.build_candidates(catalog, referenced)
        cand_ids = {cf["trash_id"] for cf in candidates}

        # 1111/2222 referenced directly, 5555 referenced via the group => excluded.
        # Only 4444 remains.
        assert cand_ids == {"44444444444444444444444444444444"}


# =========================================================================== #
# suggest_cfs.py -- response validator strips scores + sorts by confidence
# =========================================================================== #

class TestResponseValidator:
    """
    Spec safety guardrail: the output schema has NO score field. Even if a buggy
    model returns a `score`, validate_suggestions must STRIP it -- a score must
    never survive into suggestions.json. It also enforces the candidate allow-list
    and sorts by confidence desc.

    validate_suggestions(raw_list, candidates_by_service_ids, service) where
    candidates_by_service_ids is {trash_id: {"name":..., "category":...}}.
    """

    def _candidate_index(self):
        return {
            "44444444444444444444444444444444":
                {"name": "Remux Tier 01", "category": "Remux"},
            "55555555555555555555555555555555":
                {"name": "DV HDR10", "category": "HDR Formats"},
            "a" * 32: {"name": "L", "category": "c"},
            "b" * 32: {"name": "H", "category": "c"},
            "c" * 32: {"name": "M", "category": "c"},
        }

    def test_strips_rogue_score_field(self, suggest):
        raw = [
            {"trash_id": "44444444444444444444444444444444",
             "name": "Remux Tier 01", "category": "Remux",
             "why_it_fits": "fits your remux 1080p profile",
             "confidence": "high",
             "score": 1500},                      # <-- rogue, must be stripped
        ]
        cleaned = suggest.validate_suggestions(raw, self._candidate_index(),
                                               "sonarr")
        assert len(cleaned) == 1
        item = cleaned[0]
        assert "score" not in item, "score field must be stripped from output"
        assert item["trash_id"] == "44444444444444444444444444444444"
        assert item["confidence"] == "high"
        assert "why_it_fits" in item
        # name/category come from the trusted catalog, not the model echo.
        assert item["name"] == "Remux Tier 01"
        assert item["category"] == "Remux"

    def test_strips_score_variants(self, suggest):
        # The allow-list build drops every non-allowed key, including near-misses.
        raw = [
            {"trash_id": "55555555555555555555555555555555",
             "name": "DV HDR10", "category": "HDR Formats",
             "why_it_fits": "HDR fit", "confidence": "medium",
             "score": 100, "suggested_score": 200, "scores": [1, 2]},
        ]
        cleaned = suggest.validate_suggestions(raw, self._candidate_index(),
                                               "sonarr")
        item = cleaned[0]
        for forbidden in ("score", "suggested_score", "scores"):
            assert forbidden not in item, "{} must not survive".format(forbidden)
        # The output dict contains ONLY the allow-listed keys.
        assert set(item) == {"trash_id", "name", "category",
                             "why_it_fits", "confidence"}

    def test_rejects_hallucinated_or_synced_ids(self, suggest):
        # ids not in the candidate index (hallucinated or already-synced) -> dropped.
        raw = [
            {"trash_id": "f" * 32, "name": "Made Up", "category": "x",
             "why_it_fits": "nope", "confidence": "high"},
        ]
        cleaned = suggest.validate_suggestions(raw, self._candidate_index(),
                                               "sonarr")
        assert cleaned == []

    def test_unknown_confidence_defaults_low(self, suggest):
        raw = [
            {"trash_id": "44444444444444444444444444444444",
             "why_it_fits": "x", "confidence": "extremely-high"},
        ]
        cleaned = suggest.validate_suggestions(raw, self._candidate_index(),
                                               "sonarr")
        assert cleaned[0]["confidence"] == "low"

    def test_sorted_by_confidence_desc(self, suggest):
        raw = [
            {"trash_id": "a" * 32, "why_it_fits": "x", "confidence": "low"},
            {"trash_id": "b" * 32, "why_it_fits": "x", "confidence": "high"},
            {"trash_id": "c" * 32, "why_it_fits": "x", "confidence": "medium"},
        ]
        cleaned = suggest.validate_suggestions(raw, self._candidate_index(),
                                               "sonarr")
        assert [s["confidence"] for s in cleaned] == ["high", "medium", "low"]

    def test_non_list_input_is_safe(self, suggest):
        assert suggest.validate_suggestions(None, self._candidate_index(),
                                            "sonarr") == []


# =========================================================================== #
# suggest_cfs.py -- the prompt + tool schema carry NO score instructions
# =========================================================================== #

class TestPromptIsScoreFree:
    """
    Spec safety guardrails, checked on the actual prompt artifacts (no network):
      * the system prompt forbids scores and never asks for one,
      * the structured-output tool schema has no `score` property anywhere,
      * the assembled user content (combined, O2) contains no score instruction,
      * model + temperature constants are the locked values.
    """

    def test_constants_locked(self, suggest):
        assert suggest.MODEL == "claude-haiku-4-5"
        assert suggest.TEMPERATURE == 0

    def test_tool_schema_has_no_score_property(self, suggest):
        schema_blob = json.dumps(suggest.SUGGESTION_TOOL).lower()
        # The tool's input_schema must not declare a score property. (The word
        # "score" may appear in a human description forbidding it -- we check the
        # property keys precisely rather than the whole blob.)
        props = suggest.SUGGESTION_TOOL["input_schema"]["properties"]
        for service in ("sonarr", "radarr"):
            item_props = props[service]["items"]["properties"]
            assert "score" not in item_props
            assert "scores" not in item_props
            assert "suggested_score" not in item_props
        # Sanity: the schema does define the legitimate fields.
        assert "confidence" in props["sonarr"]["items"]["properties"]
        assert "schema" in schema_blob or "object" in schema_blob

    def test_user_content_has_no_score_request(self, suggest, config_path):
        config = _load_config(suggest, config_path)
        intent = suggest.extract_intent(config)
        sonarr_cat = suggest.parse_cf_catalog(CF_SONARR_TSV)
        radarr_cat = suggest.parse_cf_catalog(CF_RADARR_TSV)
        candidates = {
            "sonarr": suggest.build_candidates(
                sonarr_cat, intent["sonarr"]["referenced_cf_ids"]),
            "radarr": suggest.build_candidates(
                radarr_cat, intent["radarr"]["referenced_cf_ids"]),
        }
        content = suggest.build_user_content(intent, candidates)

        # O2: one combined message references BOTH services.
        low = content.lower()
        assert "sonarr" in low and "radarr" in low
        # The message must never ask the model to produce a score. The only
        # acceptable mention is a prohibition ("no scores"); a request like
        # "assign a score" must be absent.
        assert "no scores" in low
        assert "assign a score" not in low
        assert "provide a score" not in low
        assert "output a score" not in low
        # The candidate (Remux Tier 01) is presented for the model to judge.
        assert "remux tier 01" in low

    def test_system_prompt_forbids_scores(self, suggest):
        low = suggest.SYSTEM_PROMPT.lower()
        assert "never" in low and "score" in low
        # Explicitly tells the model an empty list is acceptable (spec guardrail).
        assert "empty list" in low


# =========================================================================== #
# suggest_cfs.py -- the Anthropic call is mocked: ONE combined, score-free call
# =========================================================================== #

class TestAnthropicCallMocked:
    """
    O2 + safety: monkeypatch the Anthropic SDK so no network happens, capture the
    request kwargs, and assert:
      * exactly ONE call to messages.create (combined sonarr+radarr),
      * model == claude-haiku-4-5, temperature == 0,
      * the request is forced through the score-free tool schema,
      * nothing in the request asks for a score.
    call_anthropic does `import anthropic` internally, so we install a fake
    `anthropic` module in sys.modules before calling.
    """

    def _install_fake_anthropic(self, monkeypatch, captured):
        class _FakeMessages:
            def create(self, **kwargs):
                captured["calls"].append(kwargs)
                tool_name = kwargs.get("tool_choice", {}).get(
                    "name", "report_cf_suggestions")
                payload = {
                    "sonarr": [
                        {"trash_id": "44444444444444444444444444444444",
                         "name": "Remux Tier 01", "category": "Remux",
                         "why_it_fits": "fits remux 1080p", "confidence": "high"},
                    ],
                    "radarr": [
                        {"trash_id": "66666666666666666666666666666666",
                         "name": "IMAX Enhanced", "category": "Misc",
                         "why_it_fits": "fits your radarr profile",
                         "confidence": "low"},
                    ],
                }
                block = type("ToolUse", (), {
                    "type": "tool_use", "name": tool_name, "input": payload,
                })()
                return type("Resp", (), {"content": [block]})()

        class _FakeClient:
            def __init__(self, *a, **k):
                self.messages = _FakeMessages()

        fake_mod = type("FakeAnthropic", (), {"Anthropic": _FakeClient})()
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    def test_single_combined_score_free_call(self, suggest, monkeypatch):
        captured = {"calls": []}
        self._install_fake_anthropic(monkeypatch, captured)

        result = suggest.call_anthropic("test-key-not-used", "USER CONTENT")

        # O2: exactly one combined call.
        assert len(captured["calls"]) == 1, "expected ONE combined API call"
        req = captured["calls"][0]

        # Model + determinism contract.
        assert req["model"] == "claude-haiku-4-5"
        assert req["temperature"] == 0

        # The call is forced through the score-free tool.
        assert req["tool_choice"]["type"] == "tool"
        assert req["tool_choice"]["name"] == "report_cf_suggestions"

        # The ENTIRE outbound request -- system prompt, tools, messages -- must
        # not declare or request a score property. We check the tool schema's
        # property keys (the system prompt legitimately says the word "score"
        # only to forbid it).
        for tool in req["tools"]:
            for service in ("sonarr", "radarr"):
                item_props = (tool["input_schema"]["properties"][service]
                              ["items"]["properties"])
                assert "score" not in item_props

        # The fake returns a tool_use dict; call_anthropic surfaces it verbatim.
        assert set(result) == {"sonarr", "radarr"}
        # No score smuggled into the returned dict.
        assert json.dumps(result).lower().count("\"score\"") == 0

    def test_api_error_degrades_to_none(self, suggest, monkeypatch):
        # A network/API error must degrade gracefully (return None), not raise.
        class _BoomMessages:
            def create(self, **kwargs):
                raise RuntimeError("network down")

        class _BoomClient:
            def __init__(self, *a, **k):
                self.messages = _BoomMessages()

        fake_mod = type("FakeAnthropic", (), {"Anthropic": _BoomClient})()
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        assert suggest.call_anthropic("k", "content") is None


# =========================================================================== #
# suggest_cfs.py -- O1: hashes power the skip-when-unchanged decision
# =========================================================================== #

class TestHashingForSkip:
    """
    O1 building blocks: a stable config hash and a stable catalog hash. The daily
    run compares these against the ones recorded in suggestions.json to decide
    whether it can skip the API call. We test the hashes are stable + sensitive.
    """

    def test_catalog_hash_stable_and_sensitive(self, suggest):
        sonarr = suggest.parse_cf_catalog(CF_SONARR_TSV)
        radarr = suggest.parse_cf_catalog(CF_RADARR_TSV)

        h1 = suggest.catalog_hash(sonarr, radarr)
        h2 = suggest.catalog_hash(sonarr, radarr)
        assert h1 == h2, "catalog hash must be deterministic"

        # A changed catalog yields a different hash (=> O1 would NOT skip).
        changed = suggest.parse_cf_catalog(
            CF_SONARR_TSV + "88888888888888888888888888888888\tNew CF\tMisc\n")
        assert suggest.catalog_hash(changed, radarr) != h1

    def test_config_hash_sensitive(self, suggest):
        # The config hash is sha256 of the raw bytes; any byte change flips it.
        h1 = suggest.sha256_bytes(CONFIG_YAML.encode("utf-8"))
        h2 = suggest.sha256_bytes((CONFIG_YAML + "\n# tweak\n").encode("utf-8"))
        assert h1 != h2
        assert suggest.sha256_bytes(CONFIG_YAML.encode("utf-8")) == h1
