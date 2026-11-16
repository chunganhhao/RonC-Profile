"""Canonical identity of a whole macOS app, without following external links."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


def bundle_sha256(bundle: Path) -> str:
    root = bundle.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Native bundle is not a directory")
    records = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError("Native bundle link escapes its root")
                records.append([relative, "link", os.readlink(path)])
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                records.append([relative, "file", mode & 0o111, digest.hexdigest()])
            elif stat.S_ISDIR(mode):
                records.append([relative, "directory"])
            else:
                raise ValueError("Native bundle contains a special file")
    encoded = json.dumps(sorted(records), ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
