"""Field-specific native editors use local drafts and the existing commit callback."""

from types import SimpleNamespace

import pytest

from src.models.profile import Profile
from src.ui.components.profile_card import build_profile_card
from src.ui.dialogs.profile_field import open_profile_field_dialog


class Page:
    def __init__(self):
        self.dialog = None
        self.closed = False

    def show_dialog(self, dialog):
        self.dialog = dialog

    def pop_dialog(self):
        self.closed = True

    def update(self):
        pass


def walk(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from walk(child)
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        yield from walk(content)


def test_native_field_dialog_changes_name_not_account():
    page, saved = Page(), []
    profile = Profile("old", account_uid="fixed@example.test")
    open_profile_field_dialog(page, profile, "name", lambda value: saved.append(value))
    field = next(c for c in walk(page.dialog.content) if c.__class__.__name__ == "TextField")
    field.value = "new"
    page.dialog.actions[-1].on_click(None)
    assert saved == ["new"] and page.closed
    assert profile.name == "old" and profile.account_uid == "fixed@example.test"


def test_cancel_and_save_error_preserve_draft():
    page = Page()
    profile = Profile("fixture", notes="before")
    open_profile_field_dialog(page, profile, "notes", lambda _: "Store busy")
    field = next(c for c in walk(page.dialog.content) if c.__class__.__name__ == "TextField")
    field.value = "after"
    page.dialog.actions[-1].on_click(None)
    assert not page.closed and profile.notes == "before"
    assert any(getattr(c, "value", None) == "Store busy" for c in walk(page.dialog.content))
    page.dialog.actions[0].on_click(None)
    assert page.closed and profile.notes == "before"


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "1000001"])
def test_order_rejects_invalid_values(value):
    page, saved = Page(), []
    open_profile_field_dialog(page, Profile("fixture"), "display_order", saved.append)
    field = next(c for c in walk(page.dialog.content) if c.__class__.__name__ == "TextField")
    field.value = value
    page.dialog.actions[-1].on_click(None)
    assert not page.closed and saved == []


def test_tags_are_local_and_removable():
    page, saved = Page(), []
    p = Profile("fixture", group="Gmail")
    open_profile_field_dialog(page, p, "tags", saved.append)
    controls = list(walk(page.dialog.content))
    field = next(c for c in controls if c.__class__.__name__ == "TextField")
    field.value = "AGY"
    field.on_submit(None)
    remove = next(
        c for c in walk(page.dialog.content) if getattr(c, "tooltip", None) == "Remove Gmail"
    )
    remove.on_click(None)
    assert p.group == "Gmail" and not p.tags
    page.dialog.actions[-1].on_click(None)
    assert saved == [["AGY"]]


def test_card_exposes_copy_and_five_field_editors():
    clicked = []
    p = Profile("fixture", account_uid="user@example.test")
    card = build_profile_card(
        p,
        False,
        False,
        lambda _: None,
        lambda _: None,
        lambda _: None,
        on_copy_user=lambda n: clicked.append(n),
        on_edit_field=lambda n, f, i: clicked.append((n, f, i)),
    )
    buttons = [c for c in walk(card) if c.__class__.__name__ == "IconButton"]
    edits = [
        c
        for c in buttons
        if getattr(c, "icon_size", None) == 14 and c.width == 28 and c.height == 36
    ]
    assert len(edits) == 5
    for button in edits:
        button.on_click(SimpleNamespace())
    assert {v[1] for v in clicked} == {"name", "display_order", "tags", "proxy", "notes"}
    copy = next(c for c in buttons if c.height == 28)
    copy.on_click(None)
    assert clicked[-1] == "fixture"
