"""Desktop choices survive a restart; the profile filter does not."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import flet as ft
import pytest

from src.core import config as app_config
from src.core.container import Container
from src.core.strings import current_language, set_language
from src.services.profile import manager as pm_mod
from src.ui import preferences
from src.ui.app import App
from src.ui.preferences import Preferences
from src.ui.theme import page as page_theme
from src.ui.theme.colors import (
    current_accent_color,
    current_theme_mode,
    set_accent_color,
    set_theme_mode,
)


@pytest.fixture(autouse=True)
def restore_appearance():
    language, theme, accent = current_language(), current_theme_mode(), current_accent_color()
    yield
    set_language(language)
    set_theme_mode(theme)
    set_accent_color(accent)


def test_saved_preferences_round_trip(tmp_path):
    path = tmp_path / "preferences.json"
    saved = Preferences("vi", "light", "green", 50, (40.0, 60.0, 1200.0, 760.0))
    assert preferences.save(path, saved)
    assert preferences.load(path) == saved
    assert json.loads(path.read_text(encoding="utf-8"))["window"] == {
        "left": 40.0,
        "top": 60.0,
        "width": 1200.0,
        "height": 760.0,
    }


@pytest.mark.parametrize(
    "content",
    [None, "", "{not json", "[]", '"vi"', "\udcff"],
    ids=["missing", "empty", "corrupt", "list", "string", "undecodable"],
)
def test_missing_or_corrupt_file_falls_back_to_defaults(tmp_path, content):
    path = tmp_path / "preferences.json"
    if content == "\udcff":
        path.write_bytes(b"\xff\xfe\x00")
    elif content is not None:
        path.write_text(content, encoding="utf-8")
    assert preferences.load(path) == Preferences()


def test_invalid_values_are_dropped_one_by_one(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "language": "fr",
                "theme": "light",
                "accent": "pink",
                "page_size": 50.0,
                "window": {"left": 0, "top": 0, "width": -1, "height": 600},
            }
        ),
        encoding="utf-8",
    )
    assert preferences.load(path) == Preferences(theme="light")


def test_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    preferences.save(path, Preferences(language="vi"))

    def fail_replace(src, dst):
        assert json.loads(Path(src).read_text(encoding="utf-8"))["language"] == "en"
        raise OSError("disk full")

    monkeypatch.setattr(preferences.os, "replace", fail_replace)
    assert not preferences.save(path, Preferences(language="en"))
    assert preferences.load(path) == Preferences(language="vi")
    assert [p.name for p in tmp_path.iterdir()] == ["preferences.json"]


AREAS = [[0, 30, 1600, 1088], [1600, -200, 1280, 800]]


def _configure(monkeypatch, bounds, areas=AREAS, platform="darwin"):
    monkeypatch.setattr(page_theme, "sys", SimpleNamespace(platform=platform))
    page = SimpleNamespace(window=ft.Window())
    with patch("subprocess.check_output", return_value=json.dumps(areas).encode()):
        page_theme.configure_page(page, bounds)
    w = page.window
    return w.left, w.top, w.width, w.height


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ((100, 80, 1200, 760), (100, 80, 1200, 760)),
        ((1700, -150, 1000, 700), (1700, -150, 1000, 700)),
        ((-300, 80, 1200, 760), (-300, 80, 1200, 760)),
        ((1700, -150, 1500, 1000), (1600, -200, 1280, 800)),
    ],
    ids=["main-screen", "second-screen", "partly-off-left", "shrunk-to-screen"],
)
def test_saved_window_is_restored_on_a_visible_screen(monkeypatch, bounds, expected):
    assert _configure(monkeypatch, bounds) == expected


@pytest.mark.parametrize(
    "bounds",
    [(4000, 80, 1200, 760), (100, 2000, 1200, 760), (-1150, 80, 1200, 760), (100, 10, 1200, 760)],
    ids=["right-of-all-screens", "below-all-screens", "title-bar-off-left", "under-menu-bar"],
)
def test_off_screen_window_uses_the_centered_default(monkeypatch, bounds):
    assert _configure(monkeypatch, bounds) == (80, 134, 1440, 880)


def test_window_is_not_restored_without_screen_information(monkeypatch):
    assert _configure(monkeypatch, (100, 80, 1200, 760), areas=[]) == (None, None, 1440, 880)
    assert _configure(monkeypatch, (100, 80, 1200, 760), platform="win32") == (
        None,
        None,
        1440,
        880,
    )


class _Page:
    def __init__(self):
        self.title = ""
        self.window = ft.Window()
        self.padding = self.spacing = 0
        self.fonts = {}
        self.theme_mode = self.theme = None
        self.bgcolor = ""
        self.services = []
        self.controls = []
        self.window_saves = []

    def add(self, ctrl):
        self.controls.append(ctrl)

    def update(self):
        pass

    def run_task(self, fn, *args):
        if fn.__name__ == "_save_window_later":
            self.window_saves.append(fn(*args))

    def settle_window(self):
        saves, self.window_saves = self.window_saves, []

        async def run_all():
            for save in saves:
                await save

        asyncio.run(run_all())


def _launch(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "PROXY_POOL_FILE", str(tmp_path / "proxies.json"))
    monkeypatch.setattr(app_config, "WARMING_FILE", str(tmp_path / "warming.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_FILE", str(tmp_path / "extensions.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_DIR", str(tmp_path / "extensions"))
    monkeypatch.setattr(page_theme, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(App, "_WINDOW_SAVE_DELAY_S", 0)
    app = App(Container())
    app.preferences_file = tmp_path / "preferences.json"
    monkeypatch.setattr(app.warming, "start_daemon", lambda *a, **k: None)
    page = _Page()
    with patch("subprocess.check_output", return_value=json.dumps(AREAS).encode()):
        app._main(page)
    return app, page


def test_choices_survive_a_restart_but_the_filter_resets(tmp_path, monkeypatch):
    app, page = _launch(tmp_path, monkeypatch)
    app._set_language("vi")
    app._set_theme("light")
    app._set_accent("green")
    app._on_change_page_size(50)
    app.filter = "running"
    page.window.left, page.window.top, page.window.width, page.window.height = 120, 90, 1100, 700
    page.window.on_event(SimpleNamespace(type=ft.WindowEventType.MOVED))
    page.settle_window()

    set_language("en")
    set_theme_mode("dark")
    set_accent_color("blue")
    app, page = _launch(tmp_path, monkeypatch)

    assert (current_language(), current_theme_mode(), current_accent_color()) == (
        "vi",
        "light",
        "green",
    )
    assert app.state.items_per_page == 50
    assert app.refs.page_size_dropdown.value == "50"
    assert app.filter == "all"
    w = page.window
    assert (w.left, w.top, w.width, w.height) == (120, 90, 1100, 700)
    assert "filter" not in json.loads((tmp_path / "preferences.json").read_text())


@pytest.mark.parametrize("state", ["full_screen", "maximized", "minimized"])
def test_full_screen_maximized_or_minimized_geometry_is_not_saved(tmp_path, monkeypatch, state):
    app, page = _launch(tmp_path, monkeypatch)
    page.window.left, page.window.top, page.window.width, page.window.height = 0, 0, 1600, 1118
    setattr(page.window, state, True)
    page.window.on_event(SimpleNamespace(type=ft.WindowEventType.RESIZED))
    page.settle_window()
    assert preferences.load(tmp_path / "preferences.json").window is None


def test_a_drag_is_written_at_its_start_and_once_more_when_it_settles(tmp_path, monkeypatch):
    app, page = _launch(tmp_path, monkeypatch)
    writes = []
    real_save = preferences.save
    monkeypatch.setattr(
        preferences,
        "save",
        lambda path, prefs: writes.append(prefs.window) or real_save(path, prefs),
    )
    for left in (100, 140, 180):
        page.window.left, page.window.top, page.window.width, page.window.height = (
            left,
            90,
            1100,
            700,
        )
        page.window.on_event(SimpleNamespace(type=ft.WindowEventType.MOVED))
    page.settle_window()
    assert writes == [(100.0, 90.0, 1100.0, 700.0), (180.0, 90.0, 1100.0, 700.0)]


def test_a_single_move_is_saved_before_the_app_can_quit(tmp_path, monkeypatch):
    app, page = _launch(tmp_path, monkeypatch)
    page.window.left, page.window.top, page.window.width, page.window.height = 120, 90, 1100, 700
    page.window.on_event(SimpleNamespace(type=ft.WindowEventType.MOVED))
    assert preferences.load(tmp_path / "preferences.json").window == (120.0, 90.0, 1100.0, 700.0)
    page.settle_window()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5, None])
def test_unusable_live_bounds_keep_the_last_saved_window(tmp_path, monkeypatch, bad):
    app, page = _launch(tmp_path, monkeypatch)
    page.window.left, page.window.top, page.window.width, page.window.height = 120, 90, 1100, 700
    page.window.on_event(SimpleNamespace(type=ft.WindowEventType.MOVED))
    page.settle_window()
    page.window.width = bad
    page.window.on_event(SimpleNamespace(type=ft.WindowEventType.RESIZED))
    page.settle_window()
    assert preferences.load(tmp_path / "preferences.json").window == (120.0, 90.0, 1100.0, 700.0)


def test_launch_with_a_corrupt_file_uses_defaults(tmp_path, monkeypatch):
    (tmp_path / "preferences.json").write_text("{oops", encoding="utf-8")
    set_language("en")
    set_theme_mode("dark")
    app, page = _launch(tmp_path, monkeypatch)
    assert (current_language(), current_theme_mode(), app.state.items_per_page) == (
        "en",
        "dark",
        20,
    )
    assert (page.window.left, page.window.top) == (80, 134)
