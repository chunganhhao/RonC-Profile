"""Process-based work/transfer/resume fixtures, not a Chromium compilation claim."""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

KIT = Path(__file__).resolve().parents[1] / "native/chromium-148"
spec = importlib.util.spec_from_file_location("native_checkpoint", KIT / "checkpoint.py")
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="Native checkpoint jobs pin Python3.12"
)


@pytest.fixture(autouse=True)
def disk_budget(monkeypatch):
    monkeypatch.setattr(
        checkpoint.shutil, "disk_usage", lambda _: SimpleNamespace(free=256 * 1024**3)
    )


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    stamp = 1700000000123456789
    sub = src / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested")
    marker = src / ".ninja_deps"
    marker.write_bytes(b"saved objects\x00deps")
    marker.chmod(0o755)
    os.utime(marker, ns=(stamp, stamp))
    os.utime(sub, ns=(stamp - 111, stamp - 111))
    (src / "relative").symlink_to(".ninja_deps")
    (src / "absolute").symlink_to(marker.resolve())
    os.utime(src / "relative", ns=(stamp - 222, stamp - 222), follow_symlinks=False)
    return src


def test_roundtrip_preserves_outputs_modes_times_and_internal_links(source, tmp_path):
    context = {"source": "pinned", "flags": "exact"}
    output = tmp_path / "checkpoint"
    checkpoint.pack(source, output, context)
    checkpoint.unpack(tmp_path / "restored", output, context)
    restored = tmp_path / "restored/src/.ninja_deps"
    stamp = 1700000000123456789
    assert restored.read_bytes() == (source / ".ninja_deps").read_bytes()
    assert restored.stat().st_mtime_ns == stamp
    assert (restored.parent / "sub").stat().st_mtime_ns == stamp - 111
    assert restored.stat().st_mode & 0o777 == 0o755
    assert (restored.parent / "relative").lstat().st_mtime_ns == stamp - 222
    for name in ("relative", "absolute"):
        link = restored.parent / name
        assert link.is_symlink() and not link.readlink().is_absolute()
        assert link.read_bytes() == restored.read_bytes()


@pytest.mark.parametrize("corrupt", [False, True])
def test_identity_or_checksum_refused_before_extraction(source, tmp_path, corrupt):
    output = tmp_path / "checkpoint"
    checkpoint.pack(source, output, {"revision": "old"})
    if corrupt:
        (output / "source.tar.gz").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="identity or archive checksum"):
        checkpoint.unpack(tmp_path / "build", output, {"revision": "old" if corrupt else "new"})
    assert not (tmp_path / "build").exists()


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside"])
def test_pack_refuses_external_links(source, tmp_path, target):
    (source / "bad").symlink_to(target)
    with pytest.raises(ValueError, match="External checkpoint symlink"):
        checkpoint.pack(source, tmp_path / "checkpoint", {})
    assert not (tmp_path / "checkpoint/manifest.json").exists()


@pytest.mark.parametrize("name", ["../escape", "src/../../escape", "/escape"])
def test_archive_traversal_refused_and_no_partial_restore(tmp_path, name):
    output = tmp_path / "checkpoint"
    output.mkdir()
    archive = output / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    (output / "manifest.json").write_text(
        json.dumps({"identity": {}, "sha256": checkpoint.digest(archive)})
    )
    with pytest.raises((ValueError, tarfile.FilterError)):
        checkpoint.unpack(tmp_path / "build", output, {})
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "build").exists()


