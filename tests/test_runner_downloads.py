import asyncio
import errno
import inspect
import os
import pathlib
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from src.interfaces.protocols import IBrowserLauncher
from src.services.browser import process, runner
from src.services.browser.launcher import BrowserLauncher


class _FakeDownload:
    def __init__(
        self,
        name: str,
        payload: bytes = b"ok",
        *,
        fail_reason: str | None = None,
        save_exc: BaseException | None = None,
        on_save=None,
    ):
        self.suggested_filename = name
        self._payload = payload
        self._fail_reason = fail_reason
        self._save_exc = save_exc
        self._on_save = on_save

    async def failure(self):
        return self._fail_reason

    async def save_as(self, dest: str):
        if self._on_save is not None:
            await self._on_save()
        if self._save_exc is not None:
            raise self._save_exc
        with open(dest, "wb") as handle:
            handle.write(self._payload)


def _patch_runner_env(
    monkeypatch,
    tmp_path,
    dl_dir,
    *,
    restore_tabs: bool = False,
    start_url: str = "",
    saved_urls: list[str] | None = None,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "_default_downloads_dir", lambda: dl_dir)
    monkeypatch.setattr(runner, "_read_profile_extra", lambda _: {"start_url": start_url})
    monkeypatch.setattr(runner, "load_or_create", lambda *_: {})
    monkeypatch.setattr(runner, "exit_ip", lambda *_: "198.51.100.1")
    monkeypatch.setattr(runner, "pin_geoip_locale", lambda _: None)
    monkeypatch.setattr(runner, "ensure_google_search", lambda: None)
    monkeypatch.setattr(runner, "HEADLESS", True)
    monkeypatch.setattr(runner, "RESTORE_TABS", restore_tabs)
    monkeypatch.setattr(runner, "_shutdown", asyncio.Event())
    if saved_urls is not None:
        monkeypatch.setattr(runner.session, "load", lambda *_: list(saved_urls))
        monkeypatch.setattr(runner.session, "save", lambda *_: True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "download"),
        ("", "download"),
        (".", "download"),
        ("..", "download"),
        ("...", "download"),
        (". .", "download"),
        ("/", "download"),
        ("\\", "download"),
        ("../../.zshrc", ".zshrc"),
        ("..\\..\\evil.exe", "evil.exe"),
        ("/etc/passwd", "passwd"),
        ("C:\\Windows\\win.ini", "win.ini"),
        ("a\x00b\n.mp4\x7f", "ab.mp4"),
        ("dir\x00/../../ok.txt", "ok.txt"),
        ("report.pdf...", "report.pdf"),
        ("  spaced name.zip  ", "spaced name.zip"),
        ("CON.txt", "_CON.txt"),
        ("CON .txt", "_CON .txt"),
        ("nul", "_nul"),
        ("COM1.mp4", "_COM1.mp4"),
        ("D:evil.mp4", "evil.mp4"),
        ("C:..", "download"),
        ("C:.", "download"),
        ("C:", "download"),
        ("foo:bar.mp4", "foo_bar.mp4"),
        ("báo cáo 2026.mp4", "báo cáo 2026.mp4"),
    ],
)
def test_safe_download_name_sanitizes_inputs(raw, expected):
    assert runner._safe_download_name(raw) == expected


def test_safe_download_name_truncates_long_names_preserving_extension():
    long_mp4 = ("a" * 210) + ".mp4"
    result_mp4 = runner._safe_download_name(long_mp4)
    assert len(result_mp4) == 200
    assert result_mp4.endswith(".mp4")

    long_ext = ("b" * 210) + "." + ("x" * 40)
    result_long_ext = runner._safe_download_name(long_ext)
    assert len(result_long_ext) == 200

    # Post-truncation Windows reserved name guard (including trailing-space stripping)
    assert runner._safe_download_name("CON" + (" " * 195) + "x.txt") == "_CON.txt"
    exact_200 = runner._safe_download_name("CON" + (" " * 193) + ".txt")
    assert exact_200 == "_CON.txt"
    assert len(exact_200) <= 200


