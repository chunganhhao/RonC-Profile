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


@pytest.mark.parametrize("overlay", ["modal", "viewer", None])
@pytest.mark.parametrize("tag", ["BUTTON", "INPUT", "TEXTAREA"])
def test_search_shortcut_only_focuses_search_when_no_modal_is_open(overlay, tag):
    start = PANEL.index('document.addEventListener("keydown", (e) => {')
    handler = PANEL[start:].split("\n});", 1)[0] + "\n});"
    result = run_js(
        f"""
        const overlay = {json.dumps(overlay)};
        let keydown, focused = false, prevented = false;
        const document = {{
          activeElement: {{tagName: {json.dumps(tag)}}},
          addEventListener: (_, fn) => keydown = fn,
        }};
        const nodes = {{
          app: {{classList: {{contains: () => false}}}},
          'modal-root': {{lastElementChild: overlay === 'modal' ? {{}} : null}},
          viewer: {{classList: {{contains: () => overlay !== 'viewer'}}}},
          search: {{focus: () => focused = true}},
        }};
        const $ = id => nodes[id];
        {handler}
        keydown({{key: '/', preventDefault: () => prevented = true}});
        console.log(JSON.stringify([focused, prevented]));
        """
    )
    assert result == [tag == "BUTTON" and overlay is None, tag == "BUTTON"]


def test_extension_settings_can_retry_after_a_profile_rename():
    settings = PANEL.split("function openExtensionSettingsModal(", 1)[1].split(
        "\nasync function openExtensionsModal", 1
    )[0]
    result = run_js(
        f"""
        const entry = {{id: 'demo', name: 'Demo', assigned_profiles: ['Alice']}};
        let profiles = [{{name: 'Alice', engine: 'chrome', extension_ids: ['demo']}}];
        let confirm, closed = 0;
        const calls = [], messages = [], enc = encodeURIComponent, t = key => key;
        const toast = message => messages.push(message);
        const modal = render => render(() => closed++);
        const modalHead = () => {{}};
        function h(tag, attrs, ...children) {{
          if (tag === 'button' && children[0] === 'ext_confirm_btn') confirm = attrs.onclick;
          return attrs;
        }}
        async function refresh() {{
          profiles = [{{name: 'Alicia', engine: 'chrome', extension_ids: ['demo']}}];
        }}
        async function api(method, path, body) {{
          calls.push(body.profile_names);
          if (body.profile_names.includes('Alice')) throw {{status: 409}};
        }}
        function openExtensionSettingsModal({settings}
        openExtensionSettingsModal(entry);
        await confirm();
        // The extension card still holds the old name even with the fresh profile list.
        openExtensionSettingsModal(entry);
        await confirm();
        console.log(JSON.stringify([calls, messages, closed]));
        """
    )
    assert result == [[["Alice"], ["Alicia"]], ["ext_selection_changed", "saved"], 1]


@pytest.mark.parametrize("action", ["launch", "stop"])
def test_bulk_browser_action_refreshes_lists_once(action):
    refresh = PANEL.split("async function refresh() {", 1)[1].split("\nconst FILTERS", 1)[0]
    actions = PANEL.split("async function bulk(action) {", 1)[1].split("\nfunction modal", 1)[0]
    result = run_js(
        f"""
        const action = {json.dumps(action)};
        let profiles = ['One', 'Two', 'Three'].map(name => ({{ name }}));
        const selected = new Set(profiles.map(p => p.name));
        let running = action === 'stop' ? new Set(selected) : new Set();
        let extensions = [];
        const busy = new Map(), failed = new Map(), liveSince = new Map(), justLanded = new Set();
        const calls = [];
        const render = () => {{}}, toast = () => {{}}, $ = () => null;
        const t = key => key, enc = encodeURIComponent, localizeLaunchError = text => text;
        async function api(method, path) {{
          calls.push([method, path]);
          if (path === '/profiles?panel=true') return {{ profiles }};
          if (path === '/browser') return {{ running: [...running] }};
          if (path === '/extensions') return {{ extensions }};
          if (path === `/browser/Two/${{action}}`) throw new Error('profile action failed');
          return {{ message: 'started' }};
        }}
        async function refresh() {{{refresh}
        async function bulk(action) {{{actions}
        await bulk(action);
        console.log(JSON.stringify(calls));
        """
    )
    assert result == [
        *[["POST", f"/browser/{name}/{action}"] for name in ("One", "Two", "Three")],
        ["GET", "/profiles?panel=true"],
        ["GET", "/browser"],
        ["GET", "/extensions"],
    ]