def test_restore_refuses_existing_build_and_insufficient_disk(source, tmp_path, monkeypatch):
    output = tmp_path / "checkpoint"
    checkpoint.pack(source, output, {})
    build = tmp_path / "build"
    build.mkdir()
    with pytest.raises(ValueError, match="fresh build"):
        checkpoint.unpack(build, output, {})
    monkeypatch.setattr(checkpoint.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(ValueError, match="Not enough disk"):
        checkpoint.unpack(tmp_path / "new-build", output, {})
    assert not (tmp_path / "new-build").exists()


def test_no_snapshot_inside_source(source):
    with pytest.raises(ValueError, match="outside the source"):
        checkpoint.pack(source, source / "checkpoint", {})


def test_compile_success_and_failure_are_not_partial():
    assert checkpoint.run_bounded([sys.executable, "-c", "pass"], 5) == 0
    assert checkpoint.run_bounded([sys.executable, "-c", "raise SystemExit(7)"], 5) == 7
    with pytest.raises(ValueError, match="four-hour"):
        checkpoint.run_bounded([], 0)


def test_uncooperative_compiler_is_failure_not_checkpoint():
    command = [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(10)",
    ]
    with pytest.raises(RuntimeError, match="no checkpoint is safe"):
        checkpoint.run_bounded(command, 0.5, grace=0.1)


@pytest.mark.parametrize("group_mode", ["inherited", "separate-group", "separate-session"])
def test_surviving_child_prevents_snapshot_even_if_supervisor_exits(group_mode):
    child = "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(10)"
    options = {
        "inherited": "",
        "separate-group": ", process_group=0",
        "separate-session": ", start_new_session=True",
    }[group_mode]
    parent = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130)); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]{options}); time.sleep(10)"
    )
    with pytest.raises(RuntimeError, match="no checkpoint is safe"):
        checkpoint.run_bounded([sys.executable, "-c", parent], 0.5, grace=0.1)


def test_separate_ninja_command_group_must_exit_before_partial(tmp_path):
    stopped = tmp_path / "stopped"
    child = f"import pathlib,signal,sys,time; signal.signal(signal.SIGINT, lambda *_: (pathlib.Path({str(stopped)!r}).touch(), sys.exit(130))); time.sleep(10)"
    parent = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130)); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], process_group=0); time.sleep(10)"
    )
    assert (
        checkpoint.run_bounded([sys.executable, "-c", parent], 0.5, grace=2) == checkpoint.PARTIAL
    )
    assert stopped.exists()


