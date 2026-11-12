# Ordered native patch fixture

`webgl-prefix.cc.txt` is the first70lines of `webgl_fingerprint.cc` introduced
by fingerprint-chromium148 patch011, **after patch020** adds its timing include.
Base repository: `adryfish/fingerprint-chromium` commit
`13b89eae304123f0710f2d33fd0816a2f61d7ffc`, `patches/extra/fingerprint/`.

Original copyright2024 The ungoogled-chromium Authors. BSD-3-Clause terms are
retained in [`native/chromium-148/LICENSE`](../../../native/chromium-148/LICENSE).
This is a source prefix for patch-context compatibility, not a complete C++
translation unit or a replacement for full Chromium compilation.

Regression: first hosted build43300148520 rejected one022hunk because the overlay
was generated against011 without020's `base/time/time.h`. This fixture prevents
reintroducing that ordered-series mismatch. Do not weaken patch application or
remove the timing include/function to make the overlay pass.
