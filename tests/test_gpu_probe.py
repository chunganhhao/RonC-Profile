import hashlib

import pytest

from src.services.browser import gpu_probe


@pytest.fixture
def probe(tmp_path, monkeypatch):
    binary = tmp_path / "browser"
    binary.write_bytes(b"synthetic browser")
    monkeypatch.setattr(
        gpu_probe, "_TESTED_BINARY_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    calls = []

    def original(executable, headless, env, prefs):
        calls.append((executable, headless, env, prefs))
        return gpu_probe._TESTED_GPU

    monkeypatch.setattr(gpu_probe, "host_gpu", original)
    return binary, calls


def test_only_probe_headless_and_parameters_preserved(probe):
    binary, calls = probe
    env, prefs = {"DISPLAY": "fixture"}, {"webgl.force-enabled": True}
    assert gpu_probe._probe(str(binary), False, env, prefs) == gpu_probe._TESTED_GPU
    assert calls == [(str(binary), True, env, prefs)]
    assert calls[0][2] is env and calls[0][3] is prefs


@pytest.mark.parametrize("case", ["already_headless", "unknown_binary", "custom_gpu_prefs"])
def test_unmeasured_cases_use_original_mode(probe, case):
    binary, calls = probe
    headless = case == "already_headless"
    prefs = {"gfx.webrender.all": True} if case == "custom_gpu_prefs" else {}
    if case == "unknown_binary":
        binary.write_bytes(b"different core")
    gpu_probe._probe(str(binary), headless, {}, prefs)
    assert [c[1] for c in calls] == [headless]


def test_unexpected_probe_gpu_falls_back_without_fabrication(probe, monkeypatch):
    binary, _ = probe
    calls = []

    def original(executable, headless, env, prefs):
        calls.append(headless)
        return ("software", "fixture") if headless else ("real", "headed")

    monkeypatch.setattr(gpu_probe, "host_gpu", original)
    assert gpu_probe._probe(str(binary), False, {}, {}) == ("real", "headed")
    assert calls == [True, False]


def test_probe_failure_propagates(probe, monkeypatch):
    binary, _ = probe

    def fail(*_):
        raise TimeoutError("probe failed")

    monkeypatch.setattr(gpu_probe, "host_gpu", fail)
    with pytest.raises(TimeoutError, match="probe failed"):
        gpu_probe._probe(str(binary), False, {}, {})


@pytest.mark.parametrize("unsupported", [None, "os", "library", "cpu", "foreign_hook"])
def test_install_is_scoped_and_idempotent(monkeypatch, unsupported):
    original = gpu_probe.host_gpu
    monkeypatch.setattr(gpu_probe.sys, "platform", "linux" if unsupported == "os" else "darwin")
    monkeypatch.setattr(gpu_probe.platform, "mac_ver", lambda: ("15.8", (), ""))
    monkeypatch.setattr(
        gpu_probe.importlib.metadata,
        "version",
        lambda name: (
            "bad"
            if unsupported == "library"
            else {"camoufox": "0.5.9", "playwright": "1.62.0"}[name]
        ),
    )
    monkeypatch.setattr(
        gpu_probe.subprocess,
        "check_output",
        lambda *a, **k: b"other" if unsupported == "cpu" else b"Apple M4\n",
    )
    before = (lambda *_: None) if unsupported == "foreign_hook" else original
    monkeypatch.setattr(gpu_probe.utils, "host_gpu", before)
    assert gpu_probe.install_headless_gpu_probe() is (unsupported is None)
    if unsupported is None:
        assert gpu_probe.utils.host_gpu is gpu_probe._probe
        assert gpu_probe.install_headless_gpu_probe()
    else:
        assert gpu_probe.utils.host_gpu is before
