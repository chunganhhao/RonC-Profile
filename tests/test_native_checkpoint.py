"""Process-based work/transfer/resume fixtures, not a Chromium compilation claim."""

import importlib.util
import io
import json
import os
import re
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
    marker = src / ".ninja_deps"
    marker.write_bytes(b"saved objects\x00deps")
    marker.chmod(0o755)
    os.utime(marker, (1700000000, 1700000000))
    (src / "relative").symlink_to(".ninja_deps")
    (src / "absolute").symlink_to(marker.resolve())
    return src


def test_roundtrip_preserves_outputs_modes_times_and_internal_links(source, tmp_path):
    context = {"source": "pinned", "flags": "exact"}
    output = tmp_path / "checkpoint"
    checkpoint.pack(source, output, context)
    checkpoint.unpack(tmp_path / "restored", output, context)
    restored = tmp_path / "restored/src/.ninja_deps"
    assert restored.read_bytes() == (source / ".ninja_deps").read_bytes()
    assert restored.stat().st_mtime == 1700000000
    assert restored.stat().st_mode & 0o777 == 0o755
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
    # Tar preserves fractional mtimes, but float conversion can lose nanoseconds.
    assert all(
        abs((restored / name).stat().st_mtime_ns - stamp) < 1000 for name, stamp in before.items()
    )


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
    assert "overwrite:" not in chunks and "retention-days: 1" in chunks
    assert 'run["head_sha"] == os.environ["GITHUB_SHA"]' in chunks
    assert chunks.index("checkpoint.py restore") < chunks.index("mode=resume")
    assert "needs: save" in probe
    assert probe.count("runs-on: macos-26") == 2
    assert "concurrency:" not in probe
