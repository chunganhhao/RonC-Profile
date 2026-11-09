# Handoff for Codex Review — FoxProfile Ponytail Audit Fixes (#1–#11 + Re-Review R1–R3)

## Latest remediation — 2026-11-09 (Codex implementation)

- Removed recursive all-frame/public-prototype `close` replay from `chrome_engine/stealth.py`.
- Same-origin realms share a private `WeakMap<port, owning-release-callback>` through a virtual Proxy capability keyed by a per-build `secrets.token_hex(32)` value. No window/storage property or enumerable function key is added. Native close runs first; cleanup calls the creator-bound callback directly, once.
- `patchMessagePort` tracks actual prototypes instead of WindowProxy identity, allowing new iframe realms to be patched after navigation.
- Permanent local DOM E2E: `chrome_engine/e2e/sharedworker_cleanup.py`, exercised in `chrome_engine/tests/test_sharedworker_cleanup.py`. Launches actual `ChromeEngine` with a throwaway profile and explicitly selected local stock Chrome; does not install a browser or use accounts/providers.
- R2/R3 probes: **5 passed**; eight-frame ordinary close now **1 wrapper call**, unrelated hook **0** (previously 1,863,217 calls / ~520 ms and hook 2).
- Full earlier handoff suite plus the new E2E and R3 probes: **525 passed, 33 skipped, 1 Starlette/httpx deprecation warning in 42.41s**; `git diff --check` clean.
- REA performed the declared click/wait scenario and independently inspected the page's PASS/result text: 30 closed connections, parent/child refs 0, sibling alive, unrelated hooks 0, plain close calls 1. Evidence and limitations: `artifacts/sharedworker-cleanup-fix-2026-11-09/`.
- Scope: real stock Chrome 156.0.8078.81 through ChromeEngine, not a live Flet UI/account workflow or fingerprint-chromium build validation (that binary is not installed). Counters observe tracked refs, not total process heap.
- Independent Gatekeeper verdict is recorded separately in `artifacts/sharedworker-cleanup-fix-2026-11-09/GATEKEEPER_REVIEW.md`. Earlier sections below are historical handoff evidence.

## 1. Upstream Commits/PRs & Prior Review Context Checked
Before touching any code, we inspected:
1. **Upstream `chienbm98/foxprofile` commits & PRs**:
   - `34fe877` / `d9cedd5` / `c7f66a1` (`src/services/browser/control.py::_check_url` & `src/services/browser/session.py::restorable`): Upstream explicitly restricts remote page navigation and session tab restoration to `http`/`https` (and `about:blank` in `control.py`), decoding percent-encoded schemes (`%66ile:`, `java%73cript:`) before `:` so `https://` is never prepended to a percent-encoded dangerous scheme.
   - `PR #49` & `PR #51` (`src/services/browser/fingerprint.py`, `camoufox.coherence`): Upstream coherence checks (`coherence.gpu_fits_os`, `coherence.gpu_fits_machine`, `APPLE_SILICON_PANELS`, `APPLE_SILICON_CORES`, `reset()` removing `IDENTITY_FILES = (FINGERPRINT_FILE, CHROME_PERSONA_FILE, GEO_LOCALE_FILE)`).
   - `PR #35` (`chrome_engine/persona.py` & `adryfish/fingerprint-chromium`): Upstream `fingerprint-chromium` derives `--fingerprint=<int32>` deterministically from `seed`:
     - `hardwareConcurrency = ((seed % 6) + 2) * 2`
     - `deviceMemory = [2, 4, 6, 8, 16][seed % 5]`
