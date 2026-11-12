import pytest


@pytest.fixture(autouse=True)
def isolate_browser_bundle(monkeypatch):
    # Unit tests must never edit the installed browser bundle.
    from src.services.browser import runner

    monkeypatch.setattr(runner, "ensure_google_search", lambda: False)
