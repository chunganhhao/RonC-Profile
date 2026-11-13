#!/usr/bin/env bash
# Experimental arm64 build, following the pinned platform's pre-sign stages.
set -euo pipefail

root="$(cd "$1" && pwd -P)"
kit="$(cd "$(dirname "$0")" && pwd -P)"
test -f "$root/source-provenance.json"
command -v ninja
command -v greadlink

fork="$root/ungoogled-chromium"
src="$root/build/src"
mode="${2:-fresh}"
if [[ "$mode" == "fresh" ]]; then
  python3 "$kit/prepare.py" --preflight "$root"
  test ! -e "$root/build"
  mkdir -p "$root/build/download_cache"

  # -d uses the release source archive and upstream hash verification, not depot_tools.
  bash "$root/retrieve_and_unpack_resource.sh" -d -g arm64
  python3 "$fork/utils/prune_binaries.py" "$src" "$fork/pruning.list"
  python3 "$fork/utils/patches.py" apply "$src" "$fork/patches" "$root/patches"
  python3 "$fork/utils/domain_substitution.py" apply -r "$fork/domain_regex.list" -f "$fork/domain_substitution.list" "$src"
  mkdir -p "$src/out/Default"
  cat "$fork/flags.gn" "$root/flags.macos.gn" "$kit/ci-build.gn" > "$src/out/Default/args.gn"
  cat "$src/out/Default/args.gn"
  bash "$root/retrieve_and_unpack_resource.sh" -d -p arm64

  cd "$src"
  ./tools/gn/bootstrap/bootstrap.py -o out/Default/gn --skip-generate-buildfiles
  ./tools/rust/build_bindgen.py --skip-test
  ./out/Default/gn gen out/Default --fail-on-unused-args
  python3 "$kit/checkpoint.py" mark "$root"
elif [[ "$mode" == "resume" ]]; then
  python3 "$kit/checkpoint.py" validate "$root"
else
  echo "Expected fresh or resume build mode" >&2
  exit 2
fi

cd "$src"
# shortcut: two workers on 7 GiB; no ThinLTO/dSYMs, one link. Revisit after CI measurements.
set +e
python3 "$kit/checkpoint.py" run "$root"
status=$?
set -e
if [[ "$status" == 75 ]]; then
  echo "state=partial" >> "$GITHUB_OUTPUT"
  exit 0
fi
if [[ "$status" != 0 ]]; then
  exit "$status"
fi

# Upstream's dev-signing alternative; no Developer ID/notarization secrets used.
codesign --force --deep --sign - out/Default/Chromium.app
codesign --verify --deep --strict out/Default/Chromium.app

# No app JS shim: reject the build if native realm/CSP/canvas gates fail.
python3 "$kit/verify.py" out/Default/Chromium.app/Contents/MacOS/Chromium "$root/native-acceptance.json"

output="$root/artifacts"
package="$root/package/experimental-chromium-148-arm64"
mkdir -p "$output" "$package"
ditto out/Default/Chromium.app "$package/Chromium.app"
cp out/Default/chromedriver "$package/chromedriver"
cp "$root/source-provenance.json" "$output/source-provenance.json"
cp "$root/source-provenance.json" "$package/source-provenance.json"
cp "$root/native-acceptance.json" "$output/native-acceptance.json"
cp "$root/native-acceptance.json" "$package/native-acceptance.json"
cp "$src/out/Default/args.gn" "$output/args.gn"
cp "$src/out/Default/args.gn" "$package/args.gn"
cp "$kit/README.md" "$package/ACCEPTANCE.md"
mkdir -p "$package/licenses"
cp "$root/LICENSE" "$package/licenses/platform-LICENSE"
cp "$fork/LICENSE" "$package/licenses/fingerprint-LICENSE"
# Archive first: direct upload-artifact loses executable modes and symlinks.
ditto -c -k --keepParent "$package" "$output/experimental-chromium-148-arm64.zip"
cd "$output"
shasum -a 256 experimental-chromium-148-arm64.zip > experimental-chromium-148-arm64.zip.sha256

echo "state=complete" >> "$GITHUB_OUTPUT"
