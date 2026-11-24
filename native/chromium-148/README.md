# Chromium 148 native correction — build input, NOT an installed fix

Base: [`adryfish/fingerprint-chromium`](https://github.com/adryfish/fingerprint-chromium) commit
`13b89eae304123f0710f2d33fd0816a2f61d7ffc` (148.0.7778.215).
`patches/109-canvas-pixel-addressing.patch` is the unmodified diff of
[PR109](https://github.com/adryfish/fingerprint-chromium/pull/109) authored by
**woolong900**, head `96d183bab734d9d056476cd6c887d35c622a010a` (unmerged as of
2026-11-12); it targets `patches/extra/bromite/flag-fingerprinting-canvas-image-data-noise.patch`,
originally from [Bromite](https://github.com/bromite/bromite) (`csagan5` / `Slaviro`).
Upstream BSD-3-Clause notice is retained in [`LICENSE`](LICENSE).

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

The workflow checks out the [`ungoogled-software/ungoogled-chromium-macos`](https://github.com/ungoogled-software/ungoogled-chromium-macos) scaffold `6a00dfe08591a15cfb0aed0edd292ba789d9a0d7` (BSD-3-Clause)
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
`native-checkpoint-<run_id>-<run_attempt>-<chunk>`. Rolling intermediate chunk checkpoints (`1..3`) are retained for **7 days**, while accepted `EXPERIMENTAL` archives and reusable terminal checkpoints (`complete`, `verify_failed`, or `chunk 4` partial) are retained for **90 days** to allow fast incremental follow-up patches without rebuilding from scratch.
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

## Native locale and font-access coherence 027

`patches/027-macos-locale-speech-font-coherence.patch` preserves explicit `--lang` across
`RenderProcessHostImpl::AppendRendererCommandLine`, initializes Blink `LocaleController` at
startup (`CoreInitializer::Initialize` -> `LocaleController::Init`) so `Intl` default locale
agrees across Window and Worker threads without CDP overrides, and filters
`FontAccess::DidGetEnumerationResponse` through the same case-insensitive
`FontCache::IsFontFamilyHiddenByFingerprint` (`base::ToLowerASCII` /
`base::EqualsCaseInsensitiveASCII`) rule as `FontCache::GetFontPlatformData`.

Google Network Speech component extension (`ComponentLoader::AddNetworkSpeechSynthesisExtension`)
is intentionally kept disabled: `ungoogled-chromium` domain substitution rewrites its endpoint to
`.qjz9zk` and this build omits `google_api_key`, so advertising non-functional Google voices
breaks `speechSynthesis.speak()` playback and leaks a `.qjz9zk` lookup.

Because CI Run #21 (`44582421830`, commit `2c366eb`) completed in Chunk 1 without uploading a
`native-checkpoint-*` archive (and `native-checkpoint-44542144017-2-1` belongs to Run #20 Attempt
2 with superseded `026` SHA `f564b9f9...`, which `checkpoint.py` intentionally rejects), building
`027` requires a fresh `prepare` + multi-chunk CI build (`resume_run_id=""`). No `027` binary is
pinned yet: `ChromeEngine`'s pre-`027` per-page CDP `Emulation.setLocaleOverride` bridge provides
best-effort eventual recovery (`~5-20ms` `en-US` window during renderer-singleton owner swaps and
cross-origin `noopener` boot), whereas native `027` (`LocaleController::Init()`) provides continuous
zero-gap locale across all realms and popups (enforced by `verify.py`'s `noopener_popup`,
`same_origin_cross_nav_popup`, and `same_origin_close_popup` gates) and automatically retires the CDP
bridge via `LOCALE_SPEECH_FONT_027_BUNDLES`.

## Native Pixelscan flat-box and macOS core-font coherence 028

`patches/028-pixelscan-noise-font-coherence.patch` resolves both native Pixelscan
`Masking detected` triggers without disabling per-seed canvas/font protection or
touching `BUILD.gn`:

- `StaticBitmapImage::ShuffleSubchannelColorData` (`static_bitmap_image.cc`) and
  `ShuffleExtendedWebGLPixels` (`webgl_rendering_context_base.cc`) run an
  early-exit `HasMultipleValidColors` / multi-color valid-pixel check (`alpha > 0`
  and neither pure black nor pure white). Flat monochrome shapes on transparent,
  black, or white backgrounds (including Pixelscan's `256x24` `#red-solid-box`
  hash `bf9da7959d914298f9ce9e41a480fd66f76fac5c6f5e0a9b5a99b18cfc6fd997`) stay
  unperturbed, while multi-color, gradient, and anti-aliased rendering continues to
  receive deterministic per-seed noise.
- `EvaluateFingerprintFont` (`font_cache.cc`) exempts the 196 protected macOS
  system and baseline font families (`IsProtectedMacOsCoreFont`, covering all 52
  root `/System/Library/Fonts/` and `FontServices/Reserved/` families plus
  Pixelscan's 128 legacy `Supplemental/` baseline families, case-insensitive via
  `canonical_family`) when `current_os == "macos"` and
  `spoofed_platform == "macos"`, and applies a `25%` per-seed filter
  (`probability < 0.25f`) only to remaining non-baseline
  `/System/Library/Fonts/Supplemental/` and user-installed (`/Library/Fonts`,
  `~/Library/Fonts`) fonts so every seed retains full macOS system-font parity
  while preserving per-profile diversity. Cross-OS spoofing behavior is unchanged.
- `028` touches only three `.cc` files and is registered in
  `checkpoint.py` (`compatible_identity`) and `prepare.py`
  (`apply_incremental_patches`), so post-`027` checkpoints such as Run `#22`
  (`44957399598`) can resume incrementally.

Before skipping an already-applied patch prefix, `prepare.py` reverses that
prefix in a disposable copy of the touched files, newest first. This restores
overlapping contexts without treating repeatable insertion hunks as evidence of
a missing patch; a missing or inconsistent file fails with its patch label and path.
Validated checkpoint `saved_source` provenance continues to exempt recorded patches.

The core-font acceptance gate also queries an isolated browser profile with
`--disable-spoofing=font` to inventory installed protected families. Every family
in that inventory must remain visible in both seeds 3003 and 3006, including
families absent from the explicit rendering probes. It does not require all 196
names to be installed, and rejects an empty protected-family inventory.

## Native macOS renderer locale-pak coherence 029

`patches/029-macos-renderer-locale-pak-coherence.patch` fixes the macOS renderer
startup `CHECK(!loaded_locale.empty())` crash when `--lang` carries a region
subtag whose `.lproj/locale.pak` directory is unbundled (for example `de-DE`,
`vi-VN`, `ja-JP`):

- **Supported BCP-47 locale contract (`language[-Script][-REGION]`)**: accepts
  2- or 3-letter language subtags (`en`, `de`, `fil`), optional 4-letter ISO-15924
  script subtags (`zh-Hans-CN`, `zh-Hant-TW`), and either 2-letter ISO-3166 alpha-2
  regions (`de-DE`, `vi-VN`) or 3-digit UN M.49 numeric regions (`es-419`, `en-001`),
  aligned across `chrome_engine/persona.py`, `chrome_engine/geo.py`, and
  `src/utils/validation.py`.
- `RenderProcessHostImpl::AppendRendererCommandLine`
  (`render_process_host_impl.cc`) resolves `switches::kLang` through
  `l10n_util::CheckAndResolveLocale(..., CheckLocaleMode::kUseKnownLocalesList)`
  (in-memory `kPlatformLocales` lookup, zero UI-thread disk I/O) so `ResourceBundle`
  always loads an existing bundled `.lproj/locale.pak` (such as `de`, `vi`,
  `es_419`, `en`, `en_GB`, `zh_CN`, `zh_TW`), while forwarding the full requested
  BCP-47 tag via `--fingerprint-locale` (unless `--disable-spoofing=locale` is set).
- `LocaleController::Init()` (`locale_controller.cc`) prefers `--fingerprint-locale`
  over `--lang`, preserving full region/script formatting across Window, Worker,
  SharedWorker, ServiceWorker, and popups. Note: `es-419` loads `es_419.lproj/locale.pak`
  and forwards `--fingerprint-locale=es-419`, matching upstream Chromium/ICU
  `Emulation.setLocaleOverride('es-419')` (`Intl` default `es-MX`, explicit
  `Intl.NumberFormat('es-419')` -> `es-419`). Known deferred P2 nuance: 3-part
  non-default-region Chinese tags such as `zh-Hant-TW` and `zh-Hant-HK` resolve
  through Chromium `l10n_util` to `zh_CN.lproj/locale.pak` for internal Blink UI
  strings while keeping `Intl` and `navigator.languages` on `zh-Hant-TW` /
  `zh-Hant-HK` without crashing (`zh-TW` and `zh-HK` load `zh_TW.lproj` /
  `zh_HK.lproj`). Malformed locale tags such as `"not_a_locale!"` are blocked at
  RonCProfile's Python boundaries (`src/utils/validation.py`,
  `chrome_engine/persona.py`, `chrome_engine/geo.py`); strict raw-CLI BCP-47
  validation in `027` (`locale_controller.cc`) is deferred to the next C++ cycle.
- `verify.py` launches every acceptance browser with `chromium_sandbox=True`
  (matching `ChromeEngine`'s macOS runtime default and recording
  `"chromium_sandbox": true` in the report), verifies `de-DE` across all
  realms/popups plus `locale_029_bcp47` (`zh-Hans-CN`, `es-419`, `en-001`), waits
  up to `3000ms` for non-empty `speechSynthesis.getVoices()` (ignoring empty
  intermediate `voiceschanged` events), and checks host macOS
  `AVSpeechSynthesisVoice.speechVoices` parity with an explicit `NSArray` +
  `!isNil()` check (`UNAVAILABLE` maps to `None` so missing/nil lists never relax
  the gate): when the host exposes voices or cannot be probed, `localVoiceCount > 0`
  fails closed; when a headless CI runner confirms an empty `NSArray` (`0` OS
  voices), `googleVoices == []` remains strictly enforced and
  `native-acceptance.json` records `speech_voice_gate: "host_unavailable_on_runner"`.
  In `chrome_engine/engine.py`, `_has_verified_locale_speech_font_027()` rejects
  waived `host_unavailable_on_runner` manifests and verifies `028` + `027`
  semantic evidence before unlocking `LOCALE_SPEECH_FONT_027_BUNDLES`.
- **Build security/update model:** `safe_browsing_mode=0` and `enable_updater=false`
  in `args.gn` come directly from pinned `ungoogled-chromium` /
  `fingerprint-chromium` defaults to prevent unproxied Google telemetry/API key
  failures and protect bundle SHA-256 integrity (`chrome_engine/native_bundle.py`).
  Security updates are delivered exclusively via maintainer-pinned SHA-256 core
  releases (`chrome_engine/release.py` / `chrome_engine/fetch.py`).
- Only two `.cc` files are modified (no header or `BUILD.gn` edits), allowing fast
  incremental resume from post-`028` checkpoints such as Run `45217857712`.
- **Pinned `029` build (`codex/native-core-ci@2835e96`, Run `45260495371`):**
  `bundle_sha256` `c3944e94fff0473c494c866b2295390c7521f114435b5a30cfd4a506f7fa469a`
  (`binary_sha256` `ccfcd3a2504e792900e15781f09416fd18532fa047dada4b10674e3ef0c21847`)
  is pinned in `NATIVE_REALM_BUNDLES`, `APPLE_SILICON_COHERENCE_BUNDLES`, and
  `LOCALE_SPEECH_FONT_027_BUNDLES`. **Scope boundary (IPv4-only):** verified for
  IPv4 HTTP/SOCKS5 proxy egress (upstream up/down, remote DNS, GeoIP via
  `ProxyBridge`, `0` direct fallback) and local dual-stack UDP STUN (`0/0`);
  external WAN routable IPv6 proxy egress is deferred and must be verified before
  relying on this bundle on dual-stack IPv6 networks.

## Attribution & Licenses

- Native Chromium patches and the macOS build kit retain the upstream BSD-3-Clause notice in [`LICENSE`](LICENSE) (`The ungoogled-chromium Authors`).
- See [`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) for full attribution of `adryfish/fingerprint-chromium`, `ungoogled-software/ungoogled-chromium`, `ungoogled-software/ungoogled-chromium-macos`, `woolong900` (PR #109), Bromite (`csagan5` / `Slaviro`), Chromium, and Google Dawn.

