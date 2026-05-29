"""
Tests for manage_prs.collect_changes() -- specifically the reconcilable_labels
gating that prevents a skipped AI tick from closing all suggestion/settings PRs.

Pure logic only: collect_changes() reads the two /tmp JSON files (module-level
path constants) and returns (changes, features, reconcilable_labels). We point
those constants at tmp_path files and assert the label set. No git/gh/network.
"""

import importlib.util
import json
import os

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _load_manage_prs():
    spec = importlib.util.spec_from_file_location(
        "manage_prs", os.path.join(SCRIPTS, "manage_prs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _drift_change(branch="recyclarr/drift/sonarr-abc"):
    return {"type": "drift", "label": "drift", "key": "abc", "service": "sonarr",
            "branch": branch, "title": "remove abc", "body": "b",
            "new_config": "sonarr: {}\n", "confidence": None, "uncertain": False}


def _suggestion_change(branch="recyclarr/suggestion/sonarr-def"):
    return {"type": "suggestion", "label": "suggestion", "key": "def", "service": "sonarr",
            "branch": branch, "title": "add def", "body": "b",
            "new_config": "sonarr: {}\n", "confidence": "high", "uncertain": False}


def _point(mod, tmp_path):
    mod.DRIFT_CHANGES_PATH = str(tmp_path / "drift_changes.json")
    mod.AI_CHANGES_PATH = str(tmp_path / "ai_changes.json")


def test_ai_skipped_is_not_reconcilable(tmp_path):
    """evaluated:false (gated skip) -> suggestion/settings NOT reconcilable."""
    mod = _load_manage_prs()
    _point(mod, tmp_path)
    _write(mod.DRIFT_CHANGES_PATH, {"changes": [_drift_change()], "feature_issues": []})
    _write(mod.AI_CHANGES_PATH, {"changes": [], "evaluated": False})

    changes, features, labels = mod.collect_changes()
    assert labels == {"drift"}                      # only drift may be reconciled
    assert "suggestion" not in labels and "settings" not in labels
    assert len(changes) == 1                         # the drift change


def test_ai_evaluated_is_reconcilable(tmp_path):
    """A present ai file (ran) -> suggestion+settings reconcilable; flag defaults True."""
    mod = _load_manage_prs()
    _point(mod, tmp_path)
    _write(mod.DRIFT_CHANGES_PATH, {"changes": [], "feature_issues": []})
    _write(mod.AI_CHANGES_PATH, {"changes": [_suggestion_change()]})  # no flag -> True

    changes, features, labels = mod.collect_changes()
    assert labels == {"drift", "suggestion", "settings"}
    assert len(changes) == 1


def test_missing_ai_file_not_reconcilable(tmp_path):
    """No ai file at all -> don't reconcile suggestion/settings (defensive)."""
    mod = _load_manage_prs()
    _point(mod, tmp_path)
    _write(mod.DRIFT_CHANGES_PATH, {"changes": [], "feature_issues": []})
    # ai file intentionally absent
    changes, features, labels = mod.collect_changes()
    assert labels == {"drift"}


def test_dedup_by_branch(tmp_path):
    """Two records targeting the same branch -> first wins."""
    mod = _load_manage_prs()
    _point(mod, tmp_path)
    dup = _suggestion_change()
    _write(mod.DRIFT_CHANGES_PATH, {"changes": [], "feature_issues": []})
    _write(mod.AI_CHANGES_PATH, {"changes": [dup, dict(dup, title="other")]})
    changes, features, labels = mod.collect_changes()
    assert len(changes) == 1
    assert changes[0]["title"] == "add def"