2. **Engram Memory & Prior Codex Batch 1–3 + Ponytail R1–R2 Review Constraints**:
   - **Batch 1 `F3` (`artifacts/review-batch1-2026-11-08/test_round2_review.py::test_f3_chrome_initial_and_engine_switch`)**:
     Requires all 5 `dropdowns[3:]` (`screen`, `cores`, `gpu`, `webrtc`, `canvas`) in `open_profile_dialog` to have `.disabled == True` when `engine == "chrome"`.
   - **Batch 2 `R2-B2-F2` & `R3-B2-F2` (`artifacts/review-batch2-2026-11-08/test_round2_review.py`, `test_round3_review.py`, `test_round4_review.py`)**:
     Requires per-profile lease acquisition (`_profile_active_users` + `bl.is_running` + `bl.start_thread`) and last-user shutdown (`bl.is_running` + `bl.stop_profile`) to be mutually serialized so a concurrent task cannot race last-user shutdown.
   - **Batch 3 `R3-B3-F2c`..`R8-B3-F1` (`artifacts/review-batch3-2026-11-08/test_round3_review.py`..`test_round8_review.py`, Engram `#7095`)**:
     Requires canonical `SharedWorker` `blob:` wrapper URLs to stay valid across sibling tabs even after the creator tab closes, `null` `WorkerOptions` to follow WebIDL empty-dictionary semantics, and same-origin `iframe.contentWindow` realms to be synchronized.
   - **Ponytail Re-Review R1 (`artifacts/review-ponytail-2026-11-08/CODEX_REVIEW.md`, Engram `#7120`)**:
     Requires live script `Blob` URLs never to be evicted by age while unrevoked (`[P1]`), closed `SharedWorker` connections to release all 5 connection references (`w`, `clientPort`, `bridgePort`, `realWorker`, `realPort`) across both sync and async append sites on `w.port.close()` and document unload (`[P2]`), and `normalize_warming_urls` to split unambiguously on whitespace/newlines (`urls.split()`) (`[P2]`).
   - **Ponytail Re-Review R2 (`artifacts/review-ponytail-r2-2026-11-08/CODEX_REVIEW_R2.md`)**:
     Requires cross-realm `MessagePort.prototype.close.call(worker.port)` (e.g. from a same-origin `iframe`) to release the owning connection's 5 retained references (`refs == 0`), while preserving native WebIDL `TypeError` receiver brand checks and leaving active sibling connections intact (`[P2]`).

---

## 2. Remediation for Codex Ponytail Re-Review Round 2 (`artifacts/review-ponytail-r2-2026-11-08/CODEX_REVIEW_R2.md`)

| # | R2 Finding | Root Cause & Fix Applied | Proof of Work |
| :- | :--- | :--- | :--- |
| **1 (`[P2]`)** | Cross-realm port closure (`iframe.contentWindow.MessagePort.prototype.close.call(w.port)`) bypassed connection cleanup (`150` retained refs after 30 closed connections) because `swPortCloseMap.get(thisArg)` only searched the invoked realm's local `WeakMap` | Extracted `patchMessagePort(realm)` (`chrome_engine/stealth.py:666-734`), applied at realm bootstrap (`patchMessagePort(globalThis)`) and inside the `HTMLIFrameElement.prototype.contentWindow` / `HTMLFrameElement.prototype.contentWindow` getter (`chrome_engine/stealth.py:456-479`). After native `Reflect.apply(target, thisArg, args)` succeeds (preserving WebIDL `TypeError` on invalid receivers), if `swPortCloseMap.get(thisArg)` misses in the current realm, `wrappedClose` (guarded against re-entrancy by `closingPorts = new WeakSet()`) forwards the idempotent close notification to `Reflect.getPrototypeOf(thisArg).close` and same-origin window realms reachable from the top-most accessible same-origin ancestor (`root` + bounded `w.frames` traversal), triggering `releaseConn(conn)` in the creator realm. Added cross-realm close + invalid receiver regression assertions alongside the same-realm close check in `tests/test_batch3_anti_cdp_stealth.py:547-587`. | `.venv/bin/pytest -o pythonpath=. --import-mode=importlib -q -s artifacts/review-ponytail-r2-2026-11-08/test_lifecycle_review.py` -> **`3 passed in 5.22s`**:<br>• `GPU {'main': 'intel', 'before': 'intel', 'after': 'intel'}`<br>• `CLOSE {'maxAfterClose': 0, 'afterEarlyClose': 0, 'brand': 'TypeError'}`<br>• `CROSS_REALM_CLOSE {'refs': 0}` |

---

## 3. Remediations for Codex Ponytail Re-Review Round 1 (`artifacts/review-ponytail-2026-11-08/CODEX_REVIEW.md`)

