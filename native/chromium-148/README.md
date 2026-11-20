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
- Native correction022 is staged (details below), but must pass the compiled
  realm/CSP matrix before the app may retire its legacy JS shield for that exact
  platform artifact. PR109 alone does not address WebGPU or CSP.
- Keep existing production core/profile bindings unchanged until compatibility
  and independent review pass. Never substitute Chromium150: Engram #7202
  records its canvas renderer crash. No anonymity guarantee follows from this
  source-level check; the installed native findings remain OPEN.

## Manual GitHub Actions experimental build

`.github/workflows/native-chromium.yml` prepares and builds this source on CI;
local Xcode is not required. It has **workflow_dispatch only**, no push/PR build,
release publishing, production activation or signing secrets. Do not dispatch it
until the intended source changes are reviewed. Artifacts remain **EXPERIMENTAL** until the native realm matrix and independent
release review pass. Installed binaries are not changed by source preparation.

The workflow targets **default GitHub-hosted `macos-26` ARM64 only**. Its default
`phase=inspect` just records actual RAM, free disk and Xcode; it does not fetch
Chromium/toolchains, install packages or compile. `phase=build` is a separate
explicit manual choice, with a conservative operational budget of90GiB free
and7GiB RAM, Xcode26 and two-worker Ninja. These are not measured universal
Chromium minimums or a guarantee a compile fits; each job has a360-minute
timeout. The bounded checkpoint chain below preserves progress between jobs. GitHub advertises only14GB guaranteed SSD
for these runners, so measure actual free disk before requesting the full build.
No paid/larger/self-hosted label or Xcode cleanup is selected automatically.
[GitHub runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

Run43322184857 reached25671/55906 Ninja tasks before the six-hour cancellation
with one worker. A later public run43373909155 measured only7GiB RAM and was
rejected by the previous14GiB guard. The public-runner configuration now uses
two workers with one concurrent link, no ThinLTO, no debug symbols or dSYMs.
`ci-build.gn` is appended after upstream flags so these overrides actually win;
official non-debug, standalone-app behavior, PGO0 and all native patches remain.
These resource reductions accompany the7GiB preflight budget; we do not merely
remove the guard or run three full-release compiler jobs on half the RAM.

The settings are declared in pinned Chromium148
[`compiler.gni`](https://github.com/chromium/chromium/blob/148.0.7778.215/build/config/compiler/compiler.gni),
[`symbols.gni`](https://github.com/chromium/chromium/blob/148.0.7778.215/build/config/apple/symbols.gni)
and [`concurrent_links.gni`](https://github.com/chromium/chromium/blob/148.0.7778.215/build/toolchain/concurrent_links.gni).
Turning off symbols sacrifices symbolized native crash backtraces; disabling LTO
can affect binary size/runtime performance. This remains an experimental build,
not an installed release. `/usr/bin/time -l` records compiler-stage resource use;
effective `args.gn` is logged, retained on failure and included with the archive.

This is a bounded low-memory CI trial, not proof of sub-six-hour completion.
Old runs retained logs/provenance only, not object files; their missing objects
cannot be recovered. The checkpoint chain applies only to future runs. No
runtime/profile/acceptance gate is bypassed.

Pinned upstream's20-job resume chain uses Depot runners and restores the entire
source/object tree in each job. It bypasses the time limit, not peak disk/RAM;
we do not present that as proof default GitHub runners can build this core.
No local Xcode installation is needed. Standard private-repo Actions still
consume the account's compute allowance; inspection is not a promise of free
unlimited full-core compilation.

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

## Native realm correction022

`patches/022-native-gpu-realms.patch` applies to the already-patched Chromium tree,
after upstream021. It shares the existing WebGL seed selector through
`components/ungoogled/fingerprint_gpu.h`. The15source GPU entries carry matching
WebGPU vendor/architecture, using Chromium148's pinned Dawn
[`78a9030d63048d832c4b822839bffe38ad4f20e5`](https://dawn.googlesource.com/dawn/+/78a9030d63048d832c4b822839bffe38ad4f20e5/src/dawn/gpu_info.json).
This is not the old Python Random GPU table. Software fallback adapters keep their
software identity; real adapter features/limits are not invented or enlarged.
Developer-info mode is redacted while persona spoofing is active instead of
revealing real backend/driver/heaps. Linux has no upstream GPU table and remains
unsupported for this native mapping; cross-OS launch refusal remains unchanged.

Native deviceMemory is set to RonC's conservative8GiB persona policy in the
shared Navigator/WorkerNavigator mixin, matching the existing JS contract.
**Correction to older notes:** Chromium148's optional2026memory-limits rollout
can legitimately expose32GiB;8is a compatibility policy here, not a universal
claim about Chrome's maximum. See
[the pinned implementation](https://github.com/chromium/chromium/blob/148.0.7778.215/third_party/blink/common/device_memory/approximated_device_memory.cc).

`verify.py` launches the built binary with no app init script, only disposable
contexts and a loopback server. It checks Window/classic/module/shared/blob,
iframe-owned parent-Blob worker, ServiceWorker, borrowed getters, native CPU20,
memory8, console getter/global probes, solid canvas/OffscreenCanvas, emoji smoke,
SharedWorker null/default-name semantics and strict `worker-src 'self'`. A failure
prevents packaging/upload of a usable experimental binary. The existing installed
148 is a negative control, not an accepted corrected core.

The app's `Asset.native_realms` capability is **false for every currently approved
asset**. Only a reviewed, integrity-pinned platform artifact with passing native
acceptance may set it true; that retires the JS worker shim and preserves native
worker URL/CSP semantics. A release version string or arbitrary executable path
never activates this capability. No corrected asset/SHA is registered yet, so
production behavior and old protections are intentionally retained. This closes
the source integration path, not installed-runtime acceptance or all hardware
fingerprint surfaces.


## Checkpointed macOS ARM64 builds (future runs only)

The target remains one macOS ARM64 core suitable for Apple Silicon/M4; no
Windows/Linux/Intel/universal compile is added. These artifacts are core inputs,
not the RonC application DMG. No local Xcode or Codex daemon is needed.

`native-checkpoint-probe.yml` manually tests a tiny archive across two separate
standard `macos-26` jobs, verifying bytes, executable mode, mtime and an absolute
internal toolchain symlink converted to a portable relative link. It does not
fetch Chromium, install build tools, change quotas or cancel a current build.

`native-chromium.yml` calls one shared chunk workflow at most four times. Each
chunk's compile budget ends four hours after the job's first step, including
restore/setup time; the remaining two hours of the hosted six-hour limit are
reserved for stopping, serialization and transfer. On a controlled timeout,
the whole compile session (including Ninja's separate command process groups)
must stop before saving. Unexpected visible detached sessions fail closed. Genuine compiler
errors, OOM/forced termination, an uncooperative child, incompatible inputs or
invalid checkpoints fail rather than pretending a partial build is complete.
A hard runner loss can still lose work since the previous completed checkpoint.

`checkpoint.py` archives the entire prepared `build/src` (including generated
files, toolchains, `.ninja_log`, `.ninja_deps` and objects) using standard-library
tar/gzip at level1. Download caches, credentials and git checkouts are excluded.
SHA256, pinned source provenance, exact kit/GN settings, host/workspace,
Xcode/SDK, Python and Ninja identity are checked before restore. Safe extraction
rejects path/symlink escapes and existing build trees, preserving timestamps and
modes. Resume does NOT redownload/repatch/regenerate the source tree. At least
8GiB free disk remains reserved for resumed work; extraction also checks space
for the expanded archive. Changed inputs require a fresh build.

Checkpoints are **not accepted browser releases**, even if they contain partial
executables. Only completed Ninja, ad-hoc signature verification and all native
acceptance gates can produce the existing `EXPERIMENTAL` archive. Production
bindings and installed cores remain untouched.

Checkpoint artifact names are
`native-checkpoint-<run_id>-<run_attempt>-<chunk>`, retained for **one day**.
Artifacts are immutable and earlier checkpoints are NOT overwritten or deleted
while a new one uploads. Four exhausted chunks make the workflow fail with a
clear resume instruction, preserving its last checkpoint instead of silently
starting an infinite loop. To resume before expiry, manually dispatch `phase=build`
on the **exact same reviewed commit and branch** with `resume_run_id` and the
full `resume_artifact` name. Repository/run/event/workflow identity must match.
Rerunning only a later failed job is intentionally fail-closed if its previous
attempt's artifact name does not match; use these explicit resume inputs.

This uses GitHub's artifact transfer, NOT the10GB dependency-cache pool; existing
Python caches and storage limits are unchanged. Public standard runners are
required, with no private/paid/larger/self-hosted fallback. Compressed full-tree
size and transfer duration are not yet measured at real Chromium scale. The
small probe proves mechanics/access only, not large-file capacity or sub-six-hour
completion. If disk, transfer or artifact quota fails, stop and inspect; never
raise a paid quota automatically. An already-running old workflow cannot gain
checkpoint steps from a later source push and is left alone.
