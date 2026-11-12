"""Cross-process lease shared by profile runners and explicit core migrations."""

import hashlib
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def profile_lease(profile_dir):
    root = Path(tempfile.gettempdir()) / f"ronc-core-leases-{getattr(os, 'getuid', lambda: 0)()}"
    if not root.exists():
        root.mkdir(mode=0o700, exist_ok=True)
    # Windows st_mode synthesizes 0777 regardless of the real ACL. Leases
    # contain no secrets; POSIX mode enforcement must not disable Windows CRUD.
    if root.is_symlink() or (os.name != "nt" and root.stat().st_mode & 0o077):
        raise RuntimeError("Unsafe core lease directory")
    key = hashlib.sha256(str(Path(profile_dir).resolve()).encode()).hexdigest()
    fd = os.open(
        root / (key + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def native_profile_guard(source: Path, engine: str):
    """Hold Firefox's persistent native lock without deleting/truncating it."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        for name in ("lock", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = source / name
            if p.exists() or p.is_symlink():
                raise RuntimeError(
                    "Browser lock artifact present; close and verify the profile first"
                )
        for name in (".parentlock", "parent.lock"):
            p = source / name
            if p.is_symlink():
                raise RuntimeError("Legacy/symlink browser lock requires manual verification")
            if not p.exists():
                continue
            if engine != "camoufox" or not p.is_file():
                raise RuntimeError("Unexpected browser lock artifact present")
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.CreateFileW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ]
                api.CreateFileW.restype = wintypes.HANDLE
                api.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = api.CreateFileW(str(p), 0, 0, None, 3, 0, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise OSError("Firefox native lock is held or cannot be inspected")
                stack.callback(api.CloseHandle, handle)
            else:
                import fcntl

                fd = os.open(p, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                stack.callback(os.close, fd)
                # Firefox uses a POSIX record lock, NOT flock. Do not open/close
                # this inode elsewhere in-process while it is held.
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


# A store transaction calls save_profiles inside itself. Re-enter only that
# metadata lease, never a profile's runner lease (which must remain exclusive).
_STORE_MUTEX = threading.RLock()
_STORE_OWNER = threading.local()


@contextmanager
def store_lease(path):
    key = str(Path(path).resolve())
    with _STORE_MUTEX:
        if getattr(_STORE_OWNER, "key", None) == key:
            yield
        else:
            with profile_lease(path):
                previous = getattr(_STORE_OWNER, "key", None)
                _STORE_OWNER.key = key
                try:
                    yield
                finally:
                    _STORE_OWNER.key = previous
