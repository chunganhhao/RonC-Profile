"""Offline trust-boundary and workflow checks; not a Chromium compile claim."""

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "native/chromium-148"
spec = importlib.util.spec_from_file_location("native_prepare", KIT / "prepare.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


@pytest.fixture
def scaffold(tmp_path, monkeypatch):
    (tmp_path / "ungoogled-chromium").mkdir()
    patches = tmp_path / "ungoogled-chromium/patches"
    patches.mkdir()
    (patches / "series").write_text("extra/fingerprint/021-device-memory-cap.patch\n")
    (tmp_path / "downloads-arm64.ini").write_text("[rust]\nversion = 2026-02-27\nurl = pinned\n")
    monkeypatch.setattr(native, "verify_checkout", Mock())
    monkeypatch.setattr(native.subprocess, "run", Mock())
    return tmp_path


def test_prepare_applies_after_revision_checks_and_records_incomplete_acceptance(scaffold):
    native.prepare(scaffold)
    assert native.verify_checkout.call_args_list[0].args == (scaffold, native.PLATFORM_REV)
    assert native.verify_checkout.call_args_list[1].args == (
        scaffold / "ungoogled-chromium",
        native.FINGERPRINT_REV,
    )
    commands = [call.args[0] for call in native.subprocess.run.call_args_list]
    assert commands[0][-2:] == ["--check", str(native.PATCH.resolve())]
    assert commands[1][-2:] == ["apply", str(native.PATCH.resolve())]
    assert f"sha256 = {native.RUST_SHA256}" in (scaffold / "downloads-arm64.ini").read_text()
    proof = json.loads((scaffold / "source-provenance.json").read_text())
    assert proof["acceptance"] == "EXPERIMENTAL_SOURCE_ONLY"
    assert proof["native_realm_patch_sha256"] == native.NATIVE_PATCH_SHA256
    assert (
        (scaffold / "ungoogled-chromium/patches/series")
        .read_text()
        .endswith(native.NATIVE_SERIES_ENTRY + "\n")
    )
    assert (
        scaffold / "ungoogled-chromium/patches" / native.NATIVE_SERIES_ENTRY
    ).read_bytes() == native.NATIVE_PATCH.read_bytes()
    assert len(proof["open_findings"]) == 2
    with pytest.raises(ValueError, match="fresh dedicated checkout"):
        native.prepare(scaffold)


@pytest.mark.parametrize("entry", ["build", "source-provenance.json"])
def test_refuse_existing_build_before_mutation(scaffold, entry):
    (scaffold / entry).touch()
    with pytest.raises(ValueError, match="fresh dedicated checkout"):
        native.prepare(scaffold)
    native.verify_checkout.assert_not_called()
    native.subprocess.run.assert_not_called()


def test_wrong_rust_input_refused_before_patch(scaffold):
    (scaffold / "downloads-arm64.ini").write_text("[rust]\nversion = unknown\n")
    with pytest.raises(ValueError, match="Rust toolchain"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_modified_overlay_refused(scaffold, tmp_path, monkeypatch):
    bad = tmp_path / "bad.patch"
    bad.write_text("not the reviewed fix")
    monkeypatch.setattr(native, "PATCH", bad)
    with pytest.raises(ValueError, match="checksum mismatch"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_patch_failure_never_writes_ready_marker(scaffold):
    native.subprocess.run.side_effect = subprocess.CalledProcessError(1, "git apply --check")
    before = (scaffold / "downloads-arm64.ini").read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        native.prepare(scaffold)
    assert not (scaffold / "source-provenance.json").exists()
    assert (scaffold / "downloads-arm64.ini").read_bytes() == before


def test_wrong_native_order_refused_before_mutation(scaffold):
    (scaffold / "ungoogled-chromium/patches/series").write_text("unexpected.patch\n")
    with pytest.raises(ValueError, match="patch-series order"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_modified_native_overlay_refused(scaffold, monkeypatch):
    bad = scaffold / "bad-native.patch"
    bad.write_text("unreviewed native changes")
    monkeypatch.setattr(native, "NATIVE_PATCH", bad)
    with pytest.raises(ValueError, match="Native realm patch checksum"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_checkout_revision_and_dirty_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(native.subprocess, "check_output", Mock(return_value="wrong\n"))
    run = Mock()
    monkeypatch.setattr(native.subprocess, "run", run)
    with pytest.raises(ValueError, match="Unexpected source revision"):
        native.verify_checkout(tmp_path, native.FINGERPRINT_REV)
    run.assert_not_called()
    native.subprocess.check_output.return_value = native.FINGERPRINT_REV + "\n"
    run.side_effect = subprocess.CalledProcessError(1, "git diff")
    with pytest.raises(subprocess.CalledProcessError):
        native.verify_checkout(tmp_path, native.FINGERPRINT_REV)


@pytest.mark.parametrize(
    ("system", "machine", "disk", "ram", "error"),
    [
        ("Linux", "aarch64", 300, 32, "macOS arm64"),
        ("Darwin", "x86_64", 300, 32, "macOS arm64"),
        ("Darwin", "arm64", 14, 32, "90 GiB"),
        ("Darwin", "arm64", 300, 7, "14 GiB"),
        ("Darwin", "arm64", 300, 13, "14 GiB"),
    ],
)
def test_resource_fail_closed(system, machine, disk, ram, error):
    with pytest.raises(ValueError, match=error):
        native.check_resources(system, machine, disk * 1024**3, ram * 1024**3)


def test_resource_budget_passes():
    native.check_resources("Darwin", "arm64", 90 * 1024**3, 14 * 1024**3)


def test_preflight_rejects_non_mac_before_xcode_or_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(native.platform, "system", lambda: "Linux")
    output = Mock()
    monkeypatch.setattr(native.subprocess, "check_output", output)
    with pytest.raises(ValueError, match="macOS arm64"):
        native.preflight(tmp_path)
    output.assert_not_called()


def test_inspect_only_reports_capacity_without_installs_or_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(native.platform, "system", lambda: "Linux")
    run = Mock()
    monkeypatch.setattr(native.subprocess, "run", run)
    report = native.inspect_runner(tmp_path)
    assert report["free_bytes"] > 0 and report["system"] == "Linux"
    run.assert_not_called()


def test_manual_workflow_contract():
    workflow = (ROOT / ".github/workflows/native-chromium.yml").read_text()
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow and "  push:" not in workflow
    assert "contents: read" in workflow and "write" not in workflow
    assert native.PLATFORM_REV in workflow and native.FINGERPRINT_REV in workflow
    assert "submodules: false" in workflow
    assert "runs-on: macos-26" in workflow
    assert "default: inspect" in workflow
    assert "if: ${{ inputs.phase == 'build' }}" in workflow
    assert "self-hosted" not in workflow and "xlarge" not in workflow
    assert workflow.index("--preflight") < workflow.index("repository: ungoogled")
    assert workflow.index('prepare.py "$GITHUB_WORKSPACE/.native-build/platform"') < workflow.index(
        "build.sh"
    )
    assert "EXPERIMENTAL-chromium" in workflow
    build = (KIT / "build.sh").read_text()
    assert "--preflight" in build and 'test ! -e "$root/build"' in build
    assert build.index('patches.py" apply') < build.index("gn gen") < build.index("ninja -j")
    assert "ninja -j 3 -C out/Default chrome chromedriver" in build
    assert build.index("ditto -c -k") < build.index("shasum -a 256")
    assert "sign_and_package_app.sh" not in build
    assert "notarytool" not in build and "rm -rf" not in build