@pytest.mark.parametrize("host", ["windows", "macos", "linux"])
def test_editor_uses_server_host_for_chrome(host):
    handler = re.search(r"engine.onchange = \(\) => \{(.*?)\n    \};", PANEL, re.S)[1]
    assert 'fetch("/api/v1/info")' in PANEL
    assert "hostOS = info.host_os" in PANEL
    result = run_js(
        f"""
        const hostOS = {json.dumps(host)}, os = {{value: 'macos'}}, engine = {{value: 'chrome'}};
        const coresSelect = {{value: ''}};
        let p = null, hwChanged = false, osChanged = false;
        function syncCoreOptions() {{}}
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
        function syncCoreOptions() {{}}
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


def test_editor_offers_only_m2_cpu_counts():
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container

    response = TestClient(create_app(Container()), base_url="http://127.0.0.1").get("/")
    assert response.status_code == 200
    html = response.text
    hardware = re.search(r"const panelHardware = .*;", html)
    controls = html.split("    const existingFp = p?.fingerprint;", 1)[1].split(
        "    const webrtcSelect", 1
    )[0]
    assert run_js(
        f"""
        {hardware[0] if hardware else ""}
        const p = {{fingerprint: {{seed: 495, hardware_concurrency: 14}}}};
        const existingFp = p.fingerprint;
        const engine = {{value: 'chrome'}}, os = {{value: 'macos'}};
        function h(tag, attrs, children = []) {{
          return {{...attrs, children, replaceChildren(...items) {{this.children = items;}}}};
        }}
        {controls}
        console.log(JSON.stringify(coresSelect.children.map(option => option.value)));
        """
    ) == ["", "8"]


def test_seedless_mac_chrome_editor_saves_compatible_manual_cpu_without_roll():
    from chrome_engine.persona import MACOS_CPU_COUNTS_BY_SLOT, SEED_MAX, macos_cpu_counts_for_seed

    controls = PANEL.split("    const existingFp = p?.fingerprint;", 1)[1].split(
        "    const webrtcSelect", 1
    )[0]
    payload = PANEL.split('const isChrome = engine.value === "chrome";')[-1].split(
        "const rawBackup"
    )[0]
    expression = re.search(r"initial_fingerprint: (.*),", PANEL)[1]
    seed = 100_000_000
    counts = macos_cpu_counts_for_seed(seed)
    result = run_js(
        f"""
        const panelHardware = {{macCpuCounts: {json.dumps(MACOS_CPU_COUNTS_BY_SLOT)}, seedMax: {SEED_MAX}}};
        const p = {{fingerprint: null}}, existingFp = p.fingerprint;
        const engine = {{value: 'chrome'}}, os = {{value: 'macos'}}, isChrome = true;
        Math.random = () => 0;
        function h(tag, attrs, children = []) {{
          return {{...attrs, children, replaceChildren(...items) {{this.children = items;}}}};
        }}
        {controls}
        let hwChanged = false;
        function prepare() {{
          {payload}
          return {expression};
        }}
        const unchanged = prepare();
        const choices = coresSelect.children.map(option => option.value);
        coresSelect.value = choices[1];
        coresOverridden = Boolean(coresSelect.value);
        hwChanged = true;
        console.log(JSON.stringify([choices, unchanged, prepare()]));
        """
    )
    assert result == [
        ["", *map(str, counts)],
        None,
        {"platform": "macos", "seed": seed, "hardware_concurrency": counts[0]},
    ]


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
