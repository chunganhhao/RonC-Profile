"""Offline trust-boundary and workflow checks; not a Chromium compile claim."""

import importlib.util
import json
import subprocess
import sys
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
    assert proof["webgl_readback_patch_sha256"] == native.READBACK_PARITY_PATCH_SHA256
    assert proof["audio_clientrects_patch_sha256"] == native.AUDIO_CLIENTRECTS_PATCH_SHA256
    assert proof["apple_silicon_coherence_patch_sha256"] == native.APPLE_SILICON_PATCH_SHA256
    assert proof["locale_speech_font_patch_sha256"] == native.LOCALE_SPEECH_FONT_PATCH_SHA256
    assert proof["pixelscan_coherence_patch_sha256"] == native.PIXELSCAN_COHERENCE_PATCH_SHA256
    assert (
        proof["macos_renderer_locale_pak_patch_sha256"]
        == native.MACOS_RENDERER_LOCALE_PAK_PATCH_SHA256
    )
    assert (
        (scaffold / "ungoogled-chromium/patches/series")
        .read_text()
        .endswith(
            native.NATIVE_SERIES_ENTRY
            + "\n"
            + native.MACOS_ANTIDETECT_SERIES_ENTRY
            + "\n"
            + native.READBACK_PARITY_SERIES_ENTRY
            + "\n"
            + native.AUDIO_CLIENTRECTS_SERIES_ENTRY
            + "\n"
            + native.APPLE_SILICON_SERIES_ENTRY
            + "\n"
            + native.LOCALE_SPEECH_FONT_SERIES_ENTRY
            + "\n"
            + native.PIXELSCAN_COHERENCE_SERIES_ENTRY
            + "\n"
            + native.MACOS_RENDERER_LOCALE_PAK_SERIES_ENTRY
            + "\n"
        )
    )
    assert (
        scaffold / "ungoogled-chromium/patches" / native.NATIVE_SERIES_ENTRY
    ).read_bytes() == native.NATIVE_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.MACOS_ANTIDETECT_SERIES_ENTRY
    ).read_bytes() == native.MACOS_ANTIDETECT_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.READBACK_PARITY_SERIES_ENTRY
    ).read_bytes() == native.READBACK_PARITY_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.AUDIO_CLIENTRECTS_SERIES_ENTRY
    ).read_bytes() == native.AUDIO_CLIENTRECTS_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.APPLE_SILICON_SERIES_ENTRY
    ).read_bytes() == native.APPLE_SILICON_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.LOCALE_SPEECH_FONT_SERIES_ENTRY
    ).read_bytes() == native.LOCALE_SPEECH_FONT_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.PIXELSCAN_COHERENCE_SERIES_ENTRY
    ).read_bytes() == native.PIXELSCAN_COHERENCE_PATCH.read_bytes()
    assert (
        scaffold / "ungoogled-chromium/patches" / native.MACOS_RENDERER_LOCALE_PAK_SERIES_ENTRY
    ).read_bytes() == native.MACOS_RENDERER_LOCALE_PAK_PATCH.read_bytes()
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


