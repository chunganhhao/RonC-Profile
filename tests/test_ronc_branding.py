import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.strings import STRINGS


def test_ronc_branding_and_public_logo():
    assets = Path(__file__).resolve().parents[1] / "src" / "assets"
    for language in ("en", "vi"):
        assert STRINGS[language]["app_name"] == "RonC Profile"
        assert STRINGS[language]["window_title"] == "RonC Profile"
    assert (assets / "icon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert (
        hashlib.sha256((assets / "icon.png").read_bytes()).digest()
        == hashlib.sha256((assets / "logo.png").read_bytes()).digest()
    )
    # These public routes do not need a profile manager or a running scheduler.
    client = TestClient(create_app(SimpleNamespace()), base_url="http://127.0.0.1")
    assert client.get("/api/v1/info").json()["name"] == "RonC Profile"
    assert client.get("/openapi.json").json()["info"]["title"] == "RonC Profile API"
    logo = client.get("/assets/icon.png")
    assert logo.status_code == 200
    assert logo.headers["content-type"] == "image/png"
    assert logo.content == (assets / "icon.png").read_bytes()
    assert "<title>RonC Profile</title>" in client.get("/").text
    assert 'href="#fox"' not in client.get("/").text
    assert client.get("/assets/profiles.json").status_code == 404