def test_concurrent_independent_runners_never_overwrite_and_preserve_existing(tmp_path):
    existing = tmp_path / "report.pdf"
    existing.write_bytes(b"pre-existing-bytes")

    async def race():
        ready = 0
        release = asyncio.Event()

        async def barrier():
            nonlocal ready
            ready += 1
            if ready == 2:
                release.set()
            await release.wait()

        return await asyncio.gather(
            runner._persist_download(
                _FakeDownload("report.pdf", b"payload-A", on_save=barrier),
                downloads_dir=tmp_path,
            ),
            runner._persist_download(
                _FakeDownload("report.pdf", b"payload-B", on_save=barrier),
                downloads_dir=tmp_path,
            ),
        )

    p1, p2 = asyncio.run(race())
    assert existing.read_bytes() == b"pre-existing-bytes"
    assert p1 is not None and p2 is not None and p1 != p2
    assert {p1.name, p2.name} == {"report (1).pdf", "report (2).pdf"}
    assert {p1.read_bytes(), p2.read_bytes()} == {b"payload-A", b"payload-B"}
    assert list(tmp_path.glob(".ronc-download-*.part")) == []


def test_concurrent_fresh_filename_race_saves_both_payloads(tmp_path):
    async def race():
        ready = 0
        release = asyncio.Event()

        async def barrier():
            nonlocal ready
            ready += 1
            if ready == 2:
                release.set()
            await release.wait()

        return await asyncio.gather(
            runner._persist_download(
                _FakeDownload("fresh.pdf", b"payload-A", on_save=barrier),
                downloads_dir=tmp_path,
            ),
            runner._persist_download(
                _FakeDownload("fresh.pdf", b"payload-B", on_save=barrier),
                downloads_dir=tmp_path,
            ),
        )

    p1, p2 = asyncio.run(race())
    assert p1 is not None and p2 is not None and p1 != p2
    assert {p1.name, p2.name} == {"fresh.pdf", "fresh (1).pdf"}
    assert {p1.read_bytes(), p2.read_bytes()} == {b"payload-A", b"payload-B"}
    assert list(tmp_path.glob(".ronc-download-*.part")) == []


@pytest.mark.parametrize("link_errno", [errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS])
def test_publish_skips_symlinks_and_falls_back_when_hardlink_unsupported(
    tmp_path, monkeypatch, link_errno
):
    # 1. Hardlink path skips existing files, live symlinks, broken symlinks, and handles dotfiles.
    (tmp_path / "file.mp4").write_bytes(b"existing")
    target = tmp_path / "real-target.txt"
    target.write_text("keep-me", encoding="utf-8")
    (tmp_path / "file (1).mp4").symlink_to(target)
    (tmp_path / "file (2).mp4").symlink_to(tmp_path / "missing-target")

    staged = tmp_path / ".ronc-download-test.part"
    staged.write_bytes(b"new-video")
    published, preserved = runner._publish_download(staged, tmp_path, "file.mp4")
    staged.unlink()
    assert preserved is False
    assert published == tmp_path / "file (3).mp4"
    assert published.read_bytes() == b"new-video"
    assert target.read_text(encoding="utf-8") == "keep-me"

    (tmp_path / ".zshrc").write_text("dotfile", encoding="utf-8")
    staged_dot = tmp_path / ".ronc-download-dot.part"
    staged_dot.write_bytes(b"new-dot")
    dot_dest, dot_preserved = runner._publish_download(staged_dot, tmp_path, ".zshrc")
    staged_dot.unlink()
    assert dot_preserved is False
    assert dot_dest == tmp_path / ".zshrc (1)"

    # 2. Non-hardlink filesystem fallback uses atomic no-replace rename (never os.replace) safely under concurrency.
    monkeypatch.setattr(
        os,
        "link",
        lambda *_a: (_ for _ in ()).throw(OSError(link_errno, "hardlink unsupported")),
    )
    monkeypatch.setattr(
        os,
        "replace",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must not replace existing files")),
    )
    (tmp_path / "doc.pdf").write_bytes(b"keep-doc")
    (tmp_path / "doc (1).pdf").symlink_to(tmp_path / "missing-doc-target")
    (tmp_path / "doc (2).pdf").symlink_to(target)

    async def fallback_race():
        ready = 0
        release = asyncio.Event()

        async def barrier():
            nonlocal ready
            ready += 1
            if ready == 2:
                release.set()
            await release.wait()

        return await asyncio.gather(
            runner._persist_download(
                _FakeDownload("doc.pdf", b"fallback-A", on_save=barrier),
                downloads_dir=tmp_path,
            ),
            runner._persist_download(
                _FakeDownload("doc.pdf", b"fallback-B", on_save=barrier),
                downloads_dir=tmp_path,
            ),
        )

    d1, d2 = asyncio.run(fallback_race())
    assert (tmp_path / "doc.pdf").read_bytes() == b"keep-doc"
    assert (tmp_path / "doc (1).pdf").is_symlink()
    assert (tmp_path / "doc (2).pdf").is_symlink()
    assert target.read_text(encoding="utf-8") == "keep-me"
    assert d1 is not None and d2 is not None and d1 != d2
    assert {d1.name, d2.name} == {"doc (3).pdf", "doc (4).pdf"}
    assert {d1.read_bytes(), d2.read_bytes()} == {b"fallback-A", b"fallback-B"}
    assert list(tmp_path.glob(".ronc-download-*.part")) == []


