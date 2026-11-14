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
    assert proof["macos_antidetect_patch_sha256"] == native.MACOS_ANTIDETECT_PATCH_SHA256
    assert (
        (scaffold / "ungoogled-chromium/patches/series")
        .read_text()
        .endswith(native.NATIVE_SERIES_ENTRY + "\n" + native.MACOS_ANTIDETECT_SERIES_ENTRY + "\n")
    )
    assert (
        scaffold / "ungoogled-chromium/patches" / native.NATIVE_SERIES_ENTRY
    ).read_bytes() == native.NATIVE_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.MACOS_ANTIDETECT_SERIES_ENTRY
    ).read_bytes() == native.MACOS_ANTIDETECT_PATCH.read_bytes()
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
        ("Darwin", "arm64", 300, 4, "7 GiB"),
        ("Darwin", "arm64", 300, 6, "7 GiB"),
    ],
)
def test_resource_fail_closed(system, machine, disk, ram, error):
    with pytest.raises(ValueError, match=error):
        native.check_resources(system, machine, disk * 1024**3, ram * 1024**3)


@pytest.mark.parametrize("ram", [7, 14])
def test_resource_budget_passes(ram):
    native.check_resources("Darwin", "arm64", 90 * 1024**3, ram * 1024**3)


def test_resource_error_reports_actual_capacity():
    with pytest.raises(ValueError, match="got 89.0 GiB free and 6.0 GiB RAM"):
        native.check_resources("Darwin", "arm64", 89 * 1024**3, 6 * 1024**3)


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
    workflow += (ROOT / ".github/workflows/native-chromium-chunk.yml").read_text()
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
    assert (
        build.index('patches.py" apply')
        < build.index("gn gen")
        < build.index('checkpoint.py" mark')
    )
    assert 'checkpoint.py" run' in build
    assert 'checkpoint.py" validate' in build
    assert '"$fork/flags.gn" "$root/flags.macos.gn" "$kit/ci-build.gn"' in build
    assert ".native-build/platform/build/src/out/Default/args.gn" in workflow
    assert build.index("ditto -c -k") < build.index("shasum -a 256")
    assert "sign_and_package_app.sh" not in build
    assert "notarytool" not in build and "rm -rf" not in build


def test_low_memory_flags_and_upstream_override_order(tmp_path):
    # Execute the actual composition command with conflicting upstream defaults.
    build = (KIT / "build.sh").read_text()
    command = next(
        line for line in build.splitlines() if line.lstrip().startswith('cat "$fork/flags.gn"')
    )
    fork = tmp_path / "fork"
    fork.mkdir()
    (fork / "flags.gn").write_text("chrome_pgo_phase = 0\n")
    (tmp_path / "flags.macos.gn").write_text(
        "is_official_build = true\nis_debug = false\nsymbol_level = 1\n"
    )
    (tmp_path / "out/Default").mkdir(parents=True)
    subprocess.run(
        ["bash", "-c", command],
        env={"fork": str(fork), "root": str(tmp_path), "src": str(tmp_path), "kit": str(KIT)},
        check=True,
    )
    flags = {}
    for line in (tmp_path / "out/Default/args.gn").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            key, value = line.split("=", 1)
            flags[key.strip()] = value.strip()
    assert flags == {
        "chrome_pgo_phase": "0",
        "is_official_build": "true",
        "is_debug": "false",
        "target_cpu": '"arm64"',
        "symbol_level": "0",
        "enable_dsyms": "false",
        "use_thin_lto": "false",
        "concurrent_links": "1",
    }


def test_modified_macos_antidetect_overlay_refused(scaffold, monkeypatch):
    bad = scaffold / "bad-macos.patch"
    bad.write_text("unreviewed antidetect changes")
    monkeypatch.setattr(native, "MACOS_ANTIDETECT_PATCH", bad)
    with pytest.raises(ValueError, match="checksum mismatch"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_apply_incremental_patches_idempotent_with_real_patch(tmp_path, monkeypatch):
    import hashlib

    src = tmp_path / "src"
    src.mkdir()
    target = src / "sample.txt"
    target.write_text("line1\nold\nline3\n")
    patch_file = tmp_path / "mini.patch"
    patch_file.write_text(
        "--- a/sample.txt\n+++ b/sample.txt\n@@ -1,3 +1,3 @@\n line1\n-old\n+new\n line3\n"
    )
    digest = hashlib.sha256(patch_file.read_bytes()).hexdigest()
    monkeypatch.setattr(native, "MACOS_ANTIDETECT_PATCH", patch_file)
    monkeypatch.setattr(native, "MACOS_ANTIDETECT_PATCH_SHA256", digest)

    assert native.apply_incremental_patches(src) is True
    assert target.read_text() == "line1\nnew\nline3\n"
    # Second invocation detects already-applied patch cleanly and makes no edits.
    assert native.apply_incremental_patches(src) is False
    assert target.read_text() == "line1\nnew\nline3\n"


def test_verify_matrix_targets_macos_only_and_covers_023_gates():
    verify_src = (KIT / "verify.py").read_text()
    assert '("macos", 2, "apple", "metal-3")' in verify_src
    assert '("macos", 3, "apple", "metal-3")' in verify_src
    assert '("windows",' not in verify_src
    for gate in (
        "textMetrics",
        "leakedFakeShadow",
        "subRectDiff",
        "blobMismatch",
        "webglOob",
        "PACK_ALIGNMENT",
        "gl.NO_ERROR",
        "htmlTextMetrics",
    ):
        assert gate in verify_src
    assert (
        'std::string platform = "macos";'
        in (KIT / "patches/023-macos-antidetect-complete.patch").read_text()
    )
    build_src = (KIT / "build.sh").read_text()
    assert "state=verify_failed" in build_src and "2>&1 | head -n 60" in build_src