| # | R1 Finding | Root Cause & Fix Applied | Proof of Work |
| :- | :--- | :--- | :--- |
| **1 (`[P1]`)** | Evicting a live `Blob` URL by age (`blobMap.size >= 64`) bypassed Worker stealth when constructing a `Worker` from an older still-valid script `Blob` URL | Removed age-based eviction of live script `Blob` URLs in `chrome_engine/stealth.py` (keeping the script/text/empty MIME filter `!b.type \|\| /javascript\|ecmascript\|text\/plain/i.test(String(b.type))` so binary/image `Blob`s are ignored, and deleting entries only on `URL.revokeObjectURL`). | `test_oldest_blob_url_keeps_profile_gpu` in `test_lifecycle_review.py`: `{"main": "intel", "before": "intel", "after": "intel"}`. |
| **2 (`[P2]`)** | `SharedWorker` retention in `activeSharedRefs` remained unbounded across both sync (`w, clientPort, bridgePort`) and async (`realWorker, realPort`) append sites even after `w.port.close()` | Tracked per-connection references in `conn = { refs: [], closed: false, bridgePort, currentPort: null }` across both `constructSharedWorkerWithCoord` and `attachRealWorker` (cleaning up replaced `[currentRealWorker, currentPort]` on fallback), and released them via `MessagePort.prototype.close` proxy (both same-realm and cross-realm) and `pagehide`/`unload`. | `test_close_releases_connections_including_before_async_attach` & `test_close_via_same_origin_iframe_releases_connection` in `test_lifecycle_review.py`: `maxAfterClose=0`, `afterEarlyClose=0`, `brand='TypeError'`, `CROSS_REALM_CLOSE={'refs': 0}`. |
| **3 (`[P2]`)** | Domain-looking URL components (`?q=coffee,tea.example`, `/path;next.example`) were still split into extra visits by regex heuristic | Replaced regex delimiter guessing in `normalize_warming_urls` (`src/services/warming/scheduler.py`) with unambiguous whitespace/newline splitting (`urls.split()`). | `.venv/bin/pytest -o pythonpath=. --import-mode=importlib -q artifacts/review-ponytail-2026-11-08/test_url_review.py` -> **`3 passed in 0.01s`**. |

---

## 4. Summary of All 11 Ponytail Fixes (#1–#11)