def test_non_hardlink_atomic_publish_never_exposes_partial_file_to_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(
        os,
        "link",
        lambda *_a: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "operation not supported")),
    )
    monkeypatch.setattr(
        os,
        "replace",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must not replace existing files")),
    )

    payload = (b"A" * 32768) + (b"B" * 32768)
    final_path = tmp_path / "atomic.bin"
    observed_sizes: list[int] = []
    stop_reader = threading.Event()

    def reader_loop():
        while not stop_reader.is_set():
            try:
                data = final_path.read_bytes()
            except FileNotFoundError:
                continue
            observed_sizes.append(len(data))

    reader = threading.Thread(target=reader_loop, daemon=True)
    reader.start()

    class ChunkedDownload:
        suggested_filename = "atomic.bin"

        async def failure(self):
            return None

        async def save_as(self, dest: str):
            with open(dest, "wb") as handle:
                handle.write(payload[:16384])
                handle.flush()
                await asyncio.sleep(0.01)
                handle.write(payload[16384:])
                handle.flush()

    try:
        published = asyncio.run(runner._persist_download(ChunkedDownload(), downloads_dir=tmp_path))
    finally:
        stop_reader.set()
        reader.join(5)

    assert published == final_path
    assert final_path.read_bytes() == payload
    assert all(size == len(payload) for size in observed_sizes), observed_sizes
    assert list(tmp_path.glob(".ronc-download-*.part")) == []


@pytest.mark.parametrize("crash_mode", ["before_rename", "after_rename"])
def test_non_hardlink_subprocess_crash_never_leaves_partial_final_file(tmp_path, crash_mode):
    existing = tmp_path / "report.pdf"
    existing.write_bytes(b"old-report")
    repo_root = str(pathlib.Path(__file__).resolve().parents[1])
    script = """
import asyncio, errno, os, pathlib, sys
from src.services.browser import runner

dl_dir = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
payload = b"Z" * 16384

os.link = lambda *_a: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "no hardlink"))
orig_rename = runner._rename_noreplace

def crashed_rename(src, dest):
    if mode == "before_rename":
        os._exit(17)
    orig_rename(src, dest)
    os._exit(17)

runner._rename_noreplace = crashed_rename

class ChunkedDownload:
    suggested_filename = "report.pdf"
    async def failure(self):
        return None
    async def save_as(self, dest: str):
        with open(dest, "wb") as handle:
            handle.write(payload[:8192])
            handle.flush()
            handle.write(payload[8192:])
            handle.flush()

asyncio.run(runner._persist_download(ChunkedDownload(), downloads_dir=dl_dir))
"""
    env = dict(os.environ, PYTHONPATH=repo_root)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), crash_mode],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 17, proc.stderr
    assert existing.read_bytes() == b"old-report"
    published = tmp_path / "report (1).pdf"
    staged_files = list(tmp_path.glob(".ronc-download-*.part"))
    if crash_mode == "before_rename":
        assert not published.exists()
        assert len(staged_files) == 1
        assert staged_files[0].read_bytes() == b"Z" * 16384
    else:
        assert published.read_bytes() == b"Z" * 16384
        assert staged_files == []


