import json

import pytest

from src.services.browser import search


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / "policies.json"
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    monkeypatch.setattr(search, "get_path", lambda _: str(path))
    return path


def test_legacy_policy_changes_only_search_and_is_idempotent(policy):
    other = {"DisableTelemetry": True, "ExtensionSettings": {"*": {"installation_mode": "blocked"}}}
    data = {
        "policies": dict(
            other,
            SearchEngines={
                "Default": "None",
                "PreventInstalls": True,
                "Remove": ["Google", "Bing"],
                "Add": [
                    {"Name": "None", "URLTemplate": "http://127.0.0.1"},
                    {"Name": "Custom", "URLTemplate": "https://example.org/?q={searchTerms}"},
                ],
            },
        )
    }
    policy.write_text(json.dumps(data))
    policy.chmod(0o644)
    assert search.ensure_google_search()
    actual = json.loads(policy.read_text())["policies"]
    assert {k: actual[k] for k in other} == other
    engines = actual.pop("SearchEngines")
    assert engines["Default"] == "Google"
    assert engines["Remove"] == ["Bing"]
    assert engines["PreventInstalls"] is True
    assert engines["Add"][-1] == {
        "Name": "Google",
        "Method": "GET",
        "URLTemplate": "https://www.google.com/search?q={searchTerms}",
    }
    assert engines["Add"][0]["Name"] == "Custom"
    before = policy.read_bytes()
    assert not search.ensure_google_search()
    assert policy.read_bytes() == before
    assert policy.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("data", [{}, {"policies": {"SearchEngines": {"Default": "Custom"}}}])
def test_other_builds_and_custom_policy_unchanged(policy, data):
    policy.write_text(json.dumps(data))
    before = policy.read_bytes()
    assert not search.ensure_google_search()
    assert policy.read_bytes() == before


def test_missing_policy_is_noop(policy):
    assert not search.ensure_google_search()
    assert not policy.exists()


def test_failed_replace_preserves_original(policy, monkeypatch):
    policy.write_text(
        json.dumps(
            {
                "policies": {
                    "SearchEngines": {
                        "Default": "None",
                        "Add": [{"Name": "None", "URLTemplate": "http://127.0.0.1"}],
                    }
                }
            }
        )
    )
    before = policy.read_bytes()

    def fail(*_):
        raise PermissionError("read-only bundle")

    monkeypatch.setattr(search.os, "replace", fail)
    with pytest.raises(PermissionError):
        search.ensure_google_search()
    assert policy.read_bytes() == before
    assert list(policy.parent.iterdir()) == [policy]


def test_explicit_binary_uses_its_resources(policy, monkeypatch):
    root = policy.parent / "Custom.app" / "Contents"
    target = root / "Resources" / "distribution" / "policies.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "policies": {
                    "SearchEngines": {
                        "Default": "None",
                        "Add": [{"Name": "None", "URLTemplate": "http://127.0.0.1"}],
                    }
                }
            }
        )
    )
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(root / "MacOS" / "camoufox"))
    assert search.ensure_google_search()
    assert not policy.exists()