def test_modified_readback_parity_overlay_refused(scaffold, monkeypatch):
    bad = scaffold / "bad-readback.patch"
    bad.write_text("unreviewed readback changes")
    monkeypatch.setattr(native, "READBACK_PARITY_PATCH", bad)
    with pytest.raises(ValueError, match="checksum mismatch"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_modified_apple_silicon_coherence_overlay_refused(scaffold, monkeypatch):
    bad = scaffold / "bad-026.patch"
    bad.write_text("unreviewed apple silicon changes")
    monkeypatch.setattr(native, "APPLE_SILICON_PATCH", bad)
    with pytest.raises(ValueError, match="Apple Silicon coherence patch checksum"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_modified_locale_speech_font_overlay_refused(scaffold, monkeypatch):
    bad = scaffold / "bad-027.patch"
    bad.write_text("unreviewed 027 changes")
    monkeypatch.setattr(native, "LOCALE_SPEECH_FONT_PATCH", bad)
    with pytest.raises(ValueError, match="Locale/speech/font coherence patch checksum"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_modified_pixelscan_coherence_overlay_refused(scaffold, monkeypatch):
    bad = scaffold / "bad-028.patch"
    bad.write_text("unreviewed 028 changes")
    monkeypatch.setattr(native, "PIXELSCAN_COHERENCE_PATCH", bad)
    with pytest.raises(ValueError, match="Pixelscan coherence patch checksum"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


def test_modified_macos_renderer_locale_pak_overlay_refused(scaffold, monkeypatch):
    bad = scaffold / "bad-029.patch"
    bad.write_text("unreviewed 029 changes")
    monkeypatch.setattr(native, "MACOS_RENDERER_LOCALE_PAK_PATCH", bad)
    with pytest.raises(ValueError, match="macOS renderer locale pak patch checksum"):
        native.prepare(scaffold)
    native.subprocess.run.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX patch required")
def test_apply_incremental_patches_idempotent_with_real_patches(tmp_path, monkeypatch):
    import hashlib

    if sys.platform.startswith("linux"):
        run = subprocess.run

        def portable_patch(args, **kwargs):
            if args[0] == "/usr/bin/patch":
                args = ["--dry-run" if arg == "-C" else arg for arg in args]
            return run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", portable_patch)

    patch_a = tmp_path / "023.patch"
    patch_a.write_text(
        "--- a/shared.txt\n+++ b/shared.txt\n"
        "@@ -1,5 +1,5 @@\n"
        " head\n"
        "-old023_canvas\n"
        "+new023_canvas\n"
        " mid\n"
        "-old023_gl\n"
        "+new023_gl\n"
        " tail\n"
    )
    patch_b = tmp_path / "024.patch"
    patch_b.write_text(
        "--- a/shared.txt\n+++ b/shared.txt\n@@ -3,3 +3,3 @@\n mid\n-new023_gl\n+new024_gl\n tail\n"
    )
    patch_c = tmp_path / "025.patch"
    patch_c.write_text(
        "--- a/audio.txt\n+++ b/audio.txt\n@@ -1,3 +1,3 @@\n x\n-old025\n+new025\n z\n"
    )
    patch_d = tmp_path / "026.patch"
    patch_d.write_text("--- a/apple.txt\n+++ b/apple.txt\n@@ -1,2 +1,3 @@\n m1\n+new026\n m3\n")
    patch_e = tmp_path / "027.patch"
    patch_e.write_text(
        "--- a/locale.txt\n+++ b/locale.txt\n@@ -1,3 +1,3 @@\n r1\n-old027_loc\n+new027_loc\n r3\n"
        "--- a/font.txt\n+++ b/font.txt\n@@ -1,3 +1,3 @@\n l1\n-old027\n+new027\n l3\n"
    )
    patch_f = tmp_path / "028.patch"
    patch_f.write_text(
        "--- a/shared.txt\n+++ b/shared.txt\n"
        "@@ -1,5 +1,5 @@\n"
        " head\n"
        "-new023_canvas\n"
        "+new028_canvas\n"
        " mid\n"
        "-new024_gl\n"
        "+new028_gl\n"
        " tail\n"
        "--- a/font.txt\n+++ b/font.txt\n"
        "@@ -1,3 +1,3 @@\n l1\n-new027\n+new028_font\n l3\n"
    )
    patch_g = tmp_path / "029.patch"
    patch_g.write_text(
        "--- a/locale.txt\n+++ b/locale.txt\n@@ -1,3 +1,3 @@\n r1\n-new027_loc\n+new029_pak\n r3\n"
    )
    for attr, path in (
        ("MACOS_ANTIDETECT_PATCH", patch_a),
        ("READBACK_PARITY_PATCH", patch_b),
        ("AUDIO_CLIENTRECTS_PATCH", patch_c),
        ("APPLE_SILICON_PATCH", patch_d),
        ("LOCALE_SPEECH_FONT_PATCH", patch_e),
        ("PIXELSCAN_COHERENCE_PATCH", patch_f),
        ("MACOS_RENDERER_LOCALE_PAK_PATCH", patch_g),
    ):
        monkeypatch.setattr(native, attr, path)
        monkeypatch.setattr(native, f"{attr}_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())

    # 1. Post-023 tree (saved_source=None): skips 023, applies 024..029 with 024/028/029 overlaps.
    src_post023 = tmp_path / "src_post023"
    src_post023.mkdir()
    (src_post023 / "shared.txt").write_text("head\nnew023_canvas\nmid\nnew023_gl\ntail\n")
    (src_post023 / "audio.txt").write_text("x\nold025\nz\n")
    (src_post023 / "apple.txt").write_text("m1\nm3\n")
    (src_post023 / "locale.txt").write_text("r1\nold027_loc\nr3\n")
    (src_post023 / "font.txt").write_text("l1\nold027\nl3\n")
    assert native.apply_incremental_patches(src_post023) is True
    assert (src_post023 / "shared.txt").read_text() == "head\nnew028_canvas\nmid\nnew028_gl\ntail\n"
    assert (src_post023 / "audio.txt").read_text() == "x\nnew025\nz\n"
    assert (src_post023 / "apple.txt").read_text() == "m1\nnew026\nm3\n"
    assert (src_post023 / "locale.txt").read_text() == "r1\nnew029_pak\nr3\n"
    assert (src_post023 / "font.txt").read_text() == "l1\nnew028_font\nl3\n"
    # Second invocation detects 029 already applied even though 023/024/027 reverse checks fail.
    assert native.apply_incremental_patches(src_post023) is False

    # 2. Post-027 and post-028 trees work both with and without saved_source.
    for stage, font_text, saved_source in (
        ("post027_no_saved", "l1\nnew027\nl3\n", None),
        (
            "post027_saved",
            "l1\nnew027\nl3\n",
            {
                "macos_antidetect_patch_sha256": "h23",
                "webgl_readback_patch_sha256": "h24",
                "audio_clientrects_patch_sha256": "h25",
                "apple_silicon_coherence_patch_sha256": "h26",
                "locale_speech_font_patch_sha256": "h27",
            },
        ),
        ("post028_no_saved", "l1\nnew028_font\nl3\n", None),
        (
            "post028_saved",
            "l1\nnew028_font\nl3\n",
            {
                "macos_antidetect_patch_sha256": "h23",
                "webgl_readback_patch_sha256": "h24",
                "audio_clientrects_patch_sha256": "h25",
                "apple_silicon_coherence_patch_sha256": "h26",
                "locale_speech_font_patch_sha256": "h27",
                "pixelscan_coherence_patch_sha256": "h28",
            },
        ),
    ):
        src_tree = tmp_path / f"src_{stage}"
        src_tree.mkdir()
        (src_tree / "shared.txt").write_text(
            "head\nnew028_canvas\nmid\nnew028_gl\ntail\n"
            if stage.startswith("post028")
            else "head\nnew023_canvas\nmid\nnew024_gl\ntail\n"
        )
        (src_tree / "audio.txt").write_text("x\nnew025\nz\n")
        (src_tree / "apple.txt").write_text("m1\nnew026\nm3\n")
        (src_tree / "locale.txt").write_text("r1\nnew027_loc\nr3\n")
        (src_tree / "font.txt").write_text(font_text)
        assert native.apply_incremental_patches(src_tree, saved_source=saved_source) is True
        assert (
            src_tree / "shared.txt"
        ).read_text() == "head\nnew028_canvas\nmid\nnew028_gl\ntail\n"
        assert (src_tree / "locale.txt").read_text() == "r1\nnew029_pak\nr3\n"
        assert (src_tree / "font.txt").read_text() == "l1\nnew028_font\nl3\n"
        assert native.apply_incremental_patches(src_tree, saved_source=saved_source) is False

    # 3. Corrupted 023 fails closed before applying pending 028/029.
    bad_src = tmp_path / "src_bad028"
    bad_src.mkdir()
    (bad_src / "shared.txt").write_text("head\ncorrupt\nmid\nnew024_gl\ntail\n")
    (bad_src / "audio.txt").write_text("x\nnew025\nz\n")
    (bad_src / "apple.txt").write_text("m1\nnew026\nm3\n")
    (bad_src / "locale.txt").write_text("r1\nnew027_loc\nr3\n")
    (bad_src / "font.txt").write_text("l1\nnew027\nl3\n")
    with pytest.raises(RuntimeError, match="Incremental patch 023.*shared.txt"):
        native.apply_incremental_patches(bad_src)

    (src_post023 / "audio.txt").write_text("x\nold025\nz\n")
    with pytest.raises(RuntimeError, match="Incremental patch 025.*audio.txt"):
        native.apply_incremental_patches(src_post023)
    with pytest.raises(RuntimeError, match="Incremental patch 025.*audio.txt"):
        native.apply_incremental_patches(
            src_post023,
            saved_source={
                "macos_antidetect_patch_sha256": "h23",
                "webgl_readback_patch_sha256": "h24",
            },
        )


def test_verify_matrix_targets_macos_only_and_covers_023_gates():
    verify_src = (KIT / "verify.py").read_text()
    assert '("macos", 2, "apple", "metal-3")' in verify_src
    assert '("macos", 3, "apple", "metal-3")' in verify_src
    assert '("macos", 6, "apple", "metal-3")' in verify_src
    assert '("windows",' not in verify_src
    for gate in (
        "textMetrics",
        "leakedFakeShadow",
        "subRectDiff",
        "blobMismatch",
        "webglOob",
        "webglParityMismatch",
        "webglExtendedParityMismatch",
        "webgl5551Digest",
        "webglFloatDigest",
        "webglHalfFloatDigest",
        "webglDigest",
        "webglRedBoxDigest",
        "flatMonochromeChanged",
        "coreFontMissingCount",
        "audioSampleRate",
        "audioDigest",
        "audioSilentMax",
        "clientRectMismatch",
        "bundle_sha256",
        "PIXEL_PACK_BUFFER",
        "PACK_ALIGNMENT",
        "gl.NO_ERROR",
        "htmlTextMetrics",
        "noopenerPopup",
        "sameOriginCrossNavPopup",
        "sameOriginClosePopup",
    ):
        assert gate in verify_src
    assert (
        'std::string platform = "macos";'
        in (KIT / "patches/023-macos-antidetect-complete.patch").read_text()
    )
    assert "ReadFingerprintPixels" in (KIT / "patches/024-webgl-readback-parity.patch").read_text()
    assert "PerturbAudioSample" in (KIT / "patches/025-audio-clientrects-parity.patch").read_text()
    patch025 = (KIT / "patches/025-audio-clientrects-parity.patch").read_text()
    assert "OffsetNonEmptyClientRect" in patch025
    assert "OffsetNonEmptyClientRects" in patch025
    assert "OffsetRangeClientQuads" in patch025
    assert "OffsetRangeBoundingRect" in patch025
    assert "rMultiSpan" in verify_src
    build_src = (KIT / "build.sh").read_text()
    assert "state=verify_failed" in build_src and "2>&1 | head -n 60" in build_src


def test_verify_launches_apple_silicon_cases_inside_live_playwright_context(tmp_path, monkeypatch):
    import asyncio
    import types

    vspec = importlib.util.spec_from_file_location("native_verify", KIT / "verify.py")
    verify_mod = importlib.util.module_from_spec(vspec)
    vspec.loader.exec_module(verify_mod)

    exe = tmp_path / "Chromium"
    exe.write_bytes(b"fake-chromium")
    out = tmp_path / "report.json"

    launches: list[tuple[bool, list[str]]] = []
    launch_kwargs: list[dict] = []
    persistent_launches: list[dict] = []
    context_kwargs: list[dict] = []
    close_popup_override: dict | None = None
    collect_override: dict | None = None
    font_probe_override: dict | None = None
    inventory_override: list[str] | None = None
    bcp47_override: dict[str, dict] | None = None

    class FakePage:
        async def goto(self, url: str) -> None:
            self._url = url

        async def wait_for_load_state(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def evaluate(self, script: str, *args):
            if "window.open(" in script or "setTimeout(r, 50)" in script:
                return None
            if "window.opener && window.opener.location.origin" in script:
                return True
            if "window.__firstInline" in script:
                payload = {
                    "first": {
                        "lang": "de-DE",
                        "langs": ["de-DE", "de", "en-US", "en"],
                        "dtf": "de-DE",
                        "nf": "de-DE",
                        "num": "123.456,789",
                    },
                    "seen": ["de-DE"],
                }
                if args:
                    payload["hadSameOriginOpener"] = args[0]
                if "openerClosed" in script:
                    payload["openerClosed"] = True
                    if close_popup_override:
                        payload.update(close_popup_override)
                return payload
            if "queryLocalFonts" in script:
                if "--disable-spoofing=font" in launches[-1][1]:
                    return (
                        inventory_override
                        if inventory_override is not None
                        else ["Arial", "Helvetica", "Palatino", "Not protected"]
                    )
                seed_arg = next(a for a in launches[-1][1] if a.startswith("--fingerprint="))
                is_3003 = seed_arg.endswith("=3003")
                realm = {
                    "dtf": "de-DE",
                    "nf": "de-DE",
                    "num": "123.456,789",
                    "langs": ["de-DE", "de", "en-US", "en"],
                }
                base_fonts = [f"Font{i}" for i in range(60)] + [
                    "Al Bayan",
                    "Arial",
                    "Avenir Next",
                    "Euphemia UCAS",
                    "Helvetica",
                    "Kohinoor Gujarati",
                    "Menlo",
                    "Monaco",
                    "Mukta Mahee",
                    "Noto Sans Kannada",
                    "Noto Sans Myanmar",
                    "Noto Sans Oriya",
                    "Noto Serif Myanmar",
                    "Optima",
                    "Palatino",
                    "PingFang HK",
                    "PingFang MO",
                    "PingFang SC",
                    "Zapfino",
                ]
                if not is_3003:
                    base_fonts.append("Party LET")
                res = {
                    "window": dict(realm),
                    "worker": dict(realm),
                    "shared": dict(realm),
                    "service": dict(realm),
                    "googleVoices": [],
                    "localVoiceCount": 42,
                    "localFontFamilies": base_fonts,
                    "coreFontMissingCount": 0,
                    "hiddenSampleCount": 1 if is_3003 else 0,
                    "caseRenderLeaks": 0,
                }
                if callable(font_probe_override):
                    font_probe_override(is_3003, res)
                elif font_probe_override:
                    res.update(font_probe_override)
                return res
            if "explicitNf" in script:
                req_tag = args[0]
                defaults = {
                    "zh-Hans-CN": ("zh-Hans-CN", "zh-Hans-CN", ["zh-Hans-CN", "zh", "en-US", "en"]),
                    "es-419": ("es-MX", "es-419", ["es-419", "es", "en-US", "en"]),
                    "en-001": ("en-001", "en-001", ["en-001", "en"]),
                }
                exp_def, exp_exp, langs = defaults[req_tag]
                payload = {
                    "window": {
                        "dtf": exp_def,
                        "nf": exp_def,
                        "explicitDtf": exp_exp,
                        "explicitNf": exp_exp,
                        "langs": list(langs),
                    },
                    "worker": {
                        "dtf": exp_def,
                        "nf": exp_def,
                        "langs": list(langs),
                    },
                }
                if bcp47_override and req_tag in bcp47_override:
                    payload.update(bcp47_override[req_tag])
                return payload
            if args or "globalThis.sample=" in script or "globalThis.rAll=" in script:
                return None
            if "WEBGL_debug_renderer_info" in script:
                seed_arg = next(a for a in launches[-1][1] if a.startswith("--fingerprint="))
                is_m3 = seed_arg.endswith("=3")
                return {
                    "cpu": 8 if is_m3 else 12,
                    "memory": 8,
                    "dpr": 2,
                    "outerHeight": 700,
                    "innerHeight": 640,
                    "screen": {
                        "w": 1512 if is_m3 else 2560,
                        "h": 982 if is_m3 else 1440,
                        "availTop": 38 if is_m3 else 25,
                        "availHeight": 874 if is_m3 else 1345,
                        "colorDepth": 30,
                        "isExtended": False,
                    },
                    "media": {
                        "p3": True,
                        "srgb": True,
                        "color10": True,
                        "dark": is_m3,
                        "light": not is_m3,
                        "devW": True,
                        "devH": True,
                    },
                    "deviceKinds": ["audioinput", "audiooutput"],
                    "pre": {
                        "gl1DrawBufVal": None,
                        "gl1DrawBufErr": 1280,
                        "gl1AnisoVal": None,
                        "gl1AnisoErr": 1280,
                        "gl2AnisoVal": None,
                        "gl2AnisoErr": 1280,
                        "gl2DrawBufVal": 8,
                    },
                    "post": {
                        "hasDraw": True,
                        "hasAniso1": True,
                        "hasAniso2": True,
                        "gl1DrawBufVal": 8,
                        "gl1AnisoVal": 16,
                        "gl2AnisoVal": 16,
                        "renderer": (
                            "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)"
                            if is_m3
                            else (
                                "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Max, "
                                "Unspecified Version)"
                            )
                        ),
                        "vendor": "Google Inc. (Apple)",
                    },
                }
            if getattr(self, "_url", "").endswith("/strict"):
                return {
                    "gpu": {
                        "vendor": "apple",
                        "architecture": "metal-3",
                        "device": "",
                        "description": "",
                    },
                    "cpu": 20,
                    "memory": 8,
                    "stackReads": 0,
                    "globals": [],
                    "solidChanged": 0,
                    "leakedFakeShadow": False,
                    "textMetrics": [110.0 + len(launches), 0.1, 109.0],
                }
            seed_arg = next(a for a in launches[-1][1] if a.startswith("--fingerprint="))
            s = int(seed_arg.split("=")[1])
            realm = {
                "gpu": {
                    "vendor": "apple",
                    "architecture": "metal-3",
                    "device": "",
                    "description": "",
                },
                "cpu": 20,
                "memory": 8,
                "stackReads": 0,
                "globals": [],
                "solidChanged": 0,
                "leakedFakeShadow": False,
                "textMetrics": [110.0 + s, 0.1, 109.0],
            }
            out_collect = {
                **{
                    k: dict(realm)
                    for k in (
                        "window",
                        "classic",
                        "module",
                        "shared",
                        "blob",
                        "iframeOwnedBlob",
                        "service",
                    )
                },
                "borrowed": {"vendor": "apple", "architecture": "metal-3"},
                "solidChanged": 0,
                "htmlTextMetrics": [110.0 + s, 0.1, 109.0],
                "subRectDiff": 0,
                "blobMismatch": 0,
                "webglOob": 0,
                "webglDigest": f"{s:064x}",
                "webglParityMismatch": 0,
                "webglExtendedParityMismatch": 0,
                "webgl5551Digest": f"{(s + 10):064x}",
                "webglFloatDigest": f"{(s + 20):064x}",
                "webglHalfFloatDigest": f"{(s + 30):064x}",
                "audioSampleRate": 44100,
                "audioDigest": f"{(s + 40):064x}",
                "audioSilentMax": 0,
                "clientRectMismatch": 0,
                "webglRedBoxDigest": (
                    "bf9da7959d914298f9ce9e41a480fd66f76fac5c6f5e0a9b5a99b18cfc6fd997"
                ),
                "flatMonochromeChanged": 0,
            }
            if collect_override:
                out_collect.update(collect_override)
            return out_collect

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]

        async def route(self, *_):
            pass

        async def grant_permissions(self, *_a, **_kw):
            pass

        async def new_page(self):
            return FakePage()

        def expect_page(self):
            class _ExpectPage:
                async def __aenter__(self):
                    async def _page():
                        return FakePage()

                    return types.SimpleNamespace(value=_page())

                async def __aexit__(self, *_):
                    return False

            return _ExpectPage()

        async def close(self):
            pass

    class FakeBrowser:
        version = "148.0.7778.215"

        async def new_context(self, **kwargs):
            context_kwargs.append(dict(kwargs))
            return FakeContext()

        async def close(self):
            pass

    class FakeChromium:
        def __init__(self, state):
            self._state = state

        async def launch(self, **kwargs):
            if not self._state["open"]:
                raise RuntimeError(
                    "BrowserType.launch: Target page, context or browser has been closed"
                )
            launches.append((self._state["open"], kwargs["args"]))
            launch_kwargs.append(dict(kwargs))
            return FakeBrowser()

        async def launch_persistent_context(self, user_data_dir, **kwargs):
            if not self._state["open"]:
                raise RuntimeError(
                    "BrowserType.launch_persistent_context: Target page, context or browser has been closed"
                )
            assert Path(user_data_dir).is_dir()
            launches.append((self._state["open"], kwargs["args"]))
            persistent_launches.append(dict(kwargs))
            context_kwargs.append({"color_scheme": kwargs.get("color_scheme")})
            return FakeContext()

    class FakePlaywrightCM:
        def __init__(self):
            self.state = {"open": False}
            self.chromium = FakeChromium(self.state)

        async def __aenter__(self):
            self.state["open"] = True
            return self

        async def __aexit__(self, *_):
            self.state["open"] = False

    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        types.SimpleNamespace(async_playwright=lambda: FakePlaywrightCM()),
    )
    monkeypatch.setattr(verify_mod, "_host_macos_speech_voice_count", lambda: 191)

    asyncio.run(verify_mod.verify(exe, out))
    report = json.loads(out.read_text())
    assert report["acceptance"] == "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL"
    assert report["chromium_sandbox"] is True
    inventory_launches = [
        kw for kw in persistent_launches if "--disable-spoofing=font" in kw["args"]
    ]
    assert len(launches) == 10 + len(inventory_launches)
    assert len(launch_kwargs) == 8
    assert all(kw.get("chromium_sandbox") is True for kw in launch_kwargs)
    assert len(persistent_launches) == 2 + len(inventory_launches)
    assert all(kw.get("chromium_sandbox") is True for kw in persistent_launches)
    assert all(kw["no_viewport"] is True for kw in persistent_launches)
    assert all(is_open for is_open, _ in launches)
    assert len(context_kwargs) == 10 + len(inventory_launches)
    assert all(kw == {"color_scheme": "null"} for kw in context_kwargs)
    assert "apple_silicon_m3_laptop" in report["cases"]
    assert "apple_silicon_m2_max_desktop" in report["cases"]
    assert "locale_speech_font_de" in report["cases"]
    assert "locale_029_bcp47" in report["cases"]
    assert report["cases"]["locale_029_bcp47"]["es-419"]["window"]["dtf"] == "es-MX"
    assert report["cases"]["locale_029_bcp47"]["es-419"]["window"]["explicitNf"] == "es-419"
    assert report["cases"]["locale_029_bcp47"]["zh-Hans-CN"]["worker"]["dtf"] == "zh-Hans-CN"
    assert report["cases"]["locale_029_bcp47"]["en-001"]["window"]["dtf"] == "en-001"
    locale_case = report["cases"]["locale_speech_font_de"]
    assert locale_case["noopener_popup"]["first"]["dtf"] == "de-DE"
    assert locale_case["same_origin_cross_nav_popup"]["hadSameOriginOpener"] is True
    assert locale_case["same_origin_cross_nav_popup"]["seen"] == ["de-DE"]
    assert locale_case["same_origin_close_popup"]["hadSameOriginOpener"] is True
    assert locale_case["same_origin_close_popup"]["openerClosed"] is True
    assert locale_case["same_origin_close_popup"]["seen"] == ["de-DE"]
    assert locale_case["core_font_missing_count"] == 0
    assert locale_case["local_voice_count"] == 42
    assert locale_case["local_voice_count_3006"] == 42
    assert locale_case["host_local_voice_count"] == 191
    assert locale_case["speech_voice_gate"] == "enforced"
    for bad_close in ({"seen": ["de-DE", "en-US", "de-DE"]}, {"hadSameOriginOpener": False}):
        close_popup_override = bad_close
        with pytest.raises(ValueError, match="027 popup locale continuity mismatch"):
            asyncio.run(verify_mod.verify(exe, out))
    close_popup_override = None
    collect_override = {"webglRedBoxDigest": "0" * 64}
    with pytest.raises(ValueError, match="028 WebGL solid red box hash mismatch"):
        asyncio.run(verify_mod.verify(exe, out))
    collect_override = {"flatMonochromeChanged": 3}
    with pytest.raises(ValueError, match="028 flat monochrome canvas perturbed"):
        asyncio.run(verify_mod.verify(exe, out))
    collect_override = None

    def _hide_installed_core(_is_3003: bool, res: dict) -> None:
        res["localFontFamilies"].remove("Palatino")

    font_probe_override = _hide_installed_core
    with pytest.raises(
        ValueError, match="028 installed protected macOS core font hidden.*palatino"
    ):
        asyncio.run(verify_mod.verify(exe, out))
    assert len(inventory_launches) == 1
    inventory_override = []
    with pytest.raises(
        ValueError, match="028 unfiltered protected macOS core font inventory empty"
    ):
        asyncio.run(verify_mod.verify(exe, out))
    inventory_override = None

    font_probe_override = {"coreFontMissingCount": 1}
    with pytest.raises(ValueError, match="028 protected macOS core font hidden"):
        asyncio.run(verify_mod.verify(exe, out))

    def _vary_system_font(is_3003: bool, res: dict) -> None:
        if is_3003:
            res["localFontFamilies"].remove("Noto Sans Kannada")

    font_probe_override = _vary_system_font
    with pytest.raises(ValueError, match="028 protected macOS system font varied across seeds"):
        asyncio.run(verify_mod.verify(exe, out))

    def _identical_non_core(_is_3003: bool, res: dict) -> None:
        res["localFontFamilies"] = [f for f in res["localFontFamilies"] if f != "Party LET"]

    font_probe_override = _identical_non_core
    with pytest.raises(
        ValueError,
        match="027 queryLocalFonts did not reflect per-seed supplemental/user FontCache filtering",
    ):
        asyncio.run(verify_mod.verify(exe, out))

    font_probe_override = {"localVoiceCount": 0}
    monkeypatch.setattr(verify_mod, "_host_macos_speech_voice_count", lambda: 191)
    with pytest.raises(ValueError, match="027 missing local system speech voices"):
        asyncio.run(verify_mod.verify(exe, out))

    monkeypatch.setattr(verify_mod, "_host_macos_speech_voice_count", lambda: None)
    with pytest.raises(ValueError, match="027 missing local system speech voices"):
        asyncio.run(verify_mod.verify(exe, out))

    monkeypatch.setattr(verify_mod, "_host_macos_speech_voice_count", lambda: 0)
    asyncio.run(verify_mod.verify(exe, out))
    zero_voice_report = json.loads(out.read_text())["cases"]["locale_speech_font_de"]
    assert zero_voice_report["host_local_voice_count"] == 0
    assert zero_voice_report["local_voice_count"] == 0
    assert zero_voice_report["local_voice_count_3006"] == 0
    assert zero_voice_report["speech_voice_gate"] == "host_unavailable_on_runner"

    font_probe_override = {"localVoiceCount": 0, "googleVoices": ["Google US English"]}
    with pytest.raises(ValueError, match="027 unexpected broken Google Network Speech voices"):
        asyncio.run(verify_mod.verify(exe, out))
    font_probe_override = None

    bcp47_override = {"es-419": {"worker": {"dtf": "en-US", "nf": "en-US", "langs": ["es-419"]}}}
    with pytest.raises(ValueError, match="029 BCP-47 locale mismatch \\(es-419/worker\\)"):
        asyncio.run(verify_mod.verify(exe, out))
    bcp47_override = None


def test_verify_speech_voice_host_probe_and_checkpoint_compat(monkeypatch):
    import types

    verify_path = KIT / "verify.py"
    vspec = importlib.util.spec_from_file_location("native_verify_speech", verify_path)
    assert vspec is not None and vspec.loader is not None
    verify_mod = importlib.util.module_from_spec(vspec)
    vspec.loader.exec_module(verify_mod)

    monkeypatch.setattr(verify_mod.sys, "platform", "linux")
    assert verify_mod._host_macos_speech_voice_count() is None

    monkeypatch.setattr(verify_mod.sys, "platform", "darwin")
    responses: list[object] = [
        types.SimpleNamespace(returncode=0, stdout="191\n"),
        types.SimpleNamespace(returncode=0, stdout="0\n"),
        types.SimpleNamespace(returncode=0, stdout="UNAVAILABLE\n"),
        types.SimpleNamespace(returncode=0, stdout="not-a-number\n"),
        types.SimpleNamespace(returncode=1, stdout="191\n"),
        subprocess.TimeoutExpired(cmd="osascript", timeout=10),
    ]

    def fake_run(*args, **kwargs):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(verify_mod.subprocess, "run", fake_run)
    assert verify_mod._host_macos_speech_voice_count() == 191
    assert verify_mod._host_macos_speech_voice_count() == 0
    assert verify_mod._host_macos_speech_voice_count() is None
    assert verify_mod._host_macos_speech_voice_count() is None
    assert verify_mod._host_macos_speech_voice_count() is None
    assert verify_mod._host_macos_speech_voice_count() is None

    verify_src = verify_path.read_text(encoding="utf-8")
    assert "if (current.length > 0) finish(current);" in verify_src
    assert "setTimeout(() => finish(read()), 3000)" in verify_src
    assert "vs ? vs.count : 0" not in verify_src
    assert "isKindOfClass($.NSArray)" in verify_mod._HOST_SPEECH_VOICE_COUNT_JXA
    assert "!vs.isNil()" in verify_mod._HOST_SPEECH_VOICE_COUNT_JXA
    assert '"UNAVAILABLE"' in verify_mod._HOST_SPEECH_VOICE_COUNT_JXA

    if __import__("sys").platform == "darwin":
        monkeypatch.undo()
        jxa_probe = (
            'ObjC.import("AVFoundation"); ObjC.import("Foundation"); '
            "const evalList = (vs) => "
            '(vs && typeof vs.isNil === "function" && !vs.isNil() '
            '&& typeof vs.isKindOfClass === "function" && vs.isKindOfClass($.NSArray)) '
            '? String(Number(vs.count)) : "UNAVAILABLE"; '
            "JSON.stringify({"
            "emptyArray: evalList($.NSArray.array), "
            "objcNil: evalList($()), "
            "jsNull: evalList(null), "
            "jsUndef: evalList(undefined), "
            'nonArray: evalList($.NSString.stringWithString("x"))'
            "})"
        )
        proc = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", jxa_probe],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        parsed = json.loads(proc.stdout)
        assert parsed == {
            "emptyArray": "0",
            "objcNil": "UNAVAILABLE",
            "jsNull": "UNAVAILABLE",
            "jsUndef": "UNAVAILABLE",
            "nonArray": "UNAVAILABLE",
        }

    cspec = importlib.util.spec_from_file_location(
        "native_checkpoint_verify_compat", KIT / "checkpoint.py"
    )
    assert cspec is not None and cspec.loader is not None
    checkpoint_mod = importlib.util.module_from_spec(cspec)
    cspec.loader.exec_module(checkpoint_mod)

    saved = {
        "format": 1,
        "workspace": "/Users/runner/work/RonC-Profile/RonC-Profile",
        "host": "arm64-macOS",
        "python": "3.12",
        "ninja": "1.12",
        "xcode": "26",
        "sdk": "macOS26",
        "kit": {"ci-build.gn": "gn-sha", "prepare.py": "prep-sha", "verify.py": "old-verify"},
        "source": {"chromium": "148.0.7778.215"},
    }
    current = {
        **saved,
        "kit": {"ci-build.gn": "gn-sha", "prepare.py": "prep-sha", "verify.py": "new-verify"},
    }
    assert checkpoint_mod.compatible_identity(saved, current) is True


def test_verify_026_recheck_exclusive_out_and_blank_api_token(tmp_path, monkeypatch, capsys):
    import hashlib
    import os
    import types

    mod_path = ROOT / "scripts/verify_026_recheck.py"
    v_spec = importlib.util.spec_from_file_location("verify_026_recheck_test", mod_path)
    assert v_spec is not None and v_spec.loader is not None
    mod = importlib.util.module_from_spec(v_spec)
    v_spec.loader.exec_module(mod)

    fake_repo = tmp_path / "repo"
    for rel in mod.TRACKED_FILES:
        target = fake_repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fixture:{rel}\n<!-- version A -->\n", encoding="utf-8")
    real_reg = fake_repo / "profiles.json"
    real_reg.write_text('{"p1": {"name": "p1"}}', encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", fake_repo)
    monkeypatch.setattr(mod, "REAL_PROFILES", real_reg)

    called: list[dict[str, str]] = []
    race_target: Path | None = None
    mutate_rel: str | None = None

    def fake_run(cmd, *, cwd, env, capture_output, text, check):
        called.append(dict(env))
        if race_target is not None and not race_target.exists():
            race_target.write_text("race-sentinel\n", encoding="utf-8")
        checked_sha = None
        if mutate_rel is not None:
            mut_path = fake_repo / mutate_rel
            checked_sha = hashlib.sha256(mut_path.read_bytes()).hexdigest()
            mut_path.write_text(
                mut_path.read_text(encoding="utf-8") + "<!-- version B -->\n", encoding="utf-8"
            )
        (Path(cwd) / "child_report.json").write_text(
            json.dumps({"ok": True, "child_checked_sha": checked_sha}), encoding="utf-8"
        )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    # 1. Existing sentinel at explicit --out is refused before launching child.
    sentinel = tmp_path / "old.json"
    sentinel.write_text("sentinel\n", encoding="utf-8")
    assert mod._resolve_out_path(["verify_026_recheck.py", "--out", str(sentinel)]) == sentinel
    assert mod._run_parent(sentinel) == 2
    assert sentinel.read_text(encoding="utf-8") == "sentinel\n"
    assert called == []

    # Bare positional path without --out is refused as invalid usage.
    with pytest.raises(SystemExit) as bad_cli:
        mod._resolve_out_path(["verify_026_recheck.py", str(sentinel)])
    assert bad_cli.value.code == 2

    # 2. Race creating --out while child runs still refuses overwrite via exclusive "x" open.
    race_target = tmp_path / "race.json"
    assert mod._run_parent(race_target) == 2
    assert race_target.read_text(encoding="utf-8") == "race-sentinel\n"
    assert len(called) == 1
    called.clear()
    race_target = None
    capsys.readouterr()

    # 3. No-option invocation ignores inherited FOXPROFILE_EVIDENCE_OUT & clears FOXPROFILE_API_TOKEN for child.
    monkeypatch.setenv("FOXPROFILE_EVIDENCE_OUT", str(sentinel))
    monkeypatch.setenv("FOXPROFILE_API_TOKEN", "codex-synthetic-token-fixture-only")
    assert mod._resolve_out_path(["verify_026_recheck.py"]) is None
    assert mod._run_parent(None) == 0
    assert sentinel.read_text(encoding="utf-8") == "sentinel\n"
    assert os.environ["FOXPROFILE_API_TOKEN"] == "codex-synthetic-token-fixture-only"
    assert len(called) == 1
    child_env = called[0]
    assert child_env["FOXPROFILE_API_TOKEN"] == ""
    assert "FOXPROFILE_EVIDENCE_OUT" not in child_env
    stdout_text = capsys.readouterr().out
    assert "codex-synthetic-token-fixture-only" not in stdout_text
    assert json.loads(stdout_text)["ok"] is True

    # 4. Fresh explicit --out succeeds cleanly and records verifier SHA + isolated token var.
    html_file = fake_repo / "src/web/index.html"
    sha_a = hashlib.sha256(html_file.read_bytes()).hexdigest()
    fresh = tmp_path / "nested/fresh.json"
    assert mod._resolve_out_path(["verify_026_recheck.py", "--out", str(fresh)]) == fresh
    assert mod._run_parent(fresh) == 0
    fresh_stdout = capsys.readouterr().out
    assert fresh.read_text(encoding="utf-8") == fresh_stdout
    parsed = json.loads(fresh_stdout)
    assert "FOXPROFILE_API_TOKEN" in parsed["isolated_temp_env_vars"]
    assert "scripts/verify_026_recheck.py" in parsed["tracked_file_sha256"]
    assert parsed["tracked_file_sha256"]["src/web/index.html"] == sha_a

    # 5. Concurrent worktree edit (A -> B) during child run fails nonzero without publishing report.
    mutate_rel = "src/web/index.html"
    drift_out = tmp_path / "drift.json"
    assert mod._run_parent(drift_out) == 2
    drift_captured = capsys.readouterr()
    assert drift_captured.out == ""
    assert "src/web/index.html" in drift_captured.err
    assert not drift_out.exists()

    # Paired stable run after edit finishes succeeds with consistent starting digest B.
    mutate_rel = None
    sha_b = hashlib.sha256(html_file.read_bytes()).hexdigest()
    assert sha_b != sha_a
    stable_out = tmp_path / "stable_b.json"
    assert mod._run_parent(stable_out) == 0
    stable_parsed = json.loads(capsys.readouterr().out)
    assert stable_parsed["tracked_file_sha256"]["src/web/index.html"] == sha_b
