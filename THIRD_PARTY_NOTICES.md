# Third-Party Notices and Upstream Attribution

RonC Profile builds on open-source software, upstream browser engines, patches, fonts, and libraries. This file documents the upstream sources, authors, copyright notices, and licenses used by this repository.

RonC Profile's own source code and modifications remain licensed under the [MIT License](LICENSE). Third-party components retain their respective copyrights and licenses below.

---

## 1. Upstream Base Project (Fork Origin)

- **[FoxProfile](https://github.com/chienbm98/foxprofile)** by **[chienbm98](https://github.com/chienbm98)**
  - **License:** MIT License (`Copyright (c) 2026 chienbm98`)
  - **Scope:** RonC Profile is a fork of FoxProfile (branched after commit `530b993`, v2.4.0), inheriting the core architecture, profile manager, Flet desktop UI and single-file web panel foundation, [`src/assets/rail-guilloche.png`](src/assets/rail-guilloche.png), [`chrome_engine/`](chrome_engine/README.md), REST API, MCP server, and core documentation. Original git history, original author attribution, and the MIT copyright notice are preserved in [`LICENSE`](LICENSE).

---

## 2. Bundled Third-Party Code, Patches, Fixtures, and Fonts

The following third-party files or unmodified patches/fixtures are stored directly in this repository:

- **Canvas pixel-addressing patch ([`native/chromium-148/patches/109-canvas-pixel-addressing.patch`](native/chromium-148/patches/109-canvas-pixel-addressing.patch))**
  - **Source & Author:** Unmodified diff from [adryfish/fingerprint-chromium#109](https://github.com/adryfish/fingerprint-chromium/pull/109) ("Fix canvas noise pixel addressing", commit `96d183bab734d9d056476cd6c887d35c622a010a`), authored by **woolong900**.
  - **License:** BSD-3-Clause (repository license of `adryfish/fingerprint-chromium`, retained in [`native/chromium-148/LICENSE`](native/chromium-148/LICENSE)).
  - **Upstream Lineage:** Modifies `patches/extra/bromite/flag-fingerprinting-canvas-image-data-noise.patch`, which originated in **[Bromite](https://github.com/bromite/bromite)** (authored by **csagan5** with credits to **Slaviro**; the upstream Bromite repository is licensed under GPL-3.0) and is carried in `ungoogled-chromium` / `fingerprint-chromium`.
- **Native patch context fixture ([`tests/fixtures/native148/webgl-prefix.cc.txt`](tests/fixtures/native148/webgl-prefix.cc.txt))**
  - **Source & Copyright:** First 70 lines of `webgl_fingerprint.cc` from [`adryfish/fingerprint-chromium`](https://github.com/adryfish/fingerprint-chromium) commit `13b89eae304123f0710f2d33fd0816a2f61d7ffc` (`patches/extra/fingerprint/`), `Copyright 2024 The ungoogled-chromium Authors`.
  - **License:** BSD-3-Clause ([`native/chromium-148/LICENSE`](native/chromium-148/LICENSE)).
- **Be Vietnam Pro font (`src/assets/fonts/BeVietnamPro-*.ttf`)**
  - **Source & Copyright:** `Copyright 2021 The Be Vietnam Pro Project Authors` ([bettergui/BeVietnamPro](https://github.com/bettergui/BeVietnamPro)).
  - **License:** SIL Open Font License, Version 1.1 — full license text in [`src/assets/fonts/OFL-BeVietnamPro.txt`](src/assets/fonts/OFL-BeVietnamPro.txt).
- **JetBrains Mono font (`src/assets/fonts/JetBrainsMono.ttf`)**
  - **Source & Copyright:** `Copyright 2020 The JetBrains Mono Project Authors` ([JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono)).
  - **License:** SIL Open Font License, Version 1.1 — full license text in [`src/assets/fonts/OFL-JetBrainsMono.txt`](src/assets/fonts/OFL-JetBrainsMono.txt).
- **Community & documentation conventions**
  - **[Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/)** (CC BY 4.0) adapted in [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
  - **[Keep a Changelog 1.1.0](https://keepachangelog.com/)** format used in [`CHANGELOG.md`](CHANGELOG.md).

---

## 3. Upstream Browser Engines and Build Inputs (Downloaded at Runtime or Build Time)

These upstream projects are not vendored in full inside this repository; they are fetched by the user, by the application at runtime (verified by SHA-256 where pinned), or by CI build workflows:

- **[Camoufox](https://github.com/daijro/camoufox)** by **daijro**
  - **Browser Engine & Firefox Source Patches:** **MPL-2.0** — downloaded at runtime via `python -m camoufox fetch` or Core Manager (for example, the pinned macOS arm64 staging build `v158.0.1-beta.41` in [`src/services/browser/cores.py`](src/services/browser/cores.py)).
  - **Python Client (`camoufox`):** **MIT License**.
  - **Upstream Issue/PR References in Source:** [`daijro/camoufox#985`](https://github.com/daijro/camoufox/pull/985) referenced in [`src/services/browser/gpu_probe.py`](src/services/browser/gpu_probe.py); [`daijro/camoufox#737`](https://github.com/daijro/camoufox/issues/737) referenced in [`src/services/browser/search.py`](src/services/browser/search.py).
  - **GeoIP Databases (`camoufox[geoip]`):** Downloaded at runtime by `camoufox` from [`daijro/geoip-all-in-one`](https://github.com/daijro/geoip-all-in-one) in MaxMind DB (`.mmdb`) format and read via [`maxminddb`](https://github.com/maxmind/MaxMind-DB-Reader-python) (Apache-2.0).
- **[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)** by **adryfish**
  - **License:** **BSD-3-Clause** ([`native/chromium-148/LICENSE`](native/chromium-148/LICENSE)).
  - **Usage:** SHA-256 pinned runtime builds (`148.0.7778.215`) declared in [`chrome_engine/release.py`](chrome_engine/release.py), and pinned source base (`13b89eae304123f0710f2d33fd0816a2f61d7ffc`) used by [`native/chromium-148/`](native/chromium-148/README.md). Upstream issue [`adryfish/fingerprint-chromium#100`](https://github.com/adryfish/fingerprint-chromium/issues/100) is referenced in [`chrome_engine/persona.py`](chrome_engine/persona.py) and [`chrome_engine/stealth.py`](chrome_engine/stealth.py).
- **[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium)** by **The ungoogled-chromium Authors**
  - **License:** **BSD-3-Clause** (`Copyright (c) 2015-2026, The ungoogled-chromium Authors`).
  - **Usage:** Upstream base for `fingerprint-chromium` and patch series infrastructure.
- **[ungoogled-chromium-macos](https://github.com/ungoogled-software/ungoogled-chromium-macos)** by **The ungoogled-chromium Authors**
  - **License:** **BSD-3-Clause** (`Copyright 2023 The ungoogled-chromium Authors`).
  - **Usage:** Pinned macOS build scaffold (`6a00dfe08591a15cfb0aed0edd292ba789d9a0d7`) checked out by [`.github/workflows/native-chromium-chunk.yml`](.github/workflows/native-chromium-chunk.yml) and [`native/chromium-148/build.sh`](native/chromium-148/build.sh).
- **[Chromium](https://chromium.googlesource.com/chromium/src)** by **The Chromium Authors**
  - **License:** **BSD-3-Clause**.
  - **Usage:** Target Chromium release `148.0.7778.215` compiled with `fingerprint-chromium` and RonC Profile native C++ patches (`native/chromium-148/patches/022..029`).
- **[Google Dawn](https://dawn.googlesource.com/dawn)** by **The Dawn Authors**
  - **License:** **BSD-3-Clause**.
  - **Usage:** Pinned [`src/dawn/gpu_info.json`](https://dawn.googlesource.com/dawn/+/78a9030d63048d832c4b822839bffe38ad4f20e5/src/dawn/gpu_info.json) (`78a9030d63048d832c4b822839bffe38ad4f20e5`) referenced for WebGPU vendor/architecture identifiers in [`native/chromium-148/patches/022-native-gpu-realms.patch`](native/chromium-148/patches/022-native-gpu-realms.patch).

---

## 4. Direct Open-Source Dependencies

Declared in [`requirements.txt`](requirements.txt), [`requirements-dev.txt`](requirements-dev.txt), and [`package.json`](package.json):

| Component | Upstream URL | Author / Organization | License | Role |
| --- | --- | --- | --- | --- |
| `flet` | https://github.com/flet-dev/flet | Appveyor Systems Inc. | Apache-2.0 | Desktop UI (Flutter renderer) |
| `camoufox` | https://github.com/daijro/camoufox | daijro | MIT (browser MPL-2.0) | Firefox anti-detect engine client |
| `playwright` | https://github.com/microsoft/playwright-python | Microsoft Corporation | Apache-2.0 | Browser automation driver |
| `fastapi` | https://github.com/fastapi/fastapi | Sebastián Ramírez | MIT | REST API framework |
| `uvicorn` | https://github.com/encode/uvicorn | Encode / Tom Christie | BSD-3-Clause | ASGI HTTP server |
| `aiohttp` | https://github.com/aio-libs/aiohttp | aio-libs | Apache-2.0 AND MIT | Async HTTP client & loopback control server |
| `mcp` | https://github.com/modelcontextprotocol/python-sdk | Anthropic, PBC | MIT | Model Context Protocol (MCP) SDK |
| `python-dotenv` | https://github.com/theskumar/python-dotenv | Saurabh Kumar | BSD-3-Clause | `.env` configuration loader |
| `tzdata` | https://github.com/python/tzdata | Python Software Foundation / IANA | Apache-2.0 (data: Public Domain) | Timezone database (Windows/Linux) |
| `maxminddb` | https://github.com/maxmind/MaxMind-DB-Reader-python | MaxMind, Inc. | Apache-2.0 | GeoIP `.mmdb` reader (`camoufox[geoip]`) |
| `pytest` *(dev)* | https://github.com/pytest-dev/pytest | pytest-dev | MIT | Test runner |
| `ruff` *(dev)* | https://github.com/astral-sh/ruff | Astral Software Inc. | MIT | Linter & formatter |
| `httpx` *(dev)* | https://github.com/encode/httpx | Encode / Tom Christie | BSD-3-Clause | HTTP test client |
| `e2e`, `@e2e-dev/web` *(dev)* | https://github.com/tester-army/e2e | tester-army | Apache-2.0 | Agentic web E2E test runner |

---

## 5. Full BSD-3-Clause Upstream License Notices

### ungoogled-chromium / fingerprint-chromium

Also preserved in [`native/chromium-148/LICENSE`](native/chromium-148/LICENSE):

```text
BSD 3-Clause License

Copyright (c) 2015-2026, The ungoogled-chromium Authors
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### ungoogled-chromium-macos

```text
Copyright 2023 The ungoogled-chromium Authors. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

* Redistributions of source code must retain the above copyright
  notice, this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above
  copyright notice, this list of conditions and the following disclaimer
  in the documentation and/or other materials provided with the
  distribution.
* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

For full MIT license terms see [`LICENSE`](LICENSE); for full SIL Open Font License 1.1 terms see [`src/assets/fonts/OFL-BeVietnamPro.txt`](src/assets/fonts/OFL-BeVietnamPro.txt) and [`src/assets/fonts/OFL-JetBrainsMono.txt`](src/assets/fonts/OFL-JetBrainsMono.txt).

---

## 6. Trademark Notice

Third-party product, service, and platform names mentioned in this repository for compatibility, comparison, or documentation — including Firefox, Chrome, Chromium, GoLogin, GPM, MoreLogin, Multilogin, Cookie-Editor, Cloudflare, ipinfo, ip-api, Apple Silicon, macOS, Windows, and Linux — are trademarks or registered trademarks of their respective owners. Their mention does not imply endorsement, sponsorship, or affiliation.