@pytest.mark.parametrize(
    ("link_errno", "rename_exc"),
    [
        (errno.EOPNOTSUPP, OSError(errno.EOPNOTSUPP, "rename unsupported")),
        (errno.EINVAL, NotImplementedError("no primitive")),
    ],
)
def test_unsupported_publish_primitives_preserve_complete_recovery_files_and_clean_incomplete(
    tmp_path, monkeypatch, capsys, link_errno, rename_exc
):
    monkeypatch.setattr(
        os,
        "link",
        lambda *_a: (_ for _ in ()).throw(OSError(link_errno, "link unsupported")),
    )
    monkeypatch.setattr(
        runner,
        "_rename_noreplace",
        lambda *_a: (_ for _ in ()).throw(rename_exc),
    )

    existing = tmp_path / "report.pdf"
    existing.write_bytes(b"keep-existing")

    async def race():
        ready = 0
        release = asyncio.Event()

        async def barrier():
            nonlocal ready
            ready += 1
            if ready == 2:
                release.set()
            await release.wait()

        return await asyncio.gather(
            runner._persist_download(
                _FakeDownload("report.pdf", b"recovery-A", on_save=barrier),
                downloads_dir=tmp_path,
            ),
            runner._persist_download(
                _FakeDownload("report.pdf", b"recovery-B", on_save=barrier),
                downloads_dir=tmp_path,
            ),
        )

    r1, r2 = asyncio.run(race())
    out = capsys.readouterr().out
    assert existing.read_bytes() == b"keep-existing"
    assert not (tmp_path / "report (1).pdf").exists()
    assert r1 is not None and r2 is not None and r1 != r2
    assert r1.parent == tmp_path and r2.parent == tmp_path
    assert {r1.read_bytes(), r2.read_bytes()} == {b"recovery-A", b"recovery-B"}
    assert out.count("Download saved at recovery path (report.pdf):") == 2

    # Incomplete downloads still get cleaned up even when primitives are unsupported.
    assert (
        asyncio.run(
            runner._persist_download(
                _FakeDownload("report.pdf", save_exc=OSError("disk full")),
                downloads_dir=tmp_path,
            )
        )
        is None
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runner._persist_download(
                _FakeDownload("report.pdf", save_exc=asyncio.CancelledError()),
                downloads_dir=tmp_path,
            )
        )
    assert set(tmp_path.glob(".ronc-download-*.part")) == {r1, r2}


@pytest.mark.parametrize("fail_via_rename", [True, False])
def test_publish_enospc_after_staging_preserves_recovery_file_and_logs_reason(
    tmp_path, monkeypatch, capsys, fail_via_rename
):
    existing = tmp_path / "report.pdf"
    existing.write_bytes(b"existing-report")
    payload = b"Q" * 16384

    if fail_via_rename:
        monkeypatch.setattr(
            os,
            "link",
            lambda *_a: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "no hardlink")),
        )
        monkeypatch.setattr(
            runner,
            "_rename_noreplace",
            lambda *_a: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device")),
        )
    else:
        monkeypatch.setattr(
            os,
            "link",
            lambda *_a: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device")),
        )

    recovery = asyncio.run(
        runner._persist_download(_FakeDownload("report.pdf", payload), downloads_dir=tmp_path)
    )
    out = capsys.readouterr().out
    assert recovery is not None
    assert recovery.parent == tmp_path
    assert recovery.read_bytes() == payload
    assert existing.read_bytes() == b"existing-report"
    assert not (tmp_path / "report (1).pdf").exists()
    assert "report.pdf" in out
    assert "No space left on device" in out
    assert str(recovery) in out


