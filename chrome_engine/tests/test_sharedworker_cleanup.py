"""Actual ChromeEngine/Chromium DOM regression for close ownership and replay."""

import asyncio
from pathlib import Path

import pytest

from chrome_engine.e2e.sharedworker_cleanup import run


def test_sharedworker_cleanup_dom_e2e(tmp_path):
    if not Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").is_file():
        pytest.skip("local Google Chrome unavailable")
    result = asyncio.run(run(tmp_path))
    assert result["parentRefs"] == result["childRefs"] == result["unrelatedHooks"] == 0
    assert result["plainCloseCalls"] <= 4
    assert result["siblingAlive"] is True