def test_bounded_fixture_work_transfer_and_incremental_resume(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    worker = tmp_path / "compile.py"
    worker.write_text(
        "import pathlib,signal,sys,time\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))\n"
        "root=pathlib.Path(sys.argv[1])\n"
        "for i in range(5):\n"
        " p=root/f'{i}.o'\n"
        " if not p.exists():\n"
        "  p.write_text(f'compiled-{i}')\n"
        "  time.sleep(0.3)\n"
    )
    assert (
        checkpoint.run_bounded([sys.executable, str(worker), str(source)], 0.6)
        == checkpoint.PARTIAL
    )
    before = {p.name: p.stat().st_mtime_ns for p in source.glob("*.o")}
    assert 0 < len(before) < 5
    checkpoint.pack(source, tmp_path / "checkpoint", {"fixture": 1})
    checkpoint.unpack(tmp_path / "restored", tmp_path / "checkpoint", {"fixture": 1})
    restored = tmp_path / "restored/src"
    assert checkpoint.run_bounded([sys.executable, str(worker), str(restored)], 5) == 0
    assert len(list(restored.glob("*.o"))) == 5
    # mtime-ns.tsv preserves exact 64-bit integer nanoseconds without float truncation.
    assert all((restored / name).stat().st_mtime_ns == stamp for name, stamp in before.items())


def test_legacy_ninja_log_mtime_repair_and_migration_compatibility(tmp_path):
    source = tmp_path / "src"
    out = source / "out/Default"
    (out / "obj").mkdir(parents=True)
    (out / "gen").mkdir(parents=True)
    obj = out / "obj/foo.o"
    obj.write_bytes(b"ELF-mock")
    exact_ns = 1700000123456789123
    os.utime(obj, ns=(exact_ns, exact_ns))
    hdr = out / "gen/header.h"
    hdr.write_text("#pragma once\n")
    hdr_ns = 1700000050123456789
    os.utime(hdr, ns=(hdr_ns, hdr_ns))
    (out / ".ninja_log").write_text(
        "# ninja log v7\n"
        f"10\t20\t{exact_ns}\tobj/foo.o\tdeadbeef\n"
        f"5\t15\t{hdr_ns}\tgen/header.h\tcafebabe\n"
        "1000\t1010\t1700009999000000000\tgen/header.h\tcafebabe\n"
    )
    saved_ctx = {
        "format": 1,
        "workspace": "/work/.native-build/platform",
        "host": ["Darwin", "arm64"],
        "python": "3.12.0",
        "ninja": "1.12.1",
        "xcode": "Xcode 16",
        "sdk": "/SDK",
        "source": {
            "platform_revision": "rev1",
            "fingerprint_revision": "rev2",
            "canvas_overlay_sha256": "abc",
            "native_realm_patch_sha256": "def",
            "rust_archive_sha256": "ghi",
            "acceptance": "EXPERIMENTAL_SOURCE_ONLY",
            "open_findings": ["a", "b"],
        },
        "kit": {"ci-build.gn": "gn-sha", "build.sh": "old", "checkpoint.py": "old"},
    }
    ckpt = tmp_path / "legacy-ckpt"
    checkpoint.pack(source, ckpt, saved_ctx)
    # Strip mtime-ns.tsv to simulate Run #9 pre-mtime-manifest checkpoint.
    (ckpt / "mtime-ns.tsv").unlink()
    manifest = json.loads((ckpt / "manifest.json").read_text())
    manifest.pop("mtime_sha256")
    (ckpt / "manifest.json").write_text(json.dumps(manifest))

    new_ctx = json.loads(json.dumps(saved_ctx))
    new_ctx["source"]["macos_antidetect_patch_sha256"] = "new-023"
    new_ctx["source"]["webgl_readback_patch_sha256"] = "new-024"
    new_ctx["source"]["audio_clientrects_patch_sha256"] = "new-025"
    new_ctx["source"]["apple_silicon_coherence_patch_sha256"] = "new-026"
    new_ctx["source"]["locale_speech_font_patch_sha256"] = "new-027"
    new_ctx["source"]["pixelscan_coherence_patch_sha256"] = "new-028"
    new_ctx["kit"]["build.sh"] = "updated"
    new_ctx["kit"]["checkpoint.py"] = "updated"
    new_ctx["kit"]["prepare.py"] = "updated"

    checkpoint.unpack(tmp_path / "restored", ckpt, new_ctx)
    assert (tmp_path / "restored/src/out/Default/obj/foo.o").stat().st_mtime_ns == exact_ns
    assert (tmp_path / "restored/src/out/Default/gen/header.h").stat().st_mtime_ns == hdr_ns

    # Run #11 checkpoint (has 023, lacks 024/025) also migrates cleanly even with updated prepare.py.
    r11_ctx = json.loads(json.dumps(saved_ctx))
    r11_ctx["source"]["macos_antidetect_patch_sha256"] = "new-023"
    r11_ctx["kit"]["prepare.py"] = "r11-prepare"
    r11_ckpt = tmp_path / "r11-ckpt"
    checkpoint.pack(source, r11_ckpt, r11_ctx)
    checkpoint.unpack(tmp_path / "r11-restored", r11_ckpt, new_ctx)

    bad_gn = json.loads(json.dumps(new_ctx))
    bad_gn["kit"]["ci-build.gn"] = "changed"
    with pytest.raises(ValueError, match="mismatch"):
        checkpoint.unpack(tmp_path / "bad-gn", ckpt, bad_gn)

    bad_rev = json.loads(json.dumps(new_ctx))
    bad_rev["source"]["platform_revision"] = "tampered"
    with pytest.raises(ValueError, match="mismatch"):
        checkpoint.unpack(tmp_path / "bad-rev", ckpt, bad_rev)

    # Once 023+024+025 are all present in the saved checkpoint, prepare.py changes are refused.
    post_ctx = json.loads(json.dumps(new_ctx))
    post_ctx["kit"]["prepare.py"] = "v1"
    post_ckpt = tmp_path / "post-ckpt"
    checkpoint.pack(source, post_ckpt, post_ctx)
    bad_prepare = json.loads(json.dumps(post_ctx))
    bad_prepare["kit"]["prepare.py"] = "v2"
    with pytest.raises(ValueError, match="mismatch"):
        checkpoint.unpack(tmp_path / "bad-prep", post_ckpt, bad_prepare)


def test_cli_restore_applies_incremental_patches_for_legacy_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "legacy-src"
    source.mkdir()
    (source / "file.txt").write_text("v1")
    saved_ctx = {
        "format": 1,
        "workspace": str((tmp_path / "root").resolve()),
        "host": ["Darwin", "arm64"],
        "python": "3.12.0",
        "ninja": "1.12.1",
        "xcode": "Xcode 16",
        "sdk": "/SDK",
        "source": {"platform_revision": "rev1", "macos_antidetect_patch_sha256": "old-023"},
        "kit": {"ci-build.gn": "gn-sha", "build.sh": "old", "prepare.py": "old"},
    }
    ckpt = tmp_path / "ckpt"
    checkpoint.pack(source, ckpt, saved_ctx)
    new_ctx = json.loads(json.dumps(saved_ctx))
    new_ctx["source"]["webgl_readback_patch_sha256"] = "new-024"
    new_ctx["kit"]["build.sh"] = "new"
    new_ctx["kit"]["prepare.py"] = "new"
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(checkpoint, "identity", lambda _: new_ctx)
    monkeypatch.setattr(checkpoint, "validate", Mock())
    patch_hook = Mock(return_value=True)
    monkeypatch.setattr(checkpoint, "_apply_incremental_patches", patch_hook)
    monkeypatch.setattr(sys, "argv", ["checkpoint.py", "restore", str(root), str(ckpt)])
    assert checkpoint.main() == 0
    patch_hook.assert_called_once_with(root / "build/src", saved_ctx["source"])
    assert json.loads((root / "native-ready.json").read_text()) == new_ctx

    # Post-025 checkpoint (Run #17/#19: has 023/024/025, lacks 026) must also apply 026 on restore.
    post025_saved = json.loads(json.dumps(saved_ctx))
    post025_saved["source"]["webgl_readback_patch_sha256"] = "old-024"
    post025_saved["source"]["audio_clientrects_patch_sha256"] = "old-025"
    ckpt25 = tmp_path / "ckpt25"
    checkpoint.pack(source, ckpt25, post025_saved)
    post026_ctx = json.loads(json.dumps(post025_saved))
    post026_ctx["source"]["apple_silicon_coherence_patch_sha256"] = "new-026"
    post026_ctx["source"]["locale_speech_font_patch_sha256"] = "new-027"
    post026_ctx["source"]["pixelscan_coherence_patch_sha256"] = "new-028"
    post026_ctx["kit"]["prepare.py"] = "new-026"
    root2 = tmp_path / "root2"
    root2.mkdir()
    monkeypatch.setattr(checkpoint, "identity", lambda _: post026_ctx)
    patch_hook2 = Mock(return_value=True)
    monkeypatch.setattr(checkpoint, "_apply_incremental_patches", patch_hook2)
    monkeypatch.setattr(sys, "argv", ["checkpoint.py", "restore", str(root2), str(ckpt25)])
    assert checkpoint.main() == 0
    patch_hook2.assert_called_once_with(root2 / "build/src", post025_saved["source"])
    assert json.loads((root2 / "native-ready.json").read_text()) == post026_ctx

    # Post-026 checkpoint (has 023..026, lacks 027) must also migrate cleanly and apply 027.
    post026_saved = json.loads(json.dumps(post025_saved))
    post026_saved["source"]["apple_silicon_coherence_patch_sha256"] = "old-026"
    ckpt26 = tmp_path / "ckpt26"
    checkpoint.pack(source, ckpt26, post026_saved)
    post027_ctx = json.loads(json.dumps(post026_saved))
    post027_ctx["source"]["locale_speech_font_patch_sha256"] = "new-027"
    post027_ctx["source"]["pixelscan_coherence_patch_sha256"] = "new-028"
    post027_ctx["kit"]["prepare.py"] = "new-027"
    root3 = tmp_path / "root3"
    root3.mkdir()
    monkeypatch.setattr(checkpoint, "identity", lambda _: post027_ctx)
    patch_hook3 = Mock(return_value=True)
    monkeypatch.setattr(checkpoint, "_apply_incremental_patches", patch_hook3)
    monkeypatch.setattr(sys, "argv", ["checkpoint.py", "restore", str(root3), str(ckpt26)])
    assert checkpoint.main() == 0
    patch_hook3.assert_called_once_with(root3 / "build/src", post026_saved["source"])
    assert json.loads((root3 / "native-ready.json").read_text()) == post027_ctx

    # Post-027 checkpoint (Run #22: has 023..027, lacks 028) must also migrate cleanly and apply 028.
    post027_saved = json.loads(json.dumps(post026_saved))
    post027_saved["source"]["locale_speech_font_patch_sha256"] = "old-027"
    ckpt27 = tmp_path / "ckpt27"
    checkpoint.pack(source, ckpt27, post027_saved)
    post028_ctx = json.loads(json.dumps(post027_saved))
    post028_ctx["source"]["pixelscan_coherence_patch_sha256"] = "new-028"
    post028_ctx["kit"]["prepare.py"] = "new-028"
    root4 = tmp_path / "root4"
    root4.mkdir()
    monkeypatch.setattr(checkpoint, "identity", lambda _: post028_ctx)
    patch_hook4 = Mock(return_value=True)
    monkeypatch.setattr(checkpoint, "_apply_incremental_patches", patch_hook4)
    monkeypatch.setattr(sys, "argv", ["checkpoint.py", "restore", str(root4), str(ckpt27)])
    assert checkpoint.main() == 0
    patch_hook4.assert_called_once_with(root4 / "build/src", post027_saved["source"])
    assert json.loads((root4 / "native-ready.json").read_text()) == post028_ctx


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX patch required")
def test_cli_restore_real_post_027_to_post_028_overlapping_patches(tmp_path, monkeypatch):
    if sys.platform.startswith("linux"):
        run = subprocess.run

        def portable_patch(args, **kwargs):
            if args[0] == "/usr/bin/patch":
                args = ["--dry-run" if arg == "-C" else arg for arg in args]
            return run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", portable_patch)

    prep_spec = importlib.util.spec_from_file_location("native_prepare", KIT / "prepare.py")
    assert prep_spec and prep_spec.loader
    native_prepare = importlib.util.module_from_spec(prep_spec)
    prep_spec.loader.exec_module(native_prepare)

    patch_028 = KIT / "patches/028-pixelscan-noise-font-coherence.patch"
    source = tmp_path / "post027-src"
    files: dict[str, list[str]] = {}
    current_rel = None
    in_hunk = False
    for raw in patch_028.read_text().splitlines(keepends=True):
        if raw.startswith("+++ b/"):
            current_rel = raw.removeprefix("+++ b/").strip()
            files[current_rel] = []
            in_hunk = False
        elif raw.startswith("@@ "):
            old_start = int(re.match(r"@@ -(\d+)(?:,\d+)? \+", raw).group(1))
            lines = files[current_rel]
            while len(lines) < old_start - 1:
                lines.append(f"// stub line {len(lines) + 1}\n")
            in_hunk = True
        elif in_hunk:
            if raw.startswith((" ", "-")):
                files[current_rel].append(raw[1:])
            elif raw.startswith("diff --git "):
                in_hunk = False
    for rel, lines in files.items():
        target = source / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(lines))

    root = tmp_path / "root"
    root.mkdir()
    saved_ctx = {
        "format": 1,
        "workspace": str(root.resolve()),
        "host": ["Darwin", "arm64"],
        "python": "3.12.0",
        "ninja": "1.12.1",
        "xcode": "Xcode 26",
        "sdk": "/SDK",
        "source": {
            "platform_revision": native_prepare.PLATFORM_REV,
            "macos_antidetect_patch_sha256": native_prepare.MACOS_ANTIDETECT_PATCH_SHA256,
            "webgl_readback_patch_sha256": native_prepare.READBACK_PARITY_PATCH_SHA256,
            "audio_clientrects_patch_sha256": native_prepare.AUDIO_CLIENTRECTS_PATCH_SHA256,
            "apple_silicon_coherence_patch_sha256": native_prepare.APPLE_SILICON_PATCH_SHA256,
            "locale_speech_font_patch_sha256": native_prepare.LOCALE_SPEECH_FONT_PATCH_SHA256,
        },
        "kit": {"ci-build.gn": "gn-sha", "build.sh": "old", "prepare.py": "old-027"},
    }
    ckpt = tmp_path / "ckpt27"
    checkpoint.pack(source, ckpt, saved_ctx)

    new_ctx = json.loads(json.dumps(saved_ctx))
    new_ctx["source"]["pixelscan_coherence_patch_sha256"] = (
        native_prepare.PIXELSCAN_COHERENCE_PATCH_SHA256
    )
    new_ctx["kit"]["prepare.py"] = "new-028"
    monkeypatch.setattr(checkpoint, "identity", lambda _: new_ctx)
    monkeypatch.setattr(checkpoint, "validate", Mock())
    monkeypatch.setattr(sys, "argv", ["checkpoint.py", "restore", str(root), str(ckpt)])

    assert checkpoint.main() == 0
    restored = root / "build/src"
    assert (
        "HasMultipleValidColors"
        in (
            restored / "third_party/blink/renderer/platform/graphics/static_bitmap_image.cc"
        ).read_text()
    )
    assert (
        "has_multiple_valid_colors"
        in (
            restored / "third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc"
        ).read_text()
    )
    assert (
        "kProtectedMacOsCoreFonts"
        in (restored / "third_party/blink/renderer/platform/fonts/font_cache.cc").read_text()
    )
    assert (
        subprocess.run(
            ["/usr/bin/patch", "-p1", "-R", "-f", "-C", "-d", str(restored), "-i", str(patch_028)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
        ).returncode
        == 0
    )
    assert checkpoint._apply_incremental_patches(restored, saved_ctx["source"]) is False
    # The fixture contains only 028's three files, not a complete 023..028 tree.
    with pytest.raises(
        RuntimeError, match="Incremental patch 023 missing file:.*fingerprint_gpu.h"
    ):
        checkpoint._apply_incremental_patches(restored)
    assert json.loads((root / "native-ready.json").read_text()) == new_ctx


def test_mtime_manifest_rejects_tampering(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("hi")
    ckpt = tmp_path / "ckpt"
    checkpoint.pack(source, ckpt, {"probe": "sha"})
    (ckpt / "mtime-ns.tsv").write_text("a.txt\t999\n")
    with pytest.raises(ValueError, match="mtime manifest mismatch"):
        checkpoint.unpack(tmp_path / "restored", ckpt, {"probe": "sha"})


def test_validate_rejects_stale_identity_flags_and_low_ram(tmp_path, monkeypatch):
    root = tmp_path
    context = {"revision": 1}
    monkeypatch.setattr(checkpoint, "identity", lambda _: context)
    monkeypatch.setattr(checkpoint.subprocess, "check_output", lambda *a, **k: str(7 * 1024**3))
    (root / "native-ready.json").write_text(json.dumps(context))
    (root / "ungoogled-chromium").mkdir()
    (root / "ungoogled-chromium/flags.gn").write_text("pgo = 0\n")
    (root / "flags.macos.gn").write_text("symbol_level = 1\n")
    args = root / "build/src/out/Default/args.gn"
    args.parent.mkdir(parents=True)
    args.write_bytes(b"pgo = 0\nsymbol_level = 1\n" + (KIT / "ci-build.gn").read_bytes())
    checkpoint.validate(root)
    args.write_text("changed")
    with pytest.raises(ValueError, match="GN flags"):
        checkpoint.validate(root)
    (root / "native-ready.json").write_text("{}")
    with pytest.raises(ValueError, match="identity changed"):
        checkpoint.validate(root)
    (root / "native-ready.json").write_text(json.dumps(context))
    args.write_bytes(b"pgo = 0\nsymbol_level = 1\n" + (KIT / "ci-build.gn").read_bytes())
    monkeypatch.setattr(checkpoint.subprocess, "check_output", lambda *a, **k: str(6 * 1024**3))
    with pytest.raises(ValueError, match="7 GiB RAM"):
        checkpoint.validate(root)


def test_identity_binds_tooling_source_and_flags(tmp_path, monkeypatch):
    kit = tmp_path / "kit"
    kit.mkdir()
    for name in ("build.sh", "prepare.py", "verify.py", "ci-build.gn", "checkpoint.py"):
        (kit / name).write_text(name)
    monkeypatch.setattr(checkpoint, "KIT", kit)
    monkeypatch.setattr(checkpoint.subprocess, "check_output", lambda cmd, **k: " ".join(cmd))
    (tmp_path / "source-provenance.json").write_text('{"revision": "pinned"}')
    before = checkpoint.identity(tmp_path)
    assert before["source"] == {"revision": "pinned"}
    assert before["workspace"] == str(tmp_path.resolve())
    assert before["xcode"] == "xcodebuild -version"
    assert before["ninja"] == "ninja --version"
    assert before["sdk"] == "xcrun --sdk macosx --show-sdk-path"
    (kit / "ci-build.gn").write_text("different flags")
    assert checkpoint.identity(tmp_path)["kit"] != before["kit"]


def test_cli_uses_two_workers_and_whole_job_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["checkpoint.py", "run", str(tmp_path)])
    monkeypatch.setattr(checkpoint, "validate", Mock())
    monkeypatch.setattr(checkpoint.time, "time", lambda: 200)
    monkeypatch.setenv("NATIVE_CHUNK_STARTED_AT", "100")
    run = Mock(return_value=0)
    monkeypatch.setattr(checkpoint, "run_bounded", run)
    assert checkpoint.main() == 0
    command, seconds = run.call_args.args
    assert command[:6] == ["/usr/bin/time", "-l", "ninja", "-j", "2", "-C"]
    assert command[-2:] == ["chrome", "chromedriver"]
    assert seconds == 4 * 3600 - 100


def test_workflow_chain_is_bounded_and_probe_is_independent():
    workflows = KIT.parents[1] / ".github/workflows"
    main = (workflows / "native-chromium.yml").read_text()
    chunks = (workflows / "native-chromium-chunk.yml").read_text()
    probe = (workflows / "native-checkpoint-probe.yml").read_text()
    jobs = dict(
        re.findall(r"^  (\w+):\n(.*?)(?=^  \w+:|\Z)", main.split("jobs:\n", 1)[1], re.M | re.S)
    )
    assert list(jobs) == ["inspect", "build", "resume_2", "resume_3", "resume_4"]
    for previous, current in [
        ("build", "resume_2"),
        ("resume_2", "resume_3"),
        ("resume_3", "resume_4"),
    ]:
        assert f"needs: {previous}" in jobs[current]
        assert f"needs.{previous}.outputs.state == 'partial'" in jobs[current]
    assert "last_chunk: true" in jobs["resume_4"]
    assert "overwrite:" not in chunks and "retention-days: 7" in chunks
    assert 'run["head_branch"] == os.environ["GITHUB_REF_NAME"]' in chunks
    assert "verify_failed" in chunks
    assert chunks.index("checkpoint.py restore") < chunks.index("mode=resume")
    assert "needs: save" in probe
    assert probe.count("runs-on: macos-26") == 2
    assert "concurrency:" not in probe
