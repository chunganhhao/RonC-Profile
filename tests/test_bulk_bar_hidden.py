import flet as ft

from src.core.strings import get_string
from src.models.profile import Profile
from src.ui.components.bulk_bar import rebuild_bulk_bar
from src.ui.state import AppState


def _texts(control):
    found = []

    def walk(c):
        if isinstance(c, ft.Text) and c.visible is not False:
            found.append(c.value)
        for child in getattr(c, "controls", None) or []:
            walk(child)
        if (content := getattr(c, "content", None)) is not None:
            walk(content)

    walk(control)
    return found


def test_bulk_bar_counts_selected_profiles_the_filter_hides():
    state, bar = AppState(), ft.Row()
    state.toggle_selection("alpha")
    state.toggle_selection("beta")
    noop = dict.fromkeys(("launch", "stop", "delete", "clear"), lambda: None)

    rebuild_bulk_bar(bar, state, [Profile(name="beta")], noop)
    assert get_string("selected_count", count=2) in _texts(bar)
    assert get_string("selected_hidden_count", count=1) in _texts(bar)

    rebuild_bulk_bar(bar, state, [Profile(name="alpha"), Profile(name="beta")], noop)
    assert get_string("selected_hidden_count", count=1) not in _texts(bar)
    assert state.selected_names() == {"alpha", "beta"}
