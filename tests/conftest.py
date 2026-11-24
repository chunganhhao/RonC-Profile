import pytest


@pytest.fixture(autouse=True)
def isolate_browser_bundle(monkeypatch):
    # Unit tests must never edit the installed browser bundle.
    from src.services.browser import runner

    monkeypatch.setattr(runner, "ensure_google_search", lambda: False)


@pytest.fixture(autouse=True)
def isolate_preferences_file(monkeypatch, tmp_path):
    # Appearance changes made by tests must not land in the working directory.
    from src.ui import app

    monkeypatch.setattr(app, "PREFERENCES_FILE", str(tmp_path / "preferences.json"))