def test_publish_is_bounded_and_cleans_part_files_on_error_or_cancel(tmp_path, monkeypatch, capsys):
    staged = tmp_path / ".ronc-download-bounded.part"
    staged.write_bytes(b"bytes")
    calls = 0

    def distinct_resolve(self):
        nonlocal calls
        calls += 1
        return tmp_path / f"dir-{calls}"

    monkeypatch.setattr(type(tmp_path), "resolve", distinct_resolve)
    with pytest.raises(OSError, match="Could not publish download destination"):
        runner._publish_download(staged, tmp_path, "clip.mp4")
    assert calls == 1 + 1 + 9999 + 1000
    monkeypatch.undo()
    staged.unlink()

    # Failed/canceled download creates no file and no .part leftovers.
    canceled = asyncio.run(
        runner._persist_download(
            _FakeDownload("ignored.mp4", fail_reason="canceled"), downloads_dir=tmp_path
        )
    )
    assert canceled is None
    assert not (tmp_path / "ignored.mp4").exists()
    assert list(tmp_path.glob(".ronc-download-*.part")) == []

    # save_as OSError cleans up .part temp file and logs error.
    errored = asyncio.run(
        runner._persist_download(
            _FakeDownload("broken.mp4", save_exc=OSError("disk full")),
            downloads_dir=tmp_path,
        )
    )
    assert errored is None
    assert "Download save failed (broken.mp4): disk full" in capsys.readouterr().out
    assert list(tmp_path.glob(".ronc-download-*.part")) == []

    # Cancellation propagates and cleans up .part temp file.
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runner._persist_download(
                _FakeDownload("cancelled.mp4", save_exc=asyncio.CancelledError()),
                downloads_dir=tmp_path,
            )
        )
    assert list(tmp_path.glob(".ronc-download-*.part")) == []


def test_stop_timeout_exceeds_download_drain_budget():
    assert process.DOWNLOAD_DRAIN_SECONDS == 5.0
    assert process.STOP_TIMEOUT_SECONDS > process.DOWNLOAD_DRAIN_SECONDS
    assert (
        inspect.signature(BrowserLauncher.stop_profile).parameters["timeout"].default
        == process.STOP_TIMEOUT_SECONDS
    )
    assert (
        inspect.signature(IBrowserLauncher.stop_profile).parameters["timeout"].default
        == process.STOP_TIMEOUT_SECONDS
    )


def test_launcher_stop_profile_allows_3s_download_drain_to_finish(tmp_path, monkeypatch):
    dl_dir = tmp_path / "Downloads"
    _patch_runner_env(monkeypatch, tmp_path, dl_dir, restore_tabs=False)
    monkeypatch.setattr(process, "cleanup", lambda _proc: None)

    class FakePage:
        def __init__(self):
            self.listeners: dict[str, list] = {}
            self.closed = False
            self.url = "about:blank"

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

        def emit(self, event, *args):
            for cb in list(self.listeners.get(event, [])):
                cb(*args)

        def is_closed(self):
            return self.closed

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]
            self.listeners: dict[str, list] = {}

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

    ctx = FakeContext()
    runner_loop: list[asyncio.AbstractEventLoop] = []
    ready_to_stop = threading.Event()
    finish_copy = asyncio.Event()
    runner_rc: list[int] = []

    async def controlled_copy():
        await finish_copy.wait()

    class FakeBrowser:
        async def __aenter__(self):
            return ctx

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(runner, "AsyncCamoufox", lambda **_: FakeBrowser())

    async def serve(_):
        runner_loop.append(asyncio.get_running_loop())
        ctx.pages[0].emit(
            "download",
            _FakeDownload("large.mp4", b"full-payload", on_save=controlled_copy),
        )
        ready_to_stop.set()
        return SimpleNamespace(cleanup=lambda: asyncio.sleep(0)), 1234, "tok"

    monkeypatch.setattr(runner.control, "serve", serve)

    def run_runner():
        runner_rc.append(
            asyncio.run(runner.run_browser("fixture", "None", "windows", locale="en-US"))
        )

    worker = threading.Thread(target=run_runner, daemon=True)
    worker.start()
    assert ready_to_stop.wait(5)

    # Simulated runner shutdown cost: 3.0s download drain (> old 2s limit, < 5s drain) + 0.5s cleanup.
    simulated_runner_finish_s = 3.5

    class FakeProc:
        def __init__(self):
            self.terminated = False
            self.killed = False
            self.wait_timeouts: list[float | None] = []
            self._rc: int | None = None

        def poll(self):
            return self._rc

        def terminate(self):
            self.terminated = True
            loop = runner_loop[0]
            loop.call_soon_threadsafe(runner._shutdown.set)

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if timeout is not None and timeout < simulated_runner_finish_s:
                raise subprocess.TimeoutExpired("runner", timeout)
            loop = runner_loop[0]
            loop.call_soon_threadsafe(finish_copy.set)
            worker.join(5)
            self._rc = runner_rc[0]
            return self._rc

        def kill(self):
            self.killed = True
            self._rc = -9

    proc = FakeProc()
    bl = BrowserLauncher()
    bl._active_sessions["fixture"] = proc
    try:
        assert bl.stop_profile("fixture") is True
    finally:
        bl._active_sessions.clear()

    assert proc.terminated is True
    assert proc.killed is False
    assert proc.wait_timeouts == [process.STOP_TIMEOUT_SECONDS]
    assert runner_rc == [0]
    assert (dl_dir / "large.mp4").read_bytes() == b"full-payload"


