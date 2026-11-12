# Chromium 148 native correction — build input, NOT an installed fix

Base: `adryfish/fingerprint-chromium` commit
`13b89eae304123f0710f2d33fd0816a2f61d7ffc` (148.0.7778.215).
`patches/109-canvas-pixel-addressing.patch` is the unmodified diff of
[PR109](https://github.com/adryfish/fingerprint-chromium/pull/109), head
`96d183bab734d9d056476cd6c887d35c622a010a`. It remains open/unmerged at
2026-11-12. Upstream BSD-3-Clause notice is retained in `LICENSE`.

## Apply at the right layer

This is a patch **to the upstream patch repository**, not to the Chromium source
tree and not to FoxProfile's installed binary. In a separate clean checkout at
the exact base above, apply it BEFORE the upstream Chromium patch-series step:

```sh
git apply --check /absolute/path/to/foxprofile/native/chromium-148/patches/109-canvas-pixel-addressing.patch
git apply /absolute/path/to/foxprofile/native/chromium-148/patches/109-canvas-pixel-addressing.patch
```

Do not apply it twice or relax patch failures. If upstream merges it, use the
merged source instead of replaying this overlay. Follow the platform-specific
[upstream build instructions](https://github.com/adryfish/fingerprint-chromium/blob/13b89eae304123f0710f2d33fd0816a2f61d7ffc/docs/building.md);
this small repository is not a full Chromium checkout or a complete macOS build.

## Mechanism and verification

`writable_addr(..., x + 1, y)` previously expanded the byte offset as
`x + 1 * sizeof(T)` rather than `(x + 1) * sizeof(T)`. Similarly `y + 1`
lost row-stride grouping. The edge detector in `012-canvas-get-image-data.patch`
then read unrelated bytes instead of neighboring pixels, treating solid fills
as edges. Parenthesizing macro arguments fixes that root cause without disabling
canvas noise or adding JavaScript interception.

`tests/test_native_canvas_patch.py` compiles both removed and added macro lines:
the old expression must fail and the correction must pass for 8/16/32-bit pixels,
all tested neighboring coordinates and padded strides. No network is used by
the test. This proves pointer arithmetic, not complete Chromium behavior.

## Still required before any runtime acceptance

- Full macOS Chromium build prerequisites and source. On this machine, current
  `xcodebuild -version` fails because only Command Line Tools are selected;
  do not change the developer directory or install Xcode automatically.
- A compiled, integrity-pinned core tested in disposable profiles: solid fills,
  edge noise, repeated readback, encoded canvas paths and OffscreenCanvas.
- Separate native WebGPU/worker fix and strict-CSP tests. PR109 does NOT fix
  ServiceWorker GPU exposure or the JavaScript Blob-worker CSP incompatibility.
  Fury's BSD native WebGPU design is a reference, not a drop-in patch: its
  configuration plumbing and Chromium API differ from this seed-based 148 core.
- Keep existing production core/profile bindings unchanged until compatibility
  and independent review pass. Never substitute Chromium150: Engram #7202
  records its canvas renderer crash. No anonymity guarantee follows from this
  source-level check; the installed native findings remain OPEN.

## Manual GitHub Actions experimental build

`.github/workflows/native-chromium.yml` prepares and builds this source on CI;
local Xcode is not required. It has **workflow_dispatch only**, no push/PR build,
release publishing, production activation or signing secrets. Do not dispatch it
until the intended source changes are reviewed. The initial artifact is canvas-only
and **EXPERIMENTAL**, not a fix for the two remaining WebGPU/CSP findings.

Supply a dedicated disposable **macOS arm64 runner label**, with selected full
Xcode26, at least16GiB RAM and200GiB free disk. These are conservative job budgets,
not a guarantee a full build fits; the single job has a360-minute timeout and no
incremental-resume support. Ordinary GitHub-hosted macOS runners (including
`-xlarge`) are advertised with14GB SSD and must not be assumed sufficient:
[GitHub runner specifications](https://docs.github.com/en/actions/reference/runners/larger-runners).
No paid runner is selected automatically. Runner provisioning and the actual
compile remain unverified until an authorized CI run; no local runner registration
or Xcode installation is performed by these files.

The workflow checks out macOS scaffold `6a00dfe08591a15cfb0aed0edd292ba789d9a0d7`
and replaces its stock ungoogled submodule with the fingerprint revision above.
`prepare.py` verifies both HEADs and clean tracked inputs, refuses existing build
trees, applies PR109, and adds a missing Rust archive SHA256 from the official
[dated checksum](https://static.rust-lang.org/dist/2026-02-27/rust-nightly-aarch64-apple-darwin.tar.xz.sha256).
LLVM and Node already have upstream SHA512 pins; Chromium source uses upstream's
official hash-list download. This is not a claim of fully hermetic/reproducible
builds: CI actions/tool installation and official source hash lists remain external
inputs. No PySocks/httplib2 install is needed by this archive-download route.

`build.sh` follows pinned upstream retrieval, pruning, patching, domain substitution,
GN and Ninja stages. It does NOT run upstream `build.sh` (which removes prior output
and calls Developer-ID/notarization packaging), or the Xcode cleanup script (which
deletes other Xcode installations). Only the CI runner installs `ninja`/`coreutils`.
The output uses upstream's ad-hoc development-signing alternative, not Developer ID
or Apple notarization. App+chromedriver are archived with `ditto` before artifact
upload so executable bits/symlinks survive. SHA256 and source identity accompany
the archive. Do not add the artifact to the app's core allowlist or migrate profiles
until native runtime acceptance and independent review succeed.
