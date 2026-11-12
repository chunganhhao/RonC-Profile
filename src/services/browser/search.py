"""Repair the legacy Camoufox loopback search policy, never profile storage."""

import json
import os
import tempfile
from pathlib import Path

from camoufox.pkgman import build_file, get_path


def ensure_google_search(executable: str = "") -> bool:
    executable = executable or os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "").strip()
    path = (
        build_file(Path(executable).absolute(), "distribution/policies.json")
        if executable
        else Path(get_path("distribution/policies.json"))
    )
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    search = data.get("policies", {}).get("SearchEngines", {})
    if search.get("Default") != "None" or not any(
        engine.get("Name") == "None" and engine.get("URLTemplate") == "http://127.0.0.1"
        for engine in search.get("Add", [])
    ):
        return False
    # ponytail: legacy binary policy workaround until an upstream build without
    # the inert engine is installed: github.com/daijro/camoufox/issues/737.
    search["Default"] = "Google"
    search["Remove"] = [name for name in search.get("Remove", []) if name != "Google"]
    search["Add"] = [
        engine for engine in search["Add"] if engine.get("Name") not in {"None", "Google"}
    ]
    search["Add"].append(
        {
            "Name": "Google",
            "URLTemplate": "https://www.google.com/search?q={searchTerms}",
            "Method": "GET",
        }
    )
    # Replace atomically so concurrent launches never read a half-written policy.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as out:
            temporary = Path(out.name)
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True
