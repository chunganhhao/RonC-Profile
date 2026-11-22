#!/usr/bin/env bash
# Safely rename local workspace directory from "foxprofile" to "RonC Profile".
set -euo pipefail

ALLOW_EXTENSION_ID_RESET=0
for arg in "$@"; do
  case "$arg" in
    --allow-extension-id-reset)
      ALLOW_EXTENSION_ID_RESET=1
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--allow-extension-id-reset]" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_PARENT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
PARENT="${RONC_RENAME_PARENT:-$DEFAULT_PARENT}"
PYTHON_BIN="$(command -v python3 || echo /usr/bin/python3)"
if [[ -d "$PARENT" ]]; then
  PARENT="$(cd "$PARENT" && pwd -P)"
fi
OLD_DIR="$PARENT/foxprofile"
NEW_DIR="$PARENT/RonC Profile"
NEW_REMOTE="https://github.com/chunganhhao/RonC-Profile.git"

if [[ ! -d "$OLD_DIR" ]]; then
  echo "Source directory not found: $OLD_DIR" >&2
  exit 1
fi
if [[ -e "$NEW_DIR" ]]; then
  echo "Destination already exists: $NEW_DIR" >&2
  exit 1
fi
if (pgrep -fl "$OLD_DIR" 2>/dev/null || true) | awk -v self="$$" -v parent="$PPID" '$1 != self && $1 != parent && $0 !~ /rename_workspace_to_ronc\.sh/ { found=1 } END { exit !found }'; then
  echo "Refusing to rename while processes are still running from $OLD_DIR." >&2
  echo "Close RonC Profile UI and all open browser profiles first." >&2
  exit 1
fi

if [[ "$ALLOW_EXTENSION_ID_RESET" -eq 0 && -f "$OLD_DIR/extensions.json" ]]; then
  "$PYTHON_BIN" - "$OLD_DIR" <<'PY'
import json
import sys
from pathlib import Path

old_dir = Path(sys.argv[1]).resolve()
ext_file = old_dir / "extensions.json"
raw = json.loads(ext_file.read_text(encoding="utf-8"))
items = raw if isinstance(raw, list) else raw.get("extensions", [])
ext_base = (old_dir / "extensions_data").resolve()

keyless = []
for item in items:
    if not isinstance(item, dict):
        continue
    eid = str(item.get("id") or "").strip()
    name = str(item.get("name") or eid).strip()
    raw_path = str(item.get("path") or "").strip()
    ext_dir = Path(raw_path).resolve() if raw_path else None
    if ext_dir is None or not ext_dir.is_dir():
        ext_dir = None
        candidates = [eid]
        if raw_path:
            candidates.append(Path(raw_path).name)
        for rel in candidates:
            if (
                not rel
                or rel in (".", "..")
                or "/" in rel
                or "\\" in rel
                or Path(rel).name != rel
            ):
                continue
            cand = (ext_base / rel).resolve()
            if cand != ext_base and cand.is_relative_to(ext_base) and cand.is_dir():
                ext_dir = cand
                break
    if ext_dir is None or not ext_dir.is_relative_to(old_dir):
        continue
    manifest_path = ext_dir / "manifest.json"
    has_key = False
    if manifest_path.is_file():
        try:
            import base64

            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            raw_key = str(manifest.get("key") or "").strip() if isinstance(manifest, dict) else ""
            has_key = bool(raw_key and base64.b64decode(raw_key, validate=False))
        except Exception:
            has_key = False
    if not has_key:
        cid = str(item.get("chrome_id") or item.get("chrome_ext_id") or "").strip()
        keyless.append(f"  - {eid} ({name}, chrome_id={cid or 'unknown'})")

if keyless:
    print(
        "Refusing to rename workspace because unpacked extensions without manifest.key "
        "derive their Chromium ID from the folder path and would lose access to existing "
        "chrome.storage.local / IndexedDB data in profiles:",
        file=sys.stderr,
    )
    print("\n".join(keyless), file=sys.stderr)
    print("Pass --allow-extension-id-reset if you confirm resetting their extension IDs.", file=sys.stderr)
    sys.exit(1)
PY
fi

cd "$PARENT"
mv "$OLD_DIR" "$NEW_DIR"

"$PYTHON_BIN" - "$OLD_DIR" "$NEW_DIR" <<'PY'
import sys
from pathlib import Path

old_dir, new_dir = sys.argv[1], sys.argv[2]
root = Path(new_dir)
targets = [root / "extensions.json"]
venv_bin = root / ".venv" / "bin"
if venv_bin.is_dir():
    targets.extend(p for p in venv_bin.iterdir() if p.is_file() and not p.is_symlink())

for path in targets:
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    if old_dir in text:
        path.write_text(text.replace(old_dir, new_dir), encoding="utf-8")
PY

if [[ -x "$NEW_DIR/.venv/bin/python" && -f "$NEW_DIR/extensions.json" ]]; then
  "$NEW_DIR/.venv/bin/python" - "$NEW_DIR" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from src.services.browser.extensions import ExtensionService

svc = ExtensionService(
    file_path=str(root / "extensions.json"),
    extensions_dir=str(root / "extensions_data"),
)
svc._save()
PY
fi

if [[ -d "$NEW_DIR/.git" ]]; then
  git -C "$NEW_DIR" remote set-url origin "$NEW_REMOTE"
  echo "Updated origin remote to: $(git -C "$NEW_DIR" remote get-url origin)"
fi
echo "Renamed workspace to: $NEW_DIR"
