import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.models.profile import Profile
from src.services.browser import cookies


def _ext_cookie(**overrides):
    base = {
        "domain": ".example.com",
        "hostOnly": False,
        "httpOnly": True,
        "name": "sid",
        "path": "/",
        "sameSite": "lax",
        "secure": True,
        "session": False,
        "expirationDate": 2_000_000_000,
        "value": "abc",
    }
    base.update(overrides)
    return base


def test_parse_extension_json():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie()]))
    assert c == {
        "name": "sid",
        "value": "abc",
        "domain": ".example.com",
        "path": "/",
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
        "expires": 2_000_000_000.0,
    }


def test_parse_host_only_keeps_bare_domain():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(domain="app.example.com", hostOnly=True)]))
    assert c["domain"] == "app.example.com"


def test_parse_non_host_only_adds_leading_dot():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(domain="example.com", hostOnly=False)]))
    assert c["domain"] == ".example.com"


def test_same_site_none_forces_secure():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(sameSite="no_restriction", secure=False)]))
    assert c["sameSite"] == "None"
    assert c["secure"] is True


def test_session_cookie_gets_long_lifetime():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(session=True, expirationDate=None)]))
    assert c["expires"] > time.time() + 300 * 24 * 3600


def test_parse_playwright_layout():
    pw = {
        "name": "a",
        "value": "1",
        "domain": ".x.com",
        "path": "/",
        "expires": 2_000_000_000,
        "httpOnly": False,
        "secure": False,
        "sameSite": "Strict",
    }
    [c] = cookies.parse_cookies(json.dumps({"cookies": [pw]}))
    assert c["sameSite"] == "Strict"
    assert c["expires"] == 2_000_000_000.0


def test_parse_skips_entries_without_name_or_domain():
    parsed = cookies.parse_cookies(json.dumps([{"value": "x"}, _ext_cookie()]))
    assert [c["name"] for c in parsed] == ["sid"]


def test_parse_invalid_json_raises():
    with pytest.raises(cookies.CookieError):
        cookies.parse_cookies("[not json")


def test_parse_netscape_with_http_only_and_session():
    text = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.shop.vn\tTRUE\t/\tTRUE\t2000000000\ttoken\tt0k\n"
        ".shop.vn\tTRUE\t/\tFALSE\t0\tvisitor\txyz\n"
        "# a comment\n"
        "malformed line\n"
    )
    token, visitor = cookies.parse_cookies(text)
    assert token["httpOnly"] is True and token["secure"] is True
    assert token["expires"] == 2_000_000_000.0
    assert visitor["httpOnly"] is False
    assert visitor["expires"] > time.time()


def test_netscape_round_trip():
    source = [
        {
            "name": "a",
            "value": "1",
            "domain": ".x.com",
            "path": "/",
            "expires": 2_000_000_000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "b",
            "value": "2",
            "domain": "y.com",
            "path": "/p",
            "expires": 2_000_000_000,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
    ]
    parsed = cookies.parse_cookies(cookies._to_netscape(source))
    assert [(c["name"], c["domain"], c["path"], c["httpOnly"]) for c in parsed] == [
        ("a", ".x.com", "/", True),
        ("b", "y.com", "/p", False),
    ]


def test_extension_json_round_trip():
    source = [
        {
            "name": "a",
            "value": "1",
            "domain": "y.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        }
    ]
    [ext] = cookies._to_extension_json(source)
    assert ext["hostOnly"] is True and ext["session"] is True
    assert "expirationDate" not in ext
    assert ext["sameSite"] == "no_restriction"
    [back] = cookies.parse_cookies(json.dumps([ext]))
    assert back["domain"] == "y.com" and back["sameSite"] == "None"


def test_export_rejects_unknown_format():
    with pytest.raises(cookies.CookieError):
        cookies.export_cookies("p", "windows", "xml")


def test_non_numeric_expiry_is_treated_as_session():
    [c] = cookies.parse_cookies(json.dumps([_ext_cookie(expirationDate="session")]))
    assert c["expires"] > time.time() + 300 * 24 * 3600


INVALID_COOKIES = [
    ([_ext_cookie(domain=123)], "domain"),
    ([_ext_cookie(name=[])], "name"),
    ([_ext_cookie(value={})], "value"),
    ([_ext_cookie(path=123)], "path"),
    ([_ext_cookie(sameSite=[])], "sameSite"),
    ([_ext_cookie(hostOnly=0)], "hostOnly"),
    ([_ext_cookie(httpOnly="false")], "httpOnly"),
    ([_ext_cookie(secure=[])], "secure"),
    ([_ext_cookie(session=1)], "session"),
    ([_ext_cookie(expirationDate={})], "expirationDate"),
    ([_ext_cookie(expires=[])], "expires"),
    ([_ext_cookie(domain=None)], "domain"),
    ([_ext_cookie(), 123], "object"),
    ([None], "object"),
    ({"cookies": {}}, "list"),
]


@pytest.mark.parametrize("data, message", INVALID_COOKIES)
def test_parse_rejects_wrong_cookie_types(data, message):
    with pytest.raises(cookies.CookieError, match=message):
        cookies.parse_cookies(json.dumps(data))


@pytest.fixture
def cookie_client(monkeypatch):
    profile = Profile("p", engine="chrome")
    container = SimpleNamespace(
        profile_manager=SimpleNamespace(profiles={"p": profile}),
        browser_launcher=SimpleNamespace(
            is_running=lambda _: False, exclusive=lambda _: nullcontext()
        ),
    )
    return TestClient(
        create_app(container), base_url="http://127.0.0.1", raise_server_exceptions=False
    )


@pytest.mark.parametrize("data, message", INVALID_COOKIES + [([], "No valid cookies")])
def test_cookie_route_rejects_malformed_input(cookie_client, monkeypatch, data, message):
    def unexpected_tool(*args, **kwargs):
        pytest.fail("Malformed input must not reach the browser")

    monkeypatch.setattr(cookies, "_call_tool", unexpected_tool)
    response = cookie_client.post("/api/v1/profiles/p/cookies", json={"content": json.dumps(data)})
    assert response.status_code == 200, response.text
    assert response.json()["success"] is False
    assert message in response.json()["error"]


def test_cookie_route_valid_import(cookie_client, monkeypatch):
    def import_tool(action, name, os_type, payload, engine):
        assert (action, name, engine) == ("import", "p", "chrome")
        assert json.loads(payload)[0]["domain"] == ".example.com"
        return {"imported": 1}

    monkeypatch.setattr(cookies, "_call_tool", import_tool)
    response = cookie_client.post(
        "/api/v1/profiles/p/cookies", json={"content": json.dumps([_ext_cookie()])}
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "imported": 1, "error": None}
