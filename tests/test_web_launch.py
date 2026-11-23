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


@pytest.mark.parametrize("host", ["windows", "macos", "linux"])
def test_editor_uses_server_host_for_chrome(host):
    handler = re.search(r"engine.onchange = \(\) => \{(.*?)\n    \};", PANEL, re.S)[1]
    assert 'fetch("/api/v1/info")' in PANEL
    assert "hostOS = info.host_os" in PANEL
    result = run_js(
        f"""
        const hostOS = {json.dumps(host)}, os = {{value: 'macos'}}, engine = {{value: 'chrome'}};
        let p = null, hwChanged = false, osChanged = false;
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
    assert result == [host, "macos", "windows"]
    assert "os_type: os.value," in PANEL
    assert '[["", t("geo_auto")], ["windows", "Windows"]' not in PANEL
    assert "initial_fingerprint: (isChrome && (!p || hwChanged)) ? fpPayload : undefined" in PANEL


def test_engine_change_preserves_explicit_os():
    engine_handler = re.search(r"engine.onchange = \(\) => \{(.*?)\n    \};", PANEL, re.S)[1]
    os_handler = re.search(r"os.onchange = \(\) => \{(.*?)\n    \};", PANEL, re.S)[1]
    result = run_js(
        f"""
        const hostOS = 'linux', os = {{value: 'windows'}}, engine = {{value: 'camoufox'}};
        const p = null, coresSelect = {{value: ''}};
        let osChanged = false, hwChanged = false, seedRolled = false;
        let coresOverridden = false, currentSeed = 123456789;
        function renderHwNote() {{}}
        function pickOS() {{{os_handler}}}
        function changeEngine() {{{engine_handler}}}
        pickOS();
        engine.value = 'chrome'; changeEngine();
        const chromeOS = os.value;
        engine.value = 'camoufox'; changeEngine();
        console.log(JSON.stringify([chromeOS, os.value]));
        """
    )
    assert result == ["windows", "windows"]


@pytest.mark.parametrize("failure", ["network", "http", "json"])
def test_info_failure_prevents_editor_access(failure):
    boot = PANEL[PANEL.rindex("(async () => {") :].split("</script>")[0]
    result = run_js(
        f"""
        let hostOS, token = '', appShown = false, errorShown = false;
        function applyI18n() {{}}
        function applyTheme() {{}}
        function h() {{}}
        function t(key) {{ return key; }}
        function showApp() {{ appShown = true; }}
        const document = {{body: {{replaceChildren() {{ errorShown = true; }}}}}};
        async function fetch() {{
          if ({json.dumps(failure)} === 'network') throw new Error('offline');
          return {{ok: {json.dumps(failure != "http")}, status: 500, statusText: 'error',
            async json() {{ throw new Error('invalid JSON'); }} }};
        }}
        await {boot.rstrip().rstrip(";")};
        console.log(JSON.stringify([appShown, errorShown]));
        """
    )
    assert result == [False, True]


@pytest.mark.parametrize("editing,changed", [(False, False), (True, True), (True, False)])
def test_editor_sends_concrete_os_and_keeps_chrome_seed_and_cores(editing, changed):
    payload = PANEL.split('const isChrome = engine.value === "chrome";')[-1].split(
        "const rawBackup"
    )[0]
    expression = re.search(r"initial_fingerprint: (.*),", PANEL)[1]
    result = run_js(
        f"""
        const engine = {{value: 'chrome'}}, os = {{value: 'linux'}};
        const p = {"{}" if editing else "null"}, hwChanged = {json.dumps(changed)};
        const seedRolled = true, currentSeed = 123456789, coresOverridden = true;
        const coresSelect = {{value: '12'}}, isChrome = true;
        {payload}
        console.log(JSON.stringify({{os_type: os.value, initial_fingerprint: {expression}}}));
        """
    )
    expected = {"os_type": "linux"}
    if not editing or changed:
        expected["initial_fingerprint"] = {
            "platform": "linux",
            "seed": 123456789,
            "hardware_concurrency": 12,
        }
    assert result == expected


@pytest.mark.parametrize(
    ("initial", "value", "expected"),
    [
        ("https://example.test/start", "https://example.test/start", {}),
        ("https://example.test/start", "", {"start_url": ""}),
        (
            "https://example.test/start",
            " https://example.test/new ",
            {"start_url": "https://example.test/new"},
        ),
        ("", "https://example.test/start", {"start_url": "https://example.test/start"}),
    ],
)
def test_editor_only_sends_changed_startup_url(initial, value, expected):
    editor = PANEL.split("function openEditor(", 1)[1]
    expression = re.search(r"^\s*start_url: (.*),$", editor, re.M)[1]
    assert (
        run_js(
            f"""
        const initialStartUrl = {json.dumps(initial)}, startUrl = {{value: {json.dumps(value)}}};
        console.log(JSON.stringify({{start_url: {expression}}}));
        """
        )
        == expected
    )


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
