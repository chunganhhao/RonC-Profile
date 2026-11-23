from src.ui.state import AppState


def test_failed_reason_tooltip():
    from src.ui.components.profile_card import _stamp

    state = AppState()
    state.failed["demo"] = "Could not start browser"
    assert _stamp(False, False, "", state.failed["demo"]).tooltip == "Could not start browser"


def test_localized_launch_error():
    from src.core.strings import localize_launch_error, set_language

    message = "EngineError: A windows persona cannot be presented consistently on a linux host: native worker WebGPU can reveal the host GPU; use a same-OS profile until a verified native core is available"
    try:
        set_language("vi")
        result = localize_launch_error(message)
        assert "EngineError" not in result
        assert "windows" in result and "linux" in result
        assert "hệ điều hành" in result
        set_language("en")
        assert "Choose the same OS" in localize_launch_error(message)
        assert localize_launch_error("EngineError: Could not start") == "Could not start"
        assert localize_launch_error("Connection refused") == "Connection refused"
        assert localize_launch_error("") == ""
    finally:
        set_language("en")


def test_select_all_accumulates_across_pages():
    """select_all should add to existing selection, not replace it."""
    state = AppState()
    state.toggle_selection("profile-a")
    state.toggle_selection("profile-b")

    # Simulate selecting a second page
    state.select_all(["profile-c", "profile-d"])

    selected = state.selected_names()
    assert "profile-a" in selected
    assert "profile-b" in selected
    assert "profile-c" in selected
    assert "profile-d" in selected


def test_select_all_deduplicates():
    """select_all with already-selected names should not duplicate."""
    state = AppState()
    state.toggle_selection("profile-a")

    state.select_all(["profile-a", "profile-b"])

    selected = state.selected_names()
    assert selected == {"profile-a", "profile-b"}
