import os
import sys
from unittest.mock import Mock

import pytest

from src.ui import app as ui


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("user_value", [None, "false", "true", ""])
def test_renderer_default_is_set_before_flet_starts(monkeypatch, platform, user_value):
    monkeypatch.setattr(sys, "platform", platform)
    if user_value is None:
        monkeypatch.delenv("FLET_NO_IMPELLER", raising=False)
    else:
        monkeypatch.setenv("FLET_NO_IMPELLER", user_value)
    seen = []
    run = Mock(side_effect=lambda *a, **k: seen.append(os.environ.get("FLET_NO_IMPELLER")))
    monkeypatch.setattr(ui.ft, "run", run)
    app = object.__new__(ui.App)
    app.run()
    expected = "true" if platform == "darwin" and user_value is None else user_value
    assert seen == [expected]
    run.assert_called_once_with(app._main, assets_dir=ui.ASSETS_DIR)