def test_launcher_stop_profile_force_kills_hung_runner(monkeypatch):
    monkeypatch.setattr(process, "cleanup", lambda _proc: None)

    class HungProc:
        def __init__(self):
            self.terminated = False
            self.killed = False
            self.wait_timeouts: list[float | None] = []

        def poll(self):
            return None if not self.killed else -9

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if not self.killed:
                raise subprocess.TimeoutExpired("runner", timeout)
            return -9

        def kill(self):
            self.killed = True

    proc = HungProc()
    bl = BrowserLauncher()
    bl._active_sessions["hung"] = proc
    try:
        assert bl.stop_profile("hung") is True
    finally:
        bl._active_sessions.clear()

    assert proc.terminated is True
    assert proc.killed is True
    assert proc.wait_timeouts == [process.STOP_TIMEOUT_SECONDS, 1]


@pytest.mark.parametrize("emit_page_event", [True, False])
def test_runner_captures_downloads_inside_restore_tabs_without_duplicates(
    tmp_path, monkeypatch, capsys, emit_page_event
):
    dl_dir = tmp_path / "Downloads"
    _patch_runner_env(
        monkeypatch,
        tmp_path,
        dl_dir,
        restore_tabs=True,
        saved_urls=["https://tab1.test/dl", "https://tab2.test/dl"],
    )

    class FakePage:
        def __init__(self):
            self.listeners: dict[str, list] = {}
            self.closed = False
            self.url = "about:blank"

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

        def emit(self, event, *args):
            for cb in list(self.listeners.get(event, [])):
                cb(*args)

        def is_closed(self):
            return self.closed

        async def goto(self, url, **_):
            self.url = url
            if "tab1.test" in url:
                self.emit("download", _FakeDownload("tab1.mp4", b"tab1-bytes"))
            elif "tab2.test" in url:
                self.emit("download", _FakeDownload("tab2.pdf", b"tab2-bytes"))

        async def bring_to_front(self):
            pass

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]
            self.listeners: dict[str, list] = {}

        async def new_page(self):
            page = FakePage()
            self.pages.append(page)
            if emit_page_event:
                self.emit("page", page)
            return page

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

        def emit(self, event, *args):
            for cb in list(self.listeners.get(event, [])):
                cb(*args)

    ctx = FakeContext()

    class FakeBrowser:
        async def __aenter__(self):
            return ctx

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(runner, "AsyncCamoufox", lambda **_: FakeBrowser())

    async def serve(_):
        for page in list(ctx.pages):
            page.closed = True
            page.emit("close", page)
        return SimpleNamespace(cleanup=lambda: asyncio.sleep(0)), 1234, "tok"

    monkeypatch.setattr(runner.control, "serve", serve)
    assert asyncio.run(runner.run_browser("fixture", "None", "windows", locale="en-US")) == 0
    capsys.readouterr()

    assert {p.name for p in dl_dir.iterdir()} == {"tab1.mp4", "tab2.pdf"}
    assert (dl_dir / "tab1.mp4").read_bytes() == b"tab1-bytes"
    assert (dl_dir / "tab2.pdf").read_bytes() == b"tab2-bytes"


