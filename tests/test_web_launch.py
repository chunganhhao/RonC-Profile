import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PANEL = (Path(__file__).parents[1] / "src/web/index.html").read_text(encoding="utf-8")


def run_js(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for panel JavaScript tests")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("host", ["windows", "macos", "linux", None])
def test_editor_uses_server_host_for_chrome(host):
    handler = re.search(r"engine.onchange = \(\) => \{(.*?)\n    \};", PANEL, re.S)[1]
    assert 'fetch("/api/v1/info")' in PANEL
    assert "hostOS = info.host_os" in PANEL
    result = run_js(
        f"""
        const hostOS = {json.dumps(host)}, os = {{value: 'macos'}}, engine = {{value: 'chrome'}};
        let p = null, hwChanged = false;
        function renderHwNote() {{}}
        function change() {{{handler}}}
        change();
        const chrome = os.value;
        engine.value = 'camoufox'; change();
        const camoufox = os.value;
        p = {{}}; os.value = 'windows'; engine.value = 'chrome'; change();
        console.log(JSON.stringify([chrome, camoufox, os.value]));
        """
    )
    assert result == [host or "", "macos", "windows"]
    assert "os_type: os.value || undefined" in PANEL


@pytest.mark.parametrize("language", ["en", "vi"])
def test_panel_localizes_cross_os_error(language):
    function = re.search(r"function localizeLaunchError\(message\) \{.*?\n\}", PANEL, re.S)[0]
    messages = re.findall(r'chrome_cross_os: (".*?")', PANEL)
    template = json.loads(messages[0 if language == "en" else 1])
    result = run_js(
        rf"""
        const template = {json.dumps(template)};
        function t(key, values) {{
          return template.replace(/\{{(\w+)\}}/g, (_, k) => values[k]);
        }}
        {function}
        console.log(JSON.stringify([
          localizeLaunchError('Launch failed: EngineError: A windows persona cannot be presented consistently on a linux host: native worker WebGPU'),
          localizeLaunchError('EngineError: Could not start'),
          localizeLaunchError(''),
          localizeLaunchError('Connection refused')
        ]));
        """
    )
    assert "Windows" in result[0] and "Linux" in result[0]
    assert "EngineError" not in result[0]
    assert (
        "hệ điều hành" if language == "vi" else "Chrome profiles must use the same OS"
    ) in result[0]
    assert "WebGPU" not in result[0]
    assert result[1:] == ["Could not start", "", "Connection refused"]