| # | Finding | Root Cause & Fix Applied | Files Modified |
| :- | :--- | :--- | :--- |
| **#1** | `start_url` lacked scheme validation & normalization across storage, ZIP import, API, UI, and runners | Added `normalize_start_url` and `validate_start_url` in `src/utils/validation.py` (following upstream `control._check_url` percent-encoded scheme check + `http`/`https` enforcement and auto-prepending `https://` for bare hosts). Applied across `ProfileManager` (`_load_profiles`, `add_profile`, `update_profile`), `import_from_zip` (drops unsafe `start_url` in archives to `None`), FastAPI `/api/v1/profiles` (`POST`/`PATCH` also validate `webrtc_mode` and `canvas_mode`), `open_profile_dialog`, and both `src/services/browser/runner.py` & `chrome_engine/runner.py` (`session.restorable([normalize_start_url(...)])`). | `src/utils/validation.py`, `src/services/profile/manager.py`, `src/services/profile/transfer.py`, `src/api/routes/profiles.py`, `src/ui/dialogs/profile.py`, `src/services/browser/runner.py`, `chrome_engine/runner.py` |
| **#2** | `open_profile_dialog` "Pick from Pool" reset `pool_idx_state = {"idx": 0}` on every dialog open instead of picking the least-used working proxy | Wired `_on_pick_from_pool` to sort non-dead pool entries by `(counts.get(e.id, 0), 0 if e.status == "working" else 1)` and call `pool.pick_least_used(pm_inst)` on the initial pick (while cycling through least-used working proxies on repeated clicks in the same dialog). Passed `profile_manager=pm` from `src/ui/actions/profile.py`. | `src/ui/dialogs/profile.py`, `src/ui/actions/profile.py` |
| **#3** | Editing an existing Chrome profile in `open_profile_dialog` overwrote `webrtc_mode` to `"proxy"` and `canvas_mode` to `"auto"` on save | In `open_profile_dialog.on_submit`, preserved `profile.webrtc_mode if profile is not None else "proxy"` and `profile.canvas_mode if profile is not None else "auto"` when `is_chrome` is `True`, while keeping all 5 `dropdowns[3:]` disabled for Chrome so Batch 1 `test_f3_chrome_initial_and_engine_switch` passes without regression. | `src/ui/dialogs/profile.py` |
| **#4** | Selecting `"auto"` ("Auto (Real device draw)") in `screen_dropdown` snapped back to the previous fixed resolution | Marked custom screen overrides with `fp["_custom_screen"] = True` in `generate_coherent_fingerprint`; when `screen_spec == "auto"` and `fp.pop("_custom_screen", False)` is set, regenerated `screen` and `window` geometry from a fresh OS fingerprint and kept `screen_dropdown.value = "auto"` in `_rebuild_fp`. | `src/services/browser/fingerprint.py`, `src/ui/dialogs/profile.py` |
| **#5** | `normalize_warming_urls` split on raw `,` and `;`, corrupting URLs containing commas/semicolons in paths or query strings | Updated `normalize_warming_urls` in `src/services/warming/scheduler.py` to split string inputs unambiguously on whitespace/newlines (`urls.split()`), preserving both `?q=coffee,tea.example` and `/path;next.example` intact. | `src/services/warming/scheduler.py` |
| **#6** | `WarmingSchedulerService` held `self._lock` across blocking `bl.start_thread` / `bl.stop_profile`, blocking `list_tasks()` on the UI thread | Introduced a dedicated `self._lease_lock = threading.RLock()` for per-profile lease/launch/stop (`_profile_active_users` and `_profile_launched_by_warming`), decoupling task-list CRUD (`self._lock`) from blocking launcher operations while preserving 100% of `R2-B2-F2` and `R3-B2-F2` concurrency guarantees. | `src/services/warming/scheduler.py` |
| **#7** | `ProxyPoolService.assign_round_robin` called `pm.update_profile()` in a loop, rewriting `profiles.json` $N$ times | Updated `assign_round_robin` to assign `prof.proxy` across all target profiles under `pm._lock` and call `pm.save_profiles()` once at the end of the batch (with fallback to `pm.update_profile` for custom mocks). | `src/services/proxy/pool.py` |
| **#8** | `URL.createObjectURL` proxy in `chrome_engine/stealth.py` cached every binary `Blob` and `activeSharedRefs` grew unboundedly even after `w.port.close()` | Restricted `blobMap` to script-eligible `Blob`s without age-evicting live script URLs, tracked per-connection `SharedWorker` refs across both sync and async append sites, and released them via `MessagePort.prototype.close` proxy (same-realm and cross-realm) and `pagehide`/`unload`. | `chrome_engine/stealth.py` |
| **#9** | `ProfileManager.update_profile` left stale `fingerprint.json` / `chrome_persona.json` on disk when `new_os != old_os` via API/MCP (`initial_fingerprint=None`) | Captured `old_os = profile.os_type` before mutation in `update_profile`; when `new_os != old_os` and `initial_fingerprint is None`, called `fp_service.reset(target_dir)` so `fingerprint.summary()` and the next launch use the new OS cleanly. | `src/services/profile/manager.py` |
| **#10** | Duplicate dwell-loop branches in `WarmingSchedulerService.execute_task_sync` (using raw JS `window.scrollBy` instead of humanized `"scroll"`) and duplicate upsert logic in `ProxyPoolService` | Unified the single-pass and budget-loop branches in `execute_task_sync` into one loop calling `bl.control(prof_name, "scroll", {"delta_y": 380, "humanize": True}, timeout=8.0)`, and extracted `_upsert_locked` shared by `add_proxy` and `bulk_import` in `ProxyPoolService`. | `src/services/warming/scheduler.py`, `src/services/proxy/pool.py` |
| **#11** | `build_profile_card._action_buttons` computed `generate_totp(profile.two_fa_secret)` (Base32 decode + HMAC-SHA1) on every row render | Removed the redundant `generate_totp(...)` call during row construction (`two_fa_secret` is already validated via `clean_totp_secret` on save/load; TOTP is generated on click in `_copy_profile_2fa`). | `src/ui/components/profile_card.py` |

---

## 5. Verification Commands & Test Results
```bash
.venv/bin/pytest -o pythonpath=. --import-mode=importlib -q \
  tests \
  chrome_engine/tests \
  artifacts/review-batch1-2026-11-08/test_round2_review.py \
  artifacts/review-batch2-2026-11-08/test_*.py \
  artifacts/review-batch3-2026-11-08/test_*.py \
  artifacts/review-ponytail-2026-11-08/test_url_review.py \
  artifacts/review-ponytail-r2-2026-11-08/test_lifecycle_review.py
```
- **Result**: `522 passed, 33 skipped in 34.98s` (0 failures, 0 `SyntaxWarning`s, `git diff --check` clean).
- **`artifacts/review-ponytail-r2-2026-11-08/test_lifecycle_review.py`**:
  - `GPU`: `{'main': 'intel', 'before': 'intel', 'after': 'intel'}`
  - `CLOSE`: `{'maxAfterClose': 0, 'afterEarlyClose': 0, 'brand': 'TypeError'}`
  - `CROSS_REALM_CLOSE`: `{'refs': 0}`
- **Gatekeeper Subagent (`Code Reviewer`)**: **`APPROVED`** (`9aa39ff1-3891-43d1-b006-fe742012dbc0`).
