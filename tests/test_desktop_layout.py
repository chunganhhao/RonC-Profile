"""Resizing keeps real controls and their actions instead of hiding profile data."""

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import flet as ft
import pytest

from src.models.profile import Profile
from src.ui.components.content_area import build_content_area
from src.ui.components.profile_card import build_header_row, build_profile_card
from src.ui.theme import page as page_theme


@pytest.mark.parametrize(
    ("probe_output", "probe_error"),
    [(b"[]", None), (None, FileNotFoundError()), (None, subprocess.TimeoutExpired("osascript", 2))],
    ids=["no-display", "unavailable", "timeout"],
)
def test_macos_startup_without_appkit_or_work_area_probe(monkeypatch, probe_output, probe_error):
    monkeypatch.setitem(sys.modules, "AppKit", None)
    monkeypatch.setattr(page_theme, "sys", SimpleNamespace(platform="darwin"))
    page = SimpleNamespace(window=ft.Window())
    with patch("subprocess.check_output", return_value=probe_output, side_effect=probe_error):
        page_theme.configure_page(page)
    window = page.window
    assert (window.left, window.top) == (None, None)
    assert (window.width, window.height, window.min_width, window.min_height) == (
        1440,
        880,
        900,
        600,
    )


@pytest.mark.parametrize(
    ("work_area", "expected"),
    [
        ([[0, 30, 1600, 1088]], (80, 134, 1440, 880, 900, 600)),
        ([[72, 30, 800, 530]], (72, 30, 800, 530, 800, 530)),
    ],
    ids=["center", "clamp"],
)
def test_macos_startup_window_fits_the_work_area(monkeypatch, work_area, expected):
    monkeypatch.setitem(sys.modules, "AppKit", None)
    monkeypatch.setattr(page_theme, "sys", SimpleNamespace(platform="darwin"))
    page = SimpleNamespace(window=ft.Window())
    with patch("subprocess.check_output", return_value=json.dumps(work_area).encode()):
        page_theme.configure_page(page)
    window = page.window
    assert (
        window.left,
        window.top,
        window.width,
        window.height,
        window.min_width,
        window.min_height,
    ) == expected


def walk(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from walk(content)


@pytest.mark.parametrize("width", [610, 890, 1150, 1359])
def test_resize_preserves_fields_selection_and_actions(width):
    clicked = []
    card = build_profile_card(
        Profile("Demo", engine="chrome", notes="Keep this note"),
        False,
        False,
        clicked.append,
        clicked.append,
        clicked.append,
        is_selected=True,
        on_select=clicked.append,
        on_edit_field=lambda *args: clicked.append(args),
    )
    ledger = card.content
    original = {id(c) for c in walk(ledger)}
    with patch.object(ft.Container, "update"):
        card.set_layout_width(width)
        assert (original - {id(ledger)}).issubset({id(c) for c in walk(card)})
        checkbox = next(c for c in walk(card) if isinstance(c, ft.Checkbox))
        assert checkbox.value
        checkbox.on_change(None)
        assert clicked == ["Demo"]
        identity, actions = card.content.controls[0].controls
        assert identity.col == (7 if width >= 820 else 12)
        assert actions.col == (5 if width >= 820 else 12)
        compact = card.content
        card.set_layout_width(width - 1)
        assert card.content is compact
        card.set_layout_width(1700)
        assert card.content is ledger
        assert {id(c) for c in walk(ledger)} == original


def test_compact_header_keeps_select_all():
    clicked = []
    header = build_header_row(False, False, clicked.append)
    ledger = header.content
    with patch.object(ft.Container, "update"):
        header.set_layout_width(610, header=True)
        next(c for c in walk(header) if isinstance(c, ft.Checkbox)).on_change(None)
        assert clicked == [True]
        header.set_layout_width(1700, header=True)
        assert header.content is ledger


def test_refreshed_rows_start_in_measured_layout_without_a_ledger_flash():
    with patch.object(ft.Container, "update") as update:
        card = build_profile_card(
            Profile("Demo", engine="chrome"),
            False,
            False,
            lambda _: None,
            lambda _: None,
            lambda _: None,
            layout_width=610,
        )
        assert isinstance(card.content, ft.Column)
        compact = card.content
        update.assert_not_called()
        card.set_layout_width(610)
        assert card.content is compact
        update.assert_not_called()


def test_workspace_fits_viewport_instead_of_forcing_horizontal_scroll():
    area = build_content_area(ft.Text(""), ft.Column(), ft.IconButton(), ft.IconButton(), ft.Text())
    controls = {getattr(c, "key", None): c for c in walk(area)}
    table = controls["profiles_table_container"]
    with patch.object(ft.Container, "update"):
        for width in (610, 890, 1150, 1700, 890):
            controls["profiles_sheet_container"].on_size_change(SimpleNamespace(width=width))
            assert table.width == width - 2


def test_sheet_batches_responsive_changes_for_200_rows():
    def noop(_):
        pass

    cards = [
        build_profile_card(
            Profile(f"Demo {i}", engine="chrome"),
            False,
            False,
            noop,
            noop,
            noop,
            layout_width=610,
        )
        for i in range(200)
    ]
    header = build_header_row(False, False, noop, layout_width=610)
    measured_widths = []
    area = build_content_area(
        ft.Text(""),
        ft.Column(controls=cards),
        ft.IconButton(),
        ft.IconButton(),
        ft.Text(),
        header_slot=ft.Container(content=header),
        initial_table_width=610,
        on_table_resize=measured_widths.append,
    )
    controls = {getattr(c, "key", None): c for c in walk(area)}
    table = controls["profiles_table_container"]
    sheet = controls["profiles_sheet_container"]
    assert all(row.on_size_change is None for row in [header, *cards])

    compact = [card.content for card in cards]
    compact_header = header.content
    with patch.object(ft.Container, "update", autospec=True) as update:
        sheet.on_size_change(SimpleNamespace(width=652))
        update.assert_called_once_with(table)
        assert all(card.content is layout for card, layout in zip(cards, compact, strict=True))
        update.reset_mock()

        sheet.on_size_change(SimpleNamespace(width=652))
        update.assert_not_called()
        assert measured_widths == [650, 650]

        sheet.on_size_change(SimpleNamespace(width=1092))
        update.assert_called_once_with(table)
        assert header.content is compact_header
        assert all(card.content.controls[1].controls[0].col == 3 for card in cards)
        update.reset_mock()

        sheet.on_size_change(SimpleNamespace(width=1402))
        update.assert_called_once_with(table)
        assert all(isinstance(row.content, ft.Row) for row in [header, *cards])


def test_profile_dialog_reflows_without_losing_draft():
    from src.ui.dialogs.profile import open_profile_dialog

    page = SimpleNamespace(width=900, height=600, on_resize=None, update=lambda: None)
    shown = []
    page.show_dialog = shown.append
    open_profile_dialog(page, None, lambda *args, **kwargs: None)
    dialog = shown[0]
    field = next(c for c in walk(dialog.content) if isinstance(c, ft.TextField))
    field.value = "Unsaved layout draft"
    original_fields = [id(c) for c in walk(dialog.content) if isinstance(c, ft.TextField)]
    for width in (1440, 900, 1180, 900):
        page.width = width
        page.on_resize(None)
        assert [
            id(c) for c in walk(dialog.content) if isinstance(c, ft.TextField)
        ] == original_fields
        assert field.value == "Unsaved layout draft"