def test_runner_captures_download_inside_start_url_fallback_without_premature_close(
    tmp_path, monkeypatch, capsys
):
    dl_dir = tmp_path / "Downloads"
    _patch_runner_env(
        monkeypatch,
        tmp_path,
        dl_dir,
        restore_tabs=True,
        saved_urls=["https://fail.test/"],
        start_url="https://startup.test/dl",
    )
    serve_called = False

    class FakePage:
        def __init__(self, context):
            self.context = context
            self.listeners: dict[str, list] = {}
            self.closed = False
            self.url = "about:blank"

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

        def emit(self, event, *args):
            for cb in list(self.listeners.get(event, [])):
                cb(*args)

        def is_closed(self):
            return self.closed

        async def goto(self, url, **_):
            assert not self.closed
            if "fail.test" in url:
                raise RuntimeError("net::ERR")
            self.url = url
            self.emit("download", _FakeDownload("startup.zip", b"startup-bytes"))

        async def close(self):
            self.closed = True
            if self in self.context.pages:
                self.context.pages.remove(self)
            self.emit("close", self)

        async def bring_to_front(self):
            pass

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage(self)]
            self.listeners: dict[str, list] = {}

        async def new_page(self):
            page = FakePage(self)
            self.pages.append(page)
            self.emit("page", page)
            return page

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

        def emit(self, event, *args):
            for cb in list(self.listeners.get(event, [])):
                cb(*args)

    ctx = FakeContext()

    class FakeBrowser:
        async def __aenter__(self):
            return ctx

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(runner, "AsyncCamoufox", lambda **_: FakeBrowser())

    async def serve(_):
        nonlocal serve_called
        serve_called = True
        for page in list(ctx.pages):
            await page.close()
        return SimpleNamespace(cleanup=lambda: asyncio.sleep(0)), 1234, "tok"

    monkeypatch.setattr(runner.control, "serve", serve)
    assert asyncio.run(runner.run_browser("fixture", "None", "windows", locale="en-US")) == 0
    capsys.readouterr()

    assert serve_called is True
    assert {p.name for p in dl_dir.iterdir()} == {"startup.zip"}
    assert (dl_dir / "startup.zip").read_bytes() == b"startup-bytes"


def test_runner_wires_page_downloads_and_drains_before_browser_closed_and_exit(
    tmp_path, monkeypatch, capsys
):
    dl_dir = tmp_path / "Downloads"
    _patch_runner_env(monkeypatch, tmp_path, dl_dir, restore_tabs=False)

    class FakePage:
        def __init__(self):
            self.listeners: dict[str, list] = {}
            self.closed = False
            self.url = "about:blank"

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

        def emit(self, event, *args):
            for cb in list(self.listeners.get(event, [])):
                cb(*args)

        def is_closed(self):
            return self.closed

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]
            self.listeners: dict[str, list] = {}

        def on(self, event, cb):
            self.listeners.setdefault(event, []).append(cb)

        def emit(self, event, *args):
            for cb in list(self.listeners.get(event, [])):
                cb(*args)

    events: list[str] = []
    ctx = FakeContext()

    class SlowDownload:
        def __init__(self, name: str, payload: bytes, delay: float):
            self.suggested_filename = name
            self._payload = payload
            self._delay = delay

        async def failure(self):
            return None

        async def save_as(self, dest: str):
            await asyncio.sleep(self._delay)
            with open(dest, "wb") as handle:
                handle.write(self._payload)
            events.append(f"saved:{self.suggested_filename}")

    class FakeBrowser:
        async def __aenter__(self):
            return ctx

        async def __aexit__(self, *_):
            events.append("browser_exit")

    monkeypatch.setattr(runner, "AsyncCamoufox", lambda **_: FakeBrowser())

    async def serve(_):
        async def trigger_downloads_and_close():
            await asyncio.sleep(0)
            ctx.pages[0].emit("download", SlowDownload("first.mp4", b"one", 0.04))
            page2 = FakePage()
            ctx.pages.append(page2)
            ctx.emit("page", page2)
            page2.emit("download", SlowDownload("second.pdf", b"two", 0.06))
            for page in list(ctx.pages):
                page.closed = True
                page.emit("close", page)

        asyncio.create_task(trigger_downloads_and_close())
        return SimpleNamespace(cleanup=lambda: asyncio.sleep(0)), 1234, "tok"

    monkeypatch.setattr(runner.control, "serve", serve)
    monkeypatch.setattr(
        runner, "_safe_print", lambda msg: events.append(msg) if msg == "BROWSER_CLOSED" else None
    )
    assert asyncio.run(runner.run_browser("fixture", "None", "windows", locale="en-US")) == 0
    capsys.readouterr()

    assert (dl_dir / "first.mp4").read_bytes() == b"one"
    assert (dl_dir / "second.pdf").read_bytes() == b"two"
    assert events.index("saved:first.mp4") < events.index("BROWSER_CLOSED")
    assert events.index("saved:second.pdf") < events.index("BROWSER_CLOSED")
    assert events.index("BROWSER_CLOSED") < events.index("browser_exit")
