"""Compiled shared-selector regression and fail-closed native acceptance checks.

The C++ boundary uses small base API stubs, not a full Blink/Chromium build.
"""

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "native/chromium-148"
PATCH = KIT / "patches/022-native-gpu-realms.patch"
spec = importlib.util.spec_from_file_location("native_verify", KIT / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def test_native_selector_compiles_and_shares_exact_seed_config(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")
    patch = PATCH.read_text()
    block = patch.split("+++ b/components/ungoogled/fingerprint_gpu.h\n", 1)[1].split(
        "diff --git", 1
    )[0]
    header = "\n".join(line[1:] for line in block.splitlines() if line.startswith("+"))
    metadata = re.findall(r'^\+    "([a-z]+)", "([a-z0-9-]+)"$', patch, re.M)
    assert len(metadata) == 15
    # Match the source WebGL PCI table to the pinned Dawn148 architecture names.
    assert metadata == [
        ("intel", "gen-12lp"),
        ("intel", "gen-12lp"),
        ("nvidia", "ampere"),
        ("intel", "gen-12lp"),
        ("intel", "gen-12lp"),
        ("amd", "gcn-5"),
        ("intel", "gen-9"),
        ("intel", "gen-12lp"),
        ("intel", "xe-lpg"),
        ("intel", "gen-12lp"),
        ("intel", "gen-9"),
        ("nvidia", "lovelace"),
        ("intel", "gen-12lp"),
        ("apple", "metal-3"),
        ("apple", "metal-3"),
    ]
    stubs = {
        "components/ungoogled/fingerprint_gpu.h": header,
        "base/command_line.h": r"""
#pragma once
#include <map>
#include <string>
namespace base {
struct CommandLine {
  std::map<std::string,std::string> values;
  static CommandLine* ForCurrentProcess() {static CommandLine c; return &c;}
  bool HasSwitch(const char* name) const {return values.count(name);}
  std::string GetSwitchValueASCII(const char* name) const {return values.at(name);}
};
}
""",
        "base/strings/string_number_conversions.h": r"""
#pragma once
#include <charconv>
#include <cstdint>
#include <string>
namespace base {inline bool StringToUint64(const std::string& s, uint64_t* value) {
return std::from_chars(s.data(),s.data()+s.size(),*value).ec==std::errc();}}
""",
        "base/strings/string_util.h": r"""
#pragma once
#include <algorithm>
#include <cctype>
#include <string>
namespace base {inline std::string ToLowerASCII(std::string s) {
std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return std::tolower(c);});return s;}}
""",
        "components/ungoogled/ungoogled_switches.h": r"""
#pragma once
namespace switches {
inline constexpr char kFingerprint[]="fingerprint";
inline constexpr char kFingerprintPlatform[]="fingerprint-platform";
inline constexpr char kDisableSpoofing[]="disable-spoofing";
}
""",
    }
    data = "#pragma once\n#include <span>\nnamespace base {using std::span;}\nnamespace ungoogled::fingerprint {\n"
    data += "struct WebGLConfig {const char* webgpu_vendor; const char* webgpu_architecture;};\n"
    for name, rows in [
        ("kWindowsWebGLConfigs", metadata[:13]),
        ("kMacOSWebGLConfigs", metadata[13:]),
    ]:
        data += (
            "inline constexpr WebGLConfig "
            + name
            + "[]={"
            + ",".join('{"' + vendor + '","' + arch + '"}' for vendor, arch in rows)
            + "};\n"
        )
    stubs["components/ungoogled/webgl_config_data.h"] = data + "}\n"
    for name, text in stubs.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    source = tmp_path / "selector.cc"
    source.write_text(r"""
#include <cassert>
#include "components/ungoogled/fingerprint_gpu.h"
using namespace ungoogled::fingerprint;
int main() {
  auto* cmd=base::CommandLine::ForCurrentProcess();
  assert(GetActiveGPUConfig()==nullptr);
  for(uint64_t seed : {0ULL,1ULL,2ULL,5ULL,11ULL,4242ULL,4294967295ULL}) {
    cmd->values={{"fingerprint",std::to_string(seed)}};
    assert(GetActiveGPUConfig()==&kWindowsWebGLConfigs[seed%13]);
    cmd->values["fingerprint-platform"]="MaCoS";
    assert(GetActiveGPUConfig()==&kMacOSWebGLConfigs[seed%2]);
    cmd->values["disable-spoofing"]="canvas,gpu";
    assert(GetActiveGPUConfig()==nullptr);
    cmd->values["disable-spoofing"]="canvas";
    assert(GetActiveGPUConfig()==&kMacOSWebGLConfigs[seed%2]);
    cmd->values["fingerprint-platform"]="linux";
    assert(GetActiveGPUConfig()==nullptr);
    cmd->values["fingerprint-platform"]="unknown";
    assert(GetActiveGPUConfig()==nullptr);
  }
}
""")
    exe = tmp_path / "selector"
    subprocess.run(
        [compiler, "-std=c++20", "-I", str(tmp_path), str(source), "-o", str(exe)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, timeout=10)


def test_overlay_applies_after_upstream_020_timing_include(tmp_path):
    relative = "third_party/blink/renderer/modules/webgl/webgl_fingerprint.cc"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    fixture = Path(__file__).with_name("fixtures") / "native148/webgl-prefix.cc.txt"
    target.write_bytes(fixture.read_bytes())
    assert '#include "base/time/time.h"' in target.read_text()
    subprocess.run(
        ["git", "apply", "--check", "--include=" + relative, str(PATCH)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def sample():
    return {
        "gpu": {"vendor": "amd", "architecture": "gcn-5", "device": "", "description": ""},
        "cpu": 20,
        "memory": 8,
        "stackReads": 0,
        "globals": [],
        "solidChanged": 0,
        "leakedFakeShadow": False,
        "textMetrics": [112.5, 0.5, 112.0],
    }


def test_complete_native_sample_passes(sample):
    verify.check_sample(sample, "amd", "gcn-5")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("gpu", None),
        ("cpu", 8),
        ("memory", 32),
        ("stackReads", 1),
        ("globals", ["__pw_leak"]),
        ("solidChanged", 2),
        ("leakedFakeShadow", True),
        ("textMetrics", None),
        ("textMetrics", [1.0, 2.0]),
        ("textMetrics", [0.0, 1.0, 2.0]),
        ("textMetrics", [float("nan"), 1.0, 2.0]),
        ("textMetrics", [True, 1.0, 2.0]),
    ],
)
def test_native_acceptance_refuses_missing_or_unprotected_realms(sample, key, value):
    sample[key] = value
    with pytest.raises(ValueError):
        verify.check_sample(sample, "amd", "gcn-5")


def test_native_acceptance_refuses_host_gpu_and_developer_strings(sample):
    sample["gpu"]["vendor"] = "apple"
    with pytest.raises(ValueError, match="GPU identity"):
        verify.check_sample(sample, "amd", "gcn-5")
    sample["gpu"]["vendor"] = "amd"
    sample["gpu"]["description"] = "host GPU description"
    with pytest.raises(ValueError, match="GPU identity"):
        verify.check_sample(sample, "amd", "gcn-5")


def test_native_patch_preserves_real_capabilities_and_redacts_developer_branch():
    patch = PATCH.read_text()
    assert "if (!fingerprint && RuntimeEnabledFeatures::WebGPUDeveloperFeaturesEnabled())" in patch
    assert "if (fingerprint && !is_fallback_adapter)" in patch
    assert "+    return 8.0f;" in patch
    assert "gpu_supported_limits.cc" not in patch and "gpu_supported_features.cc" not in patch
    assert "return ungoogled::fingerprint::GetActiveGPUConfig();" in patch
    build = (KIT / "build.sh").read_text()
    assert build.index('"$kit/verify.py"') < build.index("ditto -c -k")


def _write_026_cpp_mock_headers(tmp_path: Path, patch026: str) -> None:
    hdr_block = patch026.split("+++ b/components/ungoogled/fingerprint_display.h\n", 1)[1].split(
        "diff --git", 1
    )[0]
    display_h = "\n".join(line[1:] for line in hdr_block.splitlines() if line.startswith("+"))
    stubs = {
        "components/ungoogled/fingerprint_display.h": display_h,
        "base/command_line.h": r"""
#pragma once
#include <map>
#include <string>
namespace base {
struct CommandLine {
  std::map<std::string, std::string> values;
  static CommandLine* ForCurrentProcess() { static CommandLine* c = new CommandLine(); return c; }
  bool HasSwitch(const char* name) const { return values.count(name) > 0; }
  std::string GetSwitchValueASCII(const char* name) const {
    auto it = values.find(name);
    return it == values.end() ? std::string() : it->second;
  }
};
}
""",
        "base/strings/string_number_conversions.h": r"""
#pragma once
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
namespace base {
inline bool StringToInt(std::string_view s, int* out) {
  if (s.empty()) return false;
  try {
    size_t idx = 0;
    *out = std::stoi(std::string(s), &idx);
    return idx == s.size();
  } catch (const std::exception&) {
    return false;
  }
}
inline bool StringToUint(std::string_view s, unsigned* out) {
  if (s.empty() || s.front() == '-') return false;
  try {
    size_t idx = 0;
    unsigned long val = std::stoul(std::string(s), &idx);
    if (idx != s.size()) return false;
    *out = static_cast<unsigned>(val);
    return true;
  } catch (const std::exception&) {
    return false;
  }
}
inline bool StringToDouble(std::string_view s, double* out) {
  if (s.empty()) return false;
  try {
    size_t idx = 0;
    *out = std::stod(std::string(s), &idx);
    return idx == s.size();
  } catch (const std::exception&) {
    return false;
  }
}
}
""",
        "base/strings/string_util.h": r"""
#pragma once
#include <algorithm>
#include <cctype>
#include <string>
#include <string_view>
namespace base {
inline std::string ToLowerASCII(std::string_view s) {
  std::string out(s);
  std::transform(out.begin(), out.end(), out.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return out;
}
}
""",
        "base/strings/string_split.h": r"""
#pragma once
#include <string_view>
#include <vector>
namespace base {
enum WhitespaceHandling { TRIM_WHITESPACE };
enum SplitResult { SPLIT_WANT_NONEMPTY };
inline std::vector<std::string_view> SplitStringPiece(
    std::string_view input, std::string_view sep, WhitespaceHandling, SplitResult) {
  std::vector<std::string_view> out;
  size_t pos = 0;
  while (pos <= input.size()) {
    size_t next = input.find(sep, pos);
    std::string_view part =
        next == std::string_view::npos ? input.substr(pos) : input.substr(pos, next - pos);
    while (!part.empty() && part.front() == ' ') part.remove_prefix(1);
    while (!part.empty() && part.back() == ' ') part.remove_suffix(1);
    if (!part.empty()) out.push_back(part);
    if (next == std::string_view::npos) break;
    pos = next + sep.size();
  }
  return out;
}
}
""",
        "components/ungoogled/ungoogled_switches.h": r"""
#pragma once
namespace switches {
inline constexpr char kDisableSpoofing[] = "disable-spoofing";
inline constexpr char kFingerprintScreenWidth[] = "fingerprint-screen-width";
inline constexpr char kFingerprintScreenHeight[] = "fingerprint-screen-height";
inline constexpr char kFingerprintDeviceScaleFactor[] = "fingerprint-device-scale-factor";
inline constexpr char kFingerprintDisplayProfile[] = "fingerprint-display-profile";
}
""",
    }
    for rel, content in stubs.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_026_patch_preserves_024_025_shas_and_compiles_under_strict_cpp20(tmp_path):
    import hashlib

    compiler = shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")

    patch024 = KIT / "patches/024-webgl-readback-parity.patch"
    patch025 = KIT / "patches/025-audio-clientrects-parity.patch"
    patch026 = KIT / "patches/026-apple-silicon-device-coherence.patch"
    assert (
        hashlib.sha256(patch024.read_bytes()).hexdigest()
        == "78ff843b4586c5452b8987dc9ecea0b52b16e1ccbcdc5e472597dd77d36d21c3"
    )
    assert (
        hashlib.sha256(patch025.read_bytes()).hexdigest()
        == "9a369414029f7b3b7811d01a31d8cbfb915b381ad08876e4b39e87cf7128f881"
    )
    txt026 = patch026.read_text()

    # Verify 022 ([0..1]) + 026 ([2..12]) compose cleanly to 13 macOS Apple Silicon slots.
    txt022 = PATCH.read_text()
    mac026 = txt026.split("+++ b/components/ungoogled/webgl_config_data.h\n", 1)[1].split(
        "diff --git", 1
    )[0]
    added_mac_section = mac026.split("@@ -2076,", 1)[1]
    renderers022 = re.findall(
        r"^   // \[\d+\] ANGLE \(Apple, ANGLE Metal Renderer: (Apple [^,]+),",
        txt022,
        re.M,
    )
    renderers026 = re.findall(
        r"^\+\s*// \[\d+\] ANGLE \(Apple, ANGLE Metal Renderer: (Apple [^,]+),",
        added_mac_section,
        re.M,
    )
    renderers = renderers022 + renderers026
    assert len(renderers022) == 2
    assert len(renderers026) == 11
    assert len(renderers) == 13
    # Verify kAppleSiliconFallbackCores matches the 13 slots in order.
    cores_block = txt026.split("kAppleSiliconFallbackCores =", 1)[1].split(";", 1)[0]
    assert [int(x) for x in re.findall(r"\d+", cores_block)] == [
        10,
        8,
        8,
        8,
        10,
        12,
        12,
        14,
        10,
        12,
        16,
        16,
        8,
    ]
    assert set(renderers) == {
        "Apple M1",
        "Apple M1 Pro",
        "Apple M1 Max",
        "Apple M2",
        "Apple M2 Pro",
        "Apple M2 Max",
        "Apple M3",
        "Apple M3 Pro",
        "Apple M3 Max",
        "Apple M4",
        "Apple M4 Pro",
        "Apple M4 Max",
    }

    # Build strict C++20 base stubs (-Werror -Wunsafe-buffer-usage -Wsign-compare).
    _write_026_cpp_mock_headers(tmp_path, txt026)

    test_cc = tmp_path / "test_026.cc"
    test_cc.write_text(r"""
#include <array>
#include <cassert>
#include "components/ungoogled/fingerprint_display.h"

namespace {
constexpr unsigned kMaxTextureMaxAnisotropyExt = 0x84FF;
constexpr unsigned kMaxDrawBuffersExt = 0x8824;
constexpr unsigned kFragmentShaderDerivativeHintOes = 0x8B8B;
constexpr unsigned kMaxColorAttachmentsExt = 0x8CDF;
constexpr unsigned kUnmaskedVendorWebgl = 0x9245;
constexpr unsigned kUnmaskedRendererWebgl = 0x9246;

bool ShouldInterceptBeforeExtensionCheck(unsigned pname, bool is_webgl2) {
  if (pname == kUnmaskedVendorWebgl || pname == kUnmaskedRendererWebgl ||
      pname == kMaxTextureMaxAnisotropyExt ||
      (!is_webgl2 && (pname == kMaxDrawBuffersExt ||
                      pname == kMaxColorAttachmentsExt ||
                      pname == kFragmentShaderDerivativeHintOes))) {
    return false;
  }
  return true;
}
}  // namespace

int main() {
  using ungoogled::fingerprint::GetActiveDisplayProfile;
  using ungoogled::fingerprint::ParseDisplayProfile;
  auto* cmd = base::CommandLine::ForCurrentProcess();

  // Control: no switches -> unset profile.
  auto p0 = ParseDisplayProfile();
  assert(!p0.has_screen && !p0.has_display_profile && p0.dpr == 0.0 && p0.color_depth == 0);

  // Full MacBook Pro 14 M3 profile.
  cmd->values = {
      {"fingerprint-screen-width", "1512"},
      {"fingerprint-screen-height", "982"},
      {"fingerprint-device-scale-factor", "2"},
      {"fingerprint-display-profile",
       "avail_top=38,avail_height=874,color_depth=30,gamut=p3,internal=1,scheme=dark"},
  };
  auto p1 = ParseDisplayProfile();
  assert(p1.has_screen && p1.width == 1512 && p1.height == 982);
  assert(p1.avail_top == 38 && p1.avail_width == 1512 && p1.avail_height == 874);
  assert(p1.dpr == 2.0 && p1.color_depth == 30 && p1.gamut == 2);
  assert(p1.has_internal && p1.is_internal && p1.color_scheme == 2 && p1.has_display_profile);
  const auto& cached = GetActiveDisplayProfile();
  assert(cached.width == 1512 && cached.color_depth == 30);

  // Overflow-safe clamping & invalid color_depth=16 rejection.
  cmd->values["fingerprint-display-profile"] =
      "avail_left=100,avail_width=2147483647,avail_top=38,avail_height=2147483647,"
      "color_depth=16,gamut=srgb,scheme=light";
  auto p2 = ParseDisplayProfile();
  assert(p2.avail_left == 100 && p2.avail_width == 1512 - 100);
  assert(p2.avail_top == 38 && p2.avail_height == 982 - 38);
  assert(p2.color_depth == 0 && p2.gamut == 1 && !p2.has_internal && !p2.is_internal);
  assert(p2.color_scheme == 1);

  // --disable-spoofing=screen disables display override.
  cmd->values["disable-spoofing"] = "screen";
  auto p3 = ParseDisplayProfile();
  assert(!p3.has_screen && !p3.has_display_profile && p3.dpr == 0.0);

  // Extension-gating check matching webgl_fingerprint.cc TryGetWebGLParameter().
  assert(!ShouldInterceptBeforeExtensionCheck(kMaxTextureMaxAnisotropyExt, false));
  assert(!ShouldInterceptBeforeExtensionCheck(kMaxTextureMaxAnisotropyExt, true));
  assert(!ShouldInterceptBeforeExtensionCheck(kMaxDrawBuffersExt, false));
  assert(ShouldInterceptBeforeExtensionCheck(kMaxDrawBuffersExt, true));
  assert(!ShouldInterceptBeforeExtensionCheck(kMaxColorAttachmentsExt, false));
  assert(!ShouldInterceptBeforeExtensionCheck(kFragmentShaderDerivativeHintOes, false));
  assert(ShouldInterceptBeforeExtensionCheck(0x0D33, false));  // MAX_TEXTURE_SIZE
  return 0;
}
""")
    exe = tmp_path / "test_026"
    asm = tmp_path / "test_026.s"
    cxx_flags = [
        "-std=c++20",
        "-O2",
        "-fno-threadsafe-statics",
        "-Werror",
        "-Wexit-time-destructors",
        "-Wglobal-constructors",
        "-Wunsafe-buffer-usage",
        "-Wsign-compare",
        "-I",
        str(tmp_path),
        str(test_cc),
    ]
    subprocess.run(
        [compiler, *cxx_flags, "-o", str(exe)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run(
        [compiler, *cxx_flags, "-S", "-o", str(asm)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    asm_text = asm.read_text()
    assert "cxa_guard" not in asm_text
    assert "ZGVZN9ungoogled11fingerprint23GetActiveDisplayProfile" not in asm_text
    subprocess.run([str(exe)], check=True, capture_output=True, timeout=10)


def test_026_media_and_two_screen_topology_under_strict_cpp20(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")

    patch026 = (KIT / "patches/026-apple-silicon-device-coherence.patch").read_text()

    # Extract exact ProjectWebExposedMediaDevices + FinalizeDevicesEnumerated snippet from 026.
    mdm_chunk = patch026.split(
        "+++ b/content/browser/renderer_host/media/media_devices_manager.cc\n", 1
    )[1].split("diff --git", 1)[0]
    assert "void MediaDevicesManager::DevicesEnumerated" not in mdm_chunk
    mdm_lines = [
        line[1:] if line.startswith(("+", " ")) else ""
        for line in mdm_chunk.splitlines()
        if line.startswith(("+", " ")) and not line.startswith("+++")
    ]
    mdm_text = "\n".join(mdm_lines)
    proj_fn = mdm_text[
        mdm_text.index("void ProjectWebExposedMediaDevices(") : mdm_text.index(
            "void SendLogMessage("
        )
    ]

    # Extract exact UpdateScreenInfosImpl single-screen normalization from 026.
    sd_chunk = patch026.split(
        "+++ b/third_party/blink/renderer/modules/screen_details/screen_details.cc\n", 1
    )[1].split("diff --git", 1)[0]
    sd_added = "\n".join(line[1:] for line in sd_chunk.splitlines() if line.startswith("+"))
    end_marker = "const display::ScreenInfos& new_infos = *effective_infos;"
    sd_impl = sd_added[
        sd_added.index("display::ScreenInfos single_screen_infos;") : sd_added.index(end_marker)
        + len(end_marker)
    ]
    assert mdm_lines

    # Reconstruct full patched MediaValues::CalculateColorGamut & CalculatePreferredColorScheme
    # by applying the 026 hunks verbatim to the upstream Chromium 148 source snippet.
    mv_chunk = patch026.split("+++ b/third_party/blink/renderer/core/css/media_values.cc\n", 1)[
        1
    ].split("diff --git", 1)[0]
    upstream_mv = """ColorSpaceGamut MediaValues::CalculateColorGamut(LocalFrame* frame) {
  DCHECK(frame);
  DCHECK(frame->GetSettings());
  DCHECK(frame->GetPage());
  const MediaFeatureOverrides* overrides =
      frame->GetPage()->GetMediaFeatureOverrides();
  std::optional<ColorSpaceGamut> override_value =
      overrides ? overrides->GetColorGamut() : std::nullopt;
  return override_value.value_or(color_space_utilities::GetColorSpaceGamut(
      frame->GetPage()->GetChromeClient().GetScreenInfo(*frame)));
}

mojom::blink::PreferredColorScheme MediaValues::CalculatePreferredColorScheme(
    LocalFrame* frame) {
  DCHECK(frame);
  DCHECK(frame->GetSettings());
  DCHECK(frame->GetDocument());
  DCHECK(frame->GetPage());
  const MediaFeatureOverrides* overrides =
      frame->GetPage()->GetMediaFeatureOverrides();
  std::optional<mojom::blink::PreferredColorScheme> override_value =
      overrides ? overrides->GetPreferredColorScheme() : std::nullopt;
  if (override_value.has_value()) {
    return override_value.value();
  }

  const PreferenceOverrides* preference_overrides =
      frame->GetPage()->GetPreferenceOverrides();
  std::optional<mojom::blink::PreferredColorScheme> preference_override_value =
      preference_overrides ? preference_overrides->GetPreferredColorScheme()
                           : std::nullopt;
  return preference_override_value.value_or(
      frame->GetDocument()->GetStyleEngine().GetPreferredColorScheme());
}"""
    patched_mv = upstream_mv
    for hunk in mv_chunk.split("@@ -390,", 1)[1].split("\n@@ "):
        body = hunk.splitlines()[1:]
        old_blk = "\n".join(line[1:] for line in body if line.startswith((" ", "-")))
        new_blk = "\n".join(line[1:] for line in body if line.startswith((" ", "+")))
        assert patched_mv.count(old_blk) == 1, old_blk
        patched_mv = patched_mv.replace(old_blk, new_blk, 1)

    gamut_fn = patched_mv.split("MediaValues::CalculatePreferredColorScheme", 1)[0]
    scheme_fn = patched_mv.split("MediaValues::CalculatePreferredColorScheme", 1)[1]
    assert gamut_fn.index("if (dp.gamut == 2)") < gamut_fn.index("if (override_value.has_value())")
    assert (
        scheme_fn.index("if (dp.color_scheme == 2)")
        < scheme_fn.index("GetMediaFeatureOverrides()")
        < scheme_fn.index("if (override_value.has_value())")
    )
    assert scheme_fn.index("if (dp.color_scheme == 2)") < scheme_fn.index(
        "GetPreferenceOverrides()"
    )

    _write_026_cpp_mock_headers(tmp_path, patch026)

    test_cc = tmp_path / "test_p103_p204.cc"
    test_cc.write_text(
        f"""#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <optional>
#include <string>
#include <vector>
#include "components/ungoogled/fingerprint_display.h"

namespace media {{ struct VideoCaptureControlSupport {{}}; }}
enum class ColorSpaceGamut {{ SRGB, P3 }};
namespace color_space_utilities {{
inline ColorSpaceGamut GetColorSpaceGamut(int) {{ return ColorSpaceGamut::SRGB; }}
}}
namespace mojom::blink {{ enum class PreferredColorScheme {{ kLight, kDark }}; }}
struct MediaFeatureOverrides {{
  std::optional<ColorSpaceGamut> gamut;
  std::optional<mojom::blink::PreferredColorScheme> scheme;
  std::optional<ColorSpaceGamut> GetColorGamut() const {{ return gamut; }}
  std::optional<mojom::blink::PreferredColorScheme> GetPreferredColorScheme() const {{
    return scheme;
  }}
}};
struct PreferenceOverrides {{
  std::optional<mojom::blink::PreferredColorScheme> scheme;
  std::optional<mojom::blink::PreferredColorScheme> GetPreferredColorScheme() const {{
    return scheme;
  }}
}};
struct MockSettings {{}};
struct MockStyleEngine {{
  mojom::blink::PreferredColorScheme scheme = mojom::blink::PreferredColorScheme::kLight;
  mojom::blink::PreferredColorScheme GetPreferredColorScheme() const {{ return scheme; }}
}};
struct MockChromeClient {{
  template <typename T>
  int GetScreenInfo(const T&) const {{ return 0; }}
}};
struct MockPage {{
  MockChromeClient c;
  MediaFeatureOverrides media_overrides;
  PreferenceOverrides pref_overrides;
  MockChromeClient& GetChromeClient() {{ return c; }}
  const MediaFeatureOverrides* GetMediaFeatureOverrides() const {{ return &media_overrides; }}
  const PreferenceOverrides* GetPreferenceOverrides() const {{ return &pref_overrides; }}
}};
struct MockDocument {{
  MockStyleEngine s;
  MockStyleEngine& GetStyleEngine() {{ return s; }}
}};
struct LocalFrame {{
  MockSettings settings;
  MockPage p;
  MockDocument d;
  const MockSettings* GetSettings() const {{ return &settings; }}
  MockPage* GetPage() {{ return &p; }}
  MockDocument* GetDocument() {{ return &d; }}
}};
struct MediaValues {{
  static ColorSpaceGamut CalculateColorGamut(LocalFrame* frame);
  static mojom::blink::PreferredColorScheme CalculatePreferredColorScheme(LocalFrame* frame);
}};
#define DCHECK(x) assert(x)
{patched_mv}
#undef DCHECK
namespace blink {{
namespace mojom {{ enum class FacingMode {{ kNone, kUser }}; }}
struct WebMediaDeviceInfo {{
  std::string device_id;
  std::string label;
  std::string group_id;
  mojom::FacingMode video_facing = mojom::FacingMode::kNone;
}};
using WebMediaDeviceInfoArray = std::vector<WebMediaDeviceInfo>;
}}
enum class MediaDeviceType {{ kMediaAudioInput, kMediaVideoInput, kMediaAudioOutput }};

{proj_fn}

namespace display {{
struct ScreenInfo {{
  int64_t display_id = 0;
  bool is_primary = false;
  bool is_internal = false;
  int w = 0, h = 0;
}};
struct ScreenInfos {{
  std::vector<ScreenInfo> screen_infos;
  int64_t current_display_id = 0;
  const ScreenInfo& current() const {{
    for (const auto& s : screen_infos) {{
      if (s.display_id == current_display_id) return s;
    }}
    return screen_infos[0];
  }}
}};
}}  // namespace display

static std::string MockHmac(const std::string& raw_id) {{
  return "hmac:" + raw_id;
}}

int main() {{
  const char* raw_case = std::getenv("FOXPROFILE_026_CASE");
  const std::string test_case = raw_case ? raw_case : "dark";
  auto* cmd = base::CommandLine::ForCurrentProcess();
  if (test_case == "light") {{
    cmd->values = {{
        {{"fingerprint-screen-width", "2560"}},
        {{"fingerprint-screen-height", "1440"}},
        {{"fingerprint-device-scale-factor", "2"}},
        {{"fingerprint-display-profile",
         "avail_top=25,avail_height=1345,color_depth=30,gamut=srgb,internal=0,scheme=light"}},
    }};
    LocalFrame frame;
    frame.p.media_overrides.gamut = ColorSpaceGamut::P3;
    frame.p.media_overrides.scheme = mojom::blink::PreferredColorScheme::kDark;
    frame.p.pref_overrides.scheme = mojom::blink::PreferredColorScheme::kDark;
    assert(MediaValues::CalculateColorGamut(&frame) == ColorSpaceGamut::SRGB);
    assert(MediaValues::CalculatePreferredColorScheme(&frame) ==
           mojom::blink::PreferredColorScheme::kLight);
    return 0;
  }}
  if (test_case == "unset") {{
    LocalFrame frame;
    frame.p.media_overrides.gamut = ColorSpaceGamut::P3;
    assert(MediaValues::CalculateColorGamut(&frame) == ColorSpaceGamut::P3);
    frame.p.media_overrides.gamut = std::nullopt;
    assert(MediaValues::CalculateColorGamut(&frame) == ColorSpaceGamut::SRGB);

    frame.p.media_overrides.scheme = mojom::blink::PreferredColorScheme::kDark;
    frame.p.pref_overrides.scheme = mojom::blink::PreferredColorScheme::kLight;
    assert(MediaValues::CalculatePreferredColorScheme(&frame) ==
           mojom::blink::PreferredColorScheme::kDark);
    frame.p.media_overrides.scheme = std::nullopt;
    frame.p.pref_overrides.scheme = mojom::blink::PreferredColorScheme::kDark;
    assert(MediaValues::CalculatePreferredColorScheme(&frame) ==
           mojom::blink::PreferredColorScheme::kDark);
    frame.p.pref_overrides.scheme = std::nullopt;
    assert(MediaValues::CalculatePreferredColorScheme(&frame) ==
           mojom::blink::PreferredColorScheme::kLight);
    return 0;
  }}
  cmd->values = {{
      {{"fingerprint-screen-width", "1512"}},
      {{"fingerprint-screen-height", "982"}},
      {{"fingerprint-device-scale-factor", "2"}},
      {{"fingerprint-display-profile",
       "avail_top=38,avail_height=874,color_depth=30,gamut=p3,internal=1,scheme=dark"}},
  }};
  const auto& dp = ungoogled::fingerprint::GetActiveDisplayProfile();
  assert(dp.has_screen && dp.has_internal && dp.is_internal);

  // P1-03(a): Provider has real physical camera -> operational HMAC ID resolves back to provider.
  blink::WebMediaDeviceInfoArray raw_snapshot = {{
      {{"physical-camera-uid", "Actual USB Cam", "raw-grp", blink::mojom::FacingMode::kNone}},
  }};
  blink::WebMediaDeviceInfoArray web_out = {{
      {{MockHmac("physical-camera-uid"), "Actual USB Cam", MockHmac("raw-grp"),
       blink::mojom::FacingMode::kNone}},
  }};
  ProjectWebExposedMediaDevices(MediaDeviceType::kMediaVideoInput, false, dp, &web_out);
  assert(web_out.size() == 1);
  assert(web_out[0].label == "FaceTime HD Camera");
  assert(web_out[0].device_id == MockHmac("physical-camera-uid"));
  bool resolved = false;
  for (const auto& raw : raw_snapshot) {{
    if (MockHmac(raw.device_id) == web_out[0].device_id) resolved = true;
  }}
  assert(resolved);

  // P1-03(b): Empty provider snapshot must NOT advertise un-openable fake facetime-hd-camera.
  blink::WebMediaDeviceInfoArray empty_web;
  ProjectWebExposedMediaDevices(MediaDeviceType::kMediaVideoInput, false, dp, &empty_web);
  assert(empty_web.empty());

  // P1-03(c): Desktop profile hides videoinput from web enumeration while snapshot stays untouched.
  auto desktop_dp = dp;
  desktop_dp.is_internal = false;
  blink::WebMediaDeviceInfoArray desktop_web = web_out;
  ProjectWebExposedMediaDevices(MediaDeviceType::kMediaVideoInput, false, desktop_dp, &desktop_web);
  assert(desktop_web.empty() && raw_snapshot.size() == 1);

  // P2-04: Dual-monitor raw_infos collapses to 1 ScreenInfo matching current display & isExtended=false.
  display::ScreenInfos raw_infos;
  raw_infos.screen_infos = {{
      {{10, true, true, 1512, 982}},
      {{20, false, false, 1920, 1080}},
  }};
  raw_infos.current_display_id = 20;
{sd_impl}
  assert(new_infos.screen_infos.size() == 1);
  assert(new_infos.current_display_id == 20);
  assert(new_infos.current().display_id == 20);

  // Explicit display-profile gamut/scheme wins over both MediaFeatureOverrides and PreferenceOverrides.
  LocalFrame frame;
  frame.p.media_overrides.gamut = ColorSpaceGamut::SRGB;
  frame.p.media_overrides.scheme = mojom::blink::PreferredColorScheme::kLight;
  frame.p.pref_overrides.scheme = mojom::blink::PreferredColorScheme::kLight;
  assert(MediaValues::CalculateColorGamut(&frame) == ColorSpaceGamut::P3);
  assert(MediaValues::CalculatePreferredColorScheme(&frame) ==
         mojom::blink::PreferredColorScheme::kDark);
  return 0;
}}
"""
    )
    exe = tmp_path / "test_p103_p204"
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-fno-threadsafe-statics",
            "-Werror",
            "-Wexit-time-destructors",
            "-Wglobal-constructors",
            "-Wunsafe-buffer-usage",
            "-Wsign-compare",
            "-I",
            str(tmp_path),
            str(test_cc),
            "-o",
            str(exe),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    import os

    for mode in ("dark", "light", "unset"):
        env = dict(os.environ, FOXPROFILE_026_CASE=mode)
        subprocess.run([str(exe)], env=env, check=True, capture_output=True, timeout=10)


def test_027_patch_applies_cleanly_and_compiles_under_strict_cpp20(tmp_path):
    import hashlib

    compiler = shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")

    expected_shas = {
        "022-native-gpu-realms.patch": "e197fd91382ccf39dd1f43b36b9b89042331f09021344ee7d661133e1bc3420c",
        "023-macos-antidetect-complete.patch": "98e2131461e38c290d67f929e0729cc187c741b31838c3515da1194ddbd57e4d",
        "024-webgl-readback-parity.patch": "78ff843b4586c5452b8987dc9ecea0b52b16e1ccbcdc5e472597dd77d36d21c3",
        "025-audio-clientrects-parity.patch": "9a369414029f7b3b7811d01a31d8cbfb915b381ad08876e4b39e87cf7128f881",
        "026-apple-silicon-device-coherence.patch": "558b84bbec096583a5253889a06b4c31f5c5efa31a79fde5d42e29fce908d75f",
        "109-canvas-pixel-addressing.patch": "df190001a4ab19a97acad9758a0c0dd03a800584e9b96c81c33484a5bedd6ce2",
    }
    for name, expected in expected_shas.items():
        assert hashlib.sha256((KIT / "patches" / name).read_bytes()).hexdigest() == expected

    patch027 = (KIT / "patches/027-macos-locale-speech-font-coherence.patch").read_text()

    def section(rel: str) -> str:
        marker = f"+++ b/{rel}\n"
        assert marker in patch027
        return patch027.split(marker, 1)[1].split("\ndiff --git ", 1)[0]

    def added_lines(sec: str) -> str:
        return "\n".join(
            line[1:]
            for line in sec.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

    def post_hunk_lines(sec: str) -> str:
        return "\n".join(
            line[1:]
            for line in sec.splitlines()
            if line.startswith(("+", " ")) and not line.startswith("+++")
        )

    assert "component_loader.cc" not in patch027
    rph_post = post_hunk_lines(section("content/browser/renderer_host/render_process_host_impl.cc"))
    lc_added = added_lines(
        section("third_party/blink/renderer/core/inspector/locale_controller.cc")
    )
    fc_added = added_lines(section("third_party/blink/renderer/platform/fonts/font_cache.cc"))
    fa_added = added_lines(section("third_party/blink/renderer/modules/font_access/font_access.cc"))

    assert "String::FromUtf8(requested)" in lc_added
    assert "String::FromUtf8(element.family())" in fa_added
    assert "FontCache::IsFontFamilyHiddenByFingerprint(family)" in fa_added
    assert "base::EqualsCaseInsensitiveASCII" in fc_added
    assert "base::ToLowerASCII(requested_family)" in fc_added
    assert "LocaleController::Init();" in added_lines(
        section("third_party/blink/renderer/core/core_initializer.cc")
    )

    # Extract IsBasicFont body, EvaluateFingerprintFont, and IsFontFamilyHiddenByFingerprint from font_cache.cc diff.
    basic_return = (
        "return std::any_of"
        + fc_added.split("return std::any_of", 1)[1].split("struct FingerprintFontDecision", 1)[0]
    )
    fc_fn = (
        "struct FingerprintFontDecision"
        + fc_added.split("struct FingerprintFontDecision", 1)[1]
        .split("const FingerprintFontDecision decision", 1)[0]
        .rsplit("\n", 2)[0]
    )
    lc_fn = "void LocaleController::Init()" + lc_added.split("void LocaleController::Init()", 1)[1]
    rph_start = rph_post.index("  std::string locale =")
    rph_end_marker = "command_line->AppendSwitchASCII(switches::kLang, locale);"
    rph_end = rph_post.index(rph_end_marker, rph_start) + len(rph_end_marker)
    rph_block = rph_post[rph_start:rph_end]
    fa_filter = "\n".join(
        line
        for line in fa_added.splitlines()
        if "String family" in line
        or "IsFontFamilyHiddenByFingerprint" in line
        or line.strip() in ("continue;", "}")
    )

    cpp_src = r"""#include <algorithm>
#include <cassert>
#include <cctype>
#include <cstdint>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#define IS_WIN 0
#define IS_MAC 1
#define IS_LINUX 0
#define IS_CHROMEOS 0
#define BUILDFLAG(X) X
namespace base {
struct CommandLine {
  std::map<std::string, std::string> values;
  static CommandLine* ForCurrentProcess() {
    static CommandLine* c = new CommandLine();
    return c;
  }
  bool HasSwitch(const char* k) const { return values.count(k) > 0; }
  std::string GetSwitchValueASCII(const char* k) const {
    auto it = values.find(k);
    return it == values.end() ? std::string() : it->second;
  }
  void AppendSwitchASCII(const char* k, const std::string& v) { values[k] = v; }
};
inline std::string ToLowerASCII(std::string_view s) {
  std::string out(s);
  std::transform(out.begin(), out.end(), out.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return out;
}
inline bool EqualsCaseInsensitiveASCII(std::string_view a, std::string_view b) {
  return ToLowerASCII(a) == ToLowerASCII(b);
}
struct __attribute__((capability("mutex"))) Lock {};
struct __attribute__((scoped_lockable)) AutoLock {
  explicit AutoLock(Lock& m) __attribute__((acquire_capability(m))) {}
  ~AutoLock() __attribute__((release_capability())) {}
};
}  // namespace base

namespace switches {
inline constexpr char kFingerprint[] = "fingerprint";
inline constexpr char kFingerprintPlatform[] = "fingerprint-platform";
inline constexpr char kDisableSpoofing[] = "disable-spoofing";
inline constexpr char kLang[] = "lang";
}  // namespace switches

struct AsciiWrap {
  std::string v;
  const char* data() const { return v.c_str(); }
};
struct String {
  std::string s;
  bool empty() const { return s.empty(); }
  std::string Utf8() const { return s; }
  AsciiWrap Ascii() const { return {s}; }
  static String FromUtf8(const std::string& in) { return {in}; }
  bool operator==(const String& o) const { return s == o.s; }
};
namespace icu {
struct Locale {
  std::string tag;
  explicit Locale(const char* t) : tag(t ? t : "") {}
  const char* getLanguage() const {
    if (tag.empty() || !std::isalpha(static_cast<unsigned char>(tag[0]))) return "";
    return tag.c_str();
  }
};
}  // namespace icu

static std::vector<std::string> GetPlatformFonts(const std::string& p) {
  if (p == "windows") return {"Segoe UI Variable", "Calibri"};
  if (p == "macos") return {"Menlo", "PingFang SC"};
  return {"Ubuntu"};
}
static bool IsBasicFont(const std::string& font_family) {
  static const std::set<std::string>& basic_fonts = *new std::set<std::string>{
      "-webkit-standard", "system-ui", "Arial", "Helvetica", "Helvetica Neue", "BlinkMacSystemFont"
  };
__BASIC_RETURN__
}

struct FontCache {
  static bool IsFontFamilyHiddenByFingerprint(const String& family);
};
__FC_FN__

struct LocaleController {
  base::Lock lock_;
  String embedder_locale_{"en-US"};
  String locale_override_ __attribute__((guarded_by(lock_)));
  std::vector<std::string> updates;
  void SetEmbedderLocale(const String& locale);
  void UpdateLocale(const String& l) { updates.push_back(l.s); }
  static LocaleController& instance() {
    static LocaleController* inst = new LocaleController();
    return *inst;
  }
  static void Init();
  void Reset() {
    base::AutoLock locker(lock_);
    embedder_locale_ = {"en-US"};
    locale_override_ = {};
    updates.clear();
  }
  void SetOverrideForTest(const String& l) {
    base::AutoLock locker(lock_);
    locale_override_ = l;
  }
};
__LC_FN__

struct MockBrowserClient {
  std::string GetApplicationLocale() const { return "en-US"; }
};
struct MockContentClient {
  MockBrowserClient b;
  const MockBrowserClient* browser() const { return &b; }
};
static MockContentClient* GetContentClient() {
  static MockContentClient* c = new MockContentClient();
  return c;
}

static std::string ResolveChildLang(const base::CommandLine& browser_command_line) {
  base::CommandLine child;
  base::CommandLine* command_line = &child;
__RPH_BLOCK__
  return child.GetSwitchValueASCII(switches::kLang);
}

struct ElementStub {
  std::string fam;
  const std::string& family() const { return fam; }
};
static std::vector<std::string> FilterFontAccessFamilies(
    const std::vector<std::string>& raw_families) {
  std::vector<std::string> kept;
  for (const std::string& name : raw_families) {
    ElementStub element{name};
__FA_FILTER__
    kept.push_back(family.Utf8());
  }
  return kept;
}

int main() {
  auto* cmd = base::CommandLine::ForCurrentProcess();

  // 1. RenderProcessHostImpl --lang propagation
  assert(ResolveChildLang(*cmd) == "en-US");
  cmd->values = {{"lang", "de-DE"}};
  assert(ResolveChildLang(*cmd) == "en-US");
  cmd->values = {{"fingerprint", "3"}, {"lang", "de-DE"}};
  assert(ResolveChildLang(*cmd) == "de-DE");
  cmd->values = {{"fingerprint", "3"}, {"lang", ""}};
  assert(ResolveChildLang(*cmd) == "en-US");

  // 2. LocaleController::Init + SetEmbedderLocale
  auto& lc = LocaleController::instance();
  lc.Reset();
  cmd->values = {{"lang", "de-DE"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "en-US" && lc.updates.empty());

  cmd->values = {{"fingerprint", "3"}, {"disable-spoofing", "locale"}, {"lang", "de-DE"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "en-US" && lc.updates.empty());

  cmd->values = {{"fingerprint", "3"}, {"lang", "!!!"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "en-US" && lc.updates.empty());

  cmd->values = {{"fingerprint", "3"}, {"lang", "de-DE"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "de-DE");
  assert(lc.updates == std::vector<std::string>{"de-DE"});

  // Active override stays protected while embedder_locale_ updates.
  lc.Reset();
  lc.SetOverrideForTest({"fr-FR"});
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "de-DE" && lc.updates.empty());

  // 3. FontCache::IsFontFamilyHiddenByFingerprint + case-insensitive canonicalization + FontAccess
  cmd->values.clear();
  assert(!FontCache::IsFontFamilyHiddenByFingerprint({"CustomFont1"}));
  cmd->values = {{"fingerprint", "3003"}, {"disable-spoofing", "font"}};
  assert(!FontCache::IsFontFamilyHiddenByFingerprint({"CustomFont1"}));

  cmd->values = {{"fingerprint", "3003"}, {"fingerprint-platform", "macos"}};
  assert(!FontCache::IsFontFamilyHiddenByFingerprint({"Arial"}));
  assert(!FontCache::IsFontFamilyHiddenByFingerprint({"arial"}));
  assert(!FontCache::IsFontFamilyHiddenByFingerprint({"ARIAL"}));
  assert(!FontCache::IsFontFamilyHiddenByFingerprint({"Helvetica"}));
  assert(!FontCache::IsFontFamilyHiddenByFingerprint({"helvetica"}));
  assert(IsBasicFont("blinkmacsystemfont"));

  assert(
      FontCache::IsFontFamilyHiddenByFingerprint({"Avenir Next Condensed"}) ==
      FontCache::IsFontFamilyHiddenByFingerprint({"avenir next condensed"}) &&
      FontCache::IsFontFamilyHiddenByFingerprint({"Avenir Next Condensed"}) ==
      FontCache::IsFontFamilyHiddenByFingerprint({"AVENIR NEXT CONDENSED"}));

  int hidden_count = 0;
  std::vector<bool> mask_a;
  std::vector<std::string> raw_families = {"Arial", "Helvetica"};
  for (int i = 0; i < 400; ++i) {
    std::string mixed = "Family_" + std::to_string(i);
    std::string lower = "family_" + std::to_string(i);
    std::string upper = "FAMILY_" + std::to_string(i);
    raw_families.push_back(mixed);
    bool h = FontCache::IsFontFamilyHiddenByFingerprint({mixed});
    assert(h == FontCache::IsFontFamilyHiddenByFingerprint({lower}));
    assert(h == FontCache::IsFontFamilyHiddenByFingerprint({upper}));
    if (h) hidden_count++;
    mask_a.push_back(h);
  }
  assert(hidden_count > 0 && hidden_count < 40);
  auto kept = FilterFontAccessFamilies(raw_families);
  assert(kept.size() == raw_families.size() - static_cast<size_t>(hidden_count));
  assert(std::find(kept.begin(), kept.end(), "Arial") != kept.end());

  cmd->values = {{"fingerprint", "3006"}, {"fingerprint-platform", "macos"}};
  std::vector<bool> mask_b;
  for (int i = 0; i < 400; ++i) {
    mask_b.push_back(FontCache::IsFontFamilyHiddenByFingerprint({"Family_" + std::to_string(i)}));
  }
  assert(mask_a != mask_b);

  // Cross-OS Windows platform font substitutes case-insensitively rather than hiding, non-platform fonts ~95% hidden
  cmd->values = {{"fingerprint", "3003"}, {"fingerprint-platform", "Windows"}};
  auto win_dec = EvaluateFingerprintFont("Segoe UI Variable");
  auto win_dec_lower = EvaluateFingerprintFont("segoe ui variable");
  assert(!win_dec.hide && (win_dec.substitute_family == "Menlo" || win_dec.substitute_family == "PingFang SC"));
  assert(!win_dec_lower.hide && win_dec_lower.substitute_family == win_dec.substitute_family);
  int cross_hidden = 0;
  for (int i = 0; i < 400; ++i) {
    if (FontCache::IsFontFamilyHiddenByFingerprint({"CrossFamily_" + std::to_string(i)})) {
      cross_hidden++;
    }
  }
  assert(cross_hidden > 340);
  return 0;
}
"""
    cpp_src = (
        cpp_src.replace("__BASIC_RETURN__", basic_return)
        .replace("__FC_FN__", fc_fn)
        .replace("__LC_FN__", lc_fn)
        .replace("__RPH_BLOCK__", rph_block)
        .replace("__FA_FILTER__", fa_filter)
    )

    test_cc = tmp_path / "test_027.cc"
    test_cc.write_text(cpp_src)
    exe = tmp_path / "test_027"
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-fno-threadsafe-statics",
            "-Werror",
            "-Wthread-safety",
            "-Wexit-time-destructors",
            "-Wglobal-constructors",
            "-Wunsafe-buffer-usage",
            "-Wsign-compare",
            str(test_cc),
            "-o",
            str(exe),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, timeout=10)


def test_028_patch_applies_on_post_027_and_compiles_under_strict_cpp20(tmp_path):
    import hashlib

    compiler = shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")

    expected_shas = {
        "022-native-gpu-realms.patch": "e197fd91382ccf39dd1f43b36b9b89042331f09021344ee7d661133e1bc3420c",
        "023-macos-antidetect-complete.patch": "98e2131461e38c290d67f929e0729cc187c741b31838c3515da1194ddbd57e4d",
        "024-webgl-readback-parity.patch": "78ff843b4586c5452b8987dc9ecea0b52b16e1ccbcdc5e472597dd77d36d21c3",
        "025-audio-clientrects-parity.patch": "9a369414029f7b3b7811d01a31d8cbfb915b381ad08876e4b39e87cf7128f881",
        "026-apple-silicon-device-coherence.patch": "558b84bbec096583a5253889a06b4c31f5c5efa31a79fde5d42e29fce908d75f",
        "027-macos-locale-speech-font-coherence.patch": "ec47709cbe9a96390cb78f76c329ec6d9e3636bf31f5a44812d1e0c69c2aa726",
        "109-canvas-pixel-addressing.patch": "df190001a4ab19a97acad9758a0c0dd03a800584e9b96c81c33484a5bedd6ce2",
    }
    for name, expected in expected_shas.items():
        assert hashlib.sha256((KIT / "patches" / name).read_bytes()).hexdigest() == expected

    patch024 = (KIT / "patches/024-webgl-readback-parity.patch").read_text()
    patch027 = (KIT / "patches/027-macos-locale-speech-font-coherence.patch").read_text()
    patch028 = (KIT / "patches/028-pixelscan-noise-font-coherence.patch").read_text()
    assert "BUILD.gn" not in patch028

    def section(patch_text: str, rel: str) -> str:
        marker = f"+++ b/{rel}\n"
        assert marker in patch_text
        return patch_text.split(marker, 1)[1].split("\ndiff --git ", 1)[0]

    def apply_hunks(src: str, sec: str, *, skip_first_hunk: bool = False) -> str:
        hunks = sec.split("\n@@ ")
        out = src
        cursor = 0
        start_idx = 1 if skip_first_hunk else 0
        for hunk in hunks[start_idx:]:
            body = hunk.splitlines()[1:]
            old_blk = "\n".join(line[1:] for line in body if line.startswith((" ", "-")))
            new_blk = "\n".join(line[1:] for line in body if line.startswith((" ", "+")))
            idx = out.find(old_blk, cursor)
            assert idx != -1, f"Hunk mismatch after offset {cursor}:\n{old_blk[:240]}"
            out = out[:idx] + new_blk + out[idx + len(old_blk) :]
            cursor = idx + len(new_blk)
        return out

    # 1. Reconstruct post-028 font_cache.cc helpers from 027 + 028.
    fc_027_added = "\n".join(
        line[1:]
        for line in section(
            patch027, "third_party/blink/renderer/platform/fonts/font_cache.cc"
        ).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    fc_027_fn = (
        "struct FingerprintFontDecision"
        + fc_027_added.split("struct FingerprintFontDecision", 1)[1]
        .split("const FingerprintFontDecision decision", 1)[0]
        .rsplit("\n", 2)[0]
    )
    fc_028_sec = section(patch028, "third_party/blink/renderer/platform/fonts/font_cache.cc")
    fc_028_fn = apply_hunks(fc_027_fn, fc_028_sec, skip_first_hunk=True)
    fc_basic_ret = (
        "return std::any_of"
        + fc_027_added.split("return std::any_of", 1)[1].split("struct FingerprintFontDecision", 1)[
            0
        ]
    )

    protected_fonts = set(
        re.findall(
            r'\+\s+"([^"]+)"',
            fc_028_sec.split("kProtectedMacOsCoreFonts", 1)[1].split(
                "IsProtectedMacOsCoreFontsSorted", 1
            )[0],
        )
    )
    assert len(protected_fonts) == 196
    supplemental_probes = [
        "Academy Engraved LET",
        "Galvji",
        "Grantha Sangam MN",
        "Noto Sans Batak",
        "Noto Sans NKo",
        "Noto Sans Syriac",
        "Noto Sans Tagalog",
        "Party LET",
        "STIX Two Math",
        "STIX Two Text",
    ]
    assert all(f.lower() not in protected_fonts for f in supplemental_probes)
    if sys.platform == "darwin":
        import ctypes
        import ctypes.util

        ct = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreText"))
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        ct.CTFontManagerCreateFontDescriptorsFromURL.argtypes = [ctypes.c_void_p]
        ct.CTFontManagerCreateFontDescriptorsFromURL.restype = ctypes.c_void_p
        ct.CTFontDescriptorCopyAttribute.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ct.CTFontDescriptorCopyAttribute.restype = ctypes.c_void_p
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_bool,
        ]
        cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
        k_fam_attr = cf.CFStringCreateWithCString(None, b"NSFontFamilyAttribute", 0x08000100)
        root_sys_families: set[str] = set()
        for scan_dir in (
            Path("/System/Library/Fonts"),
            Path("/System/Library/PrivateFrameworks/FontServices.framework/Resources/Reserved"),
        ):
            if not scan_dir.exists():
                continue
            for font_file in sorted(scan_dir.iterdir()):
                if font_file.is_dir():
                    continue
                raw = str(font_file).encode("utf-8")
                url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
                descs = ct.CTFontManagerCreateFontDescriptorsFromURL(url) if url else None
                if not descs:
                    continue
                for idx in range(cf.CFArrayGetCount(descs)):
                    fam_cf = ct.CTFontDescriptorCopyAttribute(
                        cf.CFArrayGetValueAtIndex(descs, idx), k_fam_attr
                    )
                    buf = ctypes.create_string_buffer(1024)
                    if fam_cf and cf.CFStringGetCString(fam_cf, buf, 1024, 0x08000100):
                        fam = buf.value.decode("utf-8")
                        if fam and not fam.startswith("."):
                            root_sys_families.add(fam.lower())
        assert root_sys_families
        assert root_sys_families <= protected_fonts, sorted(root_sys_families - protected_fonts)
        assert all(f.lower() not in root_sys_families for f in supplemental_probes)

    # 2. Extract 028 HasMultipleValidColors & post-028 ShuffleSubchannelColorData (023 + 028).
    sbi_028_sec = section(
        patch028, "third_party/blink/renderer/platform/graphics/static_bitmap_image.cc"
    )
    sbi_028_added = "\n".join(
        line[1:]
        for line in sbi_028_sec.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    has_multi_fn = (
        "static bool HasMultipleValidColors"
        + sbi_028_added.split("static bool HasMultipleValidColors", 1)[1].split(
            "  // Flat monochrome shapes", 1
        )[0]
    )
    post027_shuffle = r"""void StaticBitmapImage::ShuffleSubchannelColorData(const void *addr, const SkImageInfo& info, int srcX, int srcY) {
  auto w = info.width(), h = info.height();

  // skip null or tiny images; info.width()/height() can also be 0
  if (!addr || (w < 8) || (h < 8)) {
    return;
  }

  std::string seed_str = "0";
  const base::CommandLine* command_line = base::CommandLine::ForCurrentProcess();
  if (command_line->HasSwitch(switches::kFingerprint) &&
      (!command_line->HasSwitch(switches::kDisableSpoofing) ||
       command_line->GetSwitchValueASCII(switches::kDisableSpoofing).find("canvas") == std::string::npos)) {
      seed_str = command_line->GetSwitchValueASCII(switches::kFingerprint);
  }

  auto colorType = info.colorType();
  auto fRowBytes = info.minRowBytes(); // stride

  DLOG(INFO) << "BRM: ShuffleSubchannelColorData() w=" << w << " h=" << h << " colorType=" << colorType << " fRowBytes=" << fRowBytes;

  auto max_pixels = (w * h) / 128;
  if (max_pixels > 10) {
    max_pixels = 10;
  } else if (max_pixels < 2) {
    max_pixels = 2;
  }

  int modified_pixels = 0;
  std::unordered_set<int> processed_rows;
  std::unordered_set<int> processed_cols;

  // 扫描所有像素，找出边缘像素并直接处理
  for (int y = 0; y < h - 1 && modified_pixels < max_pixels; y++) {  // 排除最后一行
    if (processed_rows.count(y)) continue;  // 跳过已处理的行
    for (int x = 0; x < w - 1 && modified_pixels < max_pixels; x++) {  // 排除最后一列
      if (processed_cols.count(x)) continue;  // 跳过已处理的列
      bool isEdge = false;
      bool isValidColor = false;

      // 检查当前像素是否为边缘像素，并且不是纯黑或纯白
      switch (colorType) {
        case kRGBA_8888_SkColorType:
        {
          auto *current = writable_addr(uint32_t, addr, fRowBytes, x, y);
          auto *right = writable_addr(uint32_t, addr, fRowBytes, x + 1, y);
          auto *bottom = writable_addr(uint32_t, addr, fRowBytes, x, y + 1);

          auto r = SkGetPackedR32(*current);
          auto g = SkGetPackedG32(*current);
          auto b = SkGetPackedB32(*current);

          // 检查是否不是纯黑或纯白
          isValidColor = !((r == 0 && g == 0 && b == 0) || (r == 255 && g == 255 && b == 255));

          // 检查是否为边缘（与右侧或下侧像素不同）
          if (isValidColor) {
            isEdge = (*current != *right) || (*current != *bottom);
          }

          if (isEdge && isValidColor) {
            // 直接处理这个像素
            std::string pixel_key_base = seed_str + "_x" + std::to_string(x + srcX) + "_y" + std::to_string(y + srcY);
            std::string pixel_key_r = pixel_key_base + "_r";
            std::string pixel_key_g = pixel_key_base + "_g";
            std::string pixel_key_b = pixel_key_base + "_b";

            uint8_t shuffleR = std::hash<std::string>{}(pixel_key_r) & 1;
            uint8_t shuffleG = std::hash<std::string>{}(pixel_key_g) & 1;
            uint8_t shuffleB = std::hash<std::string>{}(pixel_key_b) & 1;

            auto a = SkGetPackedA32(*current);
            r = (r & ~0x1) | shuffleR;
            g = (g & ~0x1) | shuffleG;
            b = (b & ~0x1) | shuffleB;

            *current = (a << SK_A32_SHIFT) | (r << SK_R32_SHIFT) |
                      (g << SK_G32_SHIFT) | (b << SK_B32_SHIFT);
            modified_pixels++;
            processed_rows.insert(y);
            processed_cols.insert(x);
          }
        }
        break;

        case kBGRA_8888_SkColorType:
        {
          auto *current = writable_addr(uint32_t, addr, fRowBytes, x, y);
          auto *right = writable_addr(uint32_t, addr, fRowBytes, x + 1, y);
          auto *bottom = writable_addr(uint32_t, addr, fRowBytes, x, y + 1);

          auto b = SkGetPackedR32(*current);
          auto g = SkGetPackedG32(*current);
          auto r = SkGetPackedB32(*current);

          // 检查是否不是纯黑或纯白
          isValidColor = !((r == 0 && g == 0 && b == 0) || (r == 255 && g == 255 && b == 255));

          // 检查是否为边缘（与右侧或下侧像素不同）
          if (isValidColor) {
            isEdge = (*current != *right) || (*current != *bottom);
          }

          if (isEdge && isValidColor) {
            // 直接处理这个像素
            std::string pixel_key_base = seed_str + "_x" + std::to_string(x + srcX) + "_y" + std::to_string(y + srcY);
            std::string pixel_key_r = pixel_key_base + "_r";
            std::string pixel_key_g = pixel_key_base + "_g";
            std::string pixel_key_b = pixel_key_base + "_b";

            uint8_t shuffleR = std::hash<std::string>{}(pixel_key_r) & 1;
            uint8_t shuffleG = std::hash<std::string>{}(pixel_key_g) & 1;
            uint8_t shuffleB = std::hash<std::string>{}(pixel_key_b) & 1;

            auto a = SkGetPackedA32(*current);
            r = (r & ~0x1) | shuffleR;
            g = (g & ~0x1) | shuffleG;
            b = (b & ~0x1) | shuffleB;

            *current = (a << SK_BGRA_A32_SHIFT) | (r << SK_BGRA_R32_SHIFT) |
                      (g << SK_BGRA_G32_SHIFT) | (b << SK_BGRA_B32_SHIFT);
            modified_pixels++;
            processed_rows.insert(y);
            processed_cols.insert(x);
          }
        }
        break;

        case kGray_8_SkColorType:
        {
          auto *current = writable_addr(uint8_t, addr, fRowBytes, x, y);
          auto *right = writable_addr(uint8_t, addr, fRowBytes, x + 1, y);
          auto *bottom = writable_addr(uint8_t, addr, fRowBytes, x, y + 1);

          isValidColor = (*current != 0 && *current != 255);
          if (isValidColor) {
            isEdge = (*current != *right) || (*current != *bottom);
          }

          if (isEdge && isValidColor) {
            // 直接处理这个像素
            std::string pixel_key_base = seed_str + "_x" + std::to_string(x + srcX) + "_y" + std::to_string(y + srcY);
            std::string pixel_key_b = pixel_key_base + "_b";
            uint8_t shuffleB = std::hash<std::string>{}(pixel_key_b) & 1;
            *current = (*current & ~0x1) | shuffleB;
            modified_pixels++;
            processed_rows.insert(y);
            processed_cols.insert(x);
          }
        }
        break;

        case kRGB_565_SkColorType:
        {
          auto *current = writable_addr(uint16_t, addr, fRowBytes, x, y);
          auto *right = writable_addr(uint16_t, addr, fRowBytes, x + 1, y);
          auto *bottom = writable_addr(uint16_t, addr, fRowBytes, x, y + 1);

          unsigned r = SkPacked16ToR32(*current);
          unsigned g = SkPacked16ToG32(*current);
          unsigned b = SkPacked16ToB32(*current);

          isValidColor = !((r == 0 && g == 0 && b == 0) || (r == 31 && g == 63 && b == 31));
          if (isValidColor) {
            isEdge = (*current != *right) || (*current != *bottom);
          }

          if (isEdge && isValidColor) {
            // 直接处理这个像素
            std::string pixel_key_base = seed_str + "_x" + std::to_string(x + srcX) + "_y" + std::to_string(y + srcY);
            std::string pixel_key_r = pixel_key_base + "_r";
            std::string pixel_key_g = pixel_key_base + "_g";
            std::string pixel_key_b = pixel_key_base + "_b";

            uint8_t shuffleR = std::hash<std::string>{}(pixel_key_r) & 1;
            uint8_t shuffleG = std::hash<std::string>{}(pixel_key_g) & 1;
            uint8_t shuffleB = std::hash<std::string>{}(pixel_key_b) & 1;

            r = (r & ~0x1) | (shuffleR & 0x1);
            g = (g & ~0x1) | (shuffleG & 0x1);
            b = (b & ~0x1) | (shuffleB & 0x1);

            unsigned r16 = (r & SK_R16_MASK) << SK_R16_SHIFT;
            unsigned g16 = (g & SK_G16_MASK) << SK_G16_SHIFT;
            unsigned b16 = (b & SK_B16_MASK) << SK_B16_SHIFT;

            *current = r16 | g16 | b16;
            modified_pixels++;
            processed_rows.insert(y);
            processed_cols.insert(x);
          }
        }
        break;

        case kARGB_4444_SkColorType:
        {
          auto *current = writable_addr(uint16_t, addr, fRowBytes, x, y);
          auto *right = writable_addr(uint16_t, addr, fRowBytes, x + 1, y);
          auto *bottom = writable_addr(uint16_t, addr, fRowBytes, x, y + 1);

          auto r = SkGetPackedR4444(*current);
          auto g = SkGetPackedG4444(*current);
          auto b = SkGetPackedB4444(*current);

          isValidColor = !((r == 0 && g == 0 && b == 0) || (r == 15 && g == 15 && b == 15));
          if (isValidColor) {
            isEdge = (*current != *right) || (*current != *bottom);
          }

          if (isEdge && isValidColor) {
            // 直接处理这个像素
            std::string pixel_key_base = seed_str + "_x" + std::to_string(x + srcX) + "_y" + std::to_string(y + srcY);
            std::string pixel_key_r = pixel_key_base + "_r";
            std::string pixel_key_g = pixel_key_base + "_g";
            std::string pixel_key_b = pixel_key_base + "_b";

            uint8_t shuffleR = std::hash<std::string>{}(pixel_key_r) & 1;
            uint8_t shuffleG = std::hash<std::string>{}(pixel_key_g) & 1;
            uint8_t shuffleB = std::hash<std::string>{}(pixel_key_b) & 1;

            auto a = SkGetPackedA4444(*current);
            r = (r & ~0x1) | (shuffleR & 0x1);
            g = (g & ~0x1) | (shuffleG & 0x1);
            b = (b & ~0x1) | (shuffleB & 0x1);

            unsigned a4 = (a & 0xF) << SK_A4444_SHIFT;
            unsigned r4 = (r & 0xF) << SK_R4444_SHIFT;
            unsigned g4 = (g & 0xF) << SK_G4444_SHIFT;
            unsigned b4 = (b & 0xF) << SK_B4444_SHIFT;

            *current = r4 | b4 | g4 | a4;
            modified_pixels++;
            processed_rows.insert(y);
            processed_cols.insert(x);
          }
        }
        break;

        default:
          break;
      }
    }
  }
}"""
    # Skip hunks[0] (includes) and hunks[1] (HasMultipleValidColors, already extracted above).
    sbi_028_shuffle_hunks = "\n@@ ".join(sbi_028_sec.split("\n@@ ")[2:])
    shuffle_fn = apply_hunks(post027_shuffle, sbi_028_shuffle_hunks)

    # 3. Extract ShuffleExtendedWebGLPixels from 024 + apply 028 webgl hunks.
    wgl_024_added = "\n".join(
        line[1:]
        for line in section(
            patch024, "third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc"
        ).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    wgl_024_ext = wgl_024_added[
        wgl_024_added.index("float HalfToFloat(") : wgl_024_added.index("\n}  // namespace")
    ]
    wgl_028_sec = section(
        patch028, "third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc"
    )
    wgl_ext_fn = apply_hunks(wgl_024_ext, wgl_028_sec, skip_first_hunk=True)

    cpp_src = r"""#include <algorithm>
#include <array>
#include <cassert>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <unordered_set>
#include <utility>
#include <vector>

#define IS_WIN 0
#define IS_MAC 1
#define IS_LINUX 0
#define IS_CHROMEOS 0
#define BUILDFLAG(X) X
#define UNSAFE_BUFFERS(...) \
  _Pragma("clang unsafe_buffer_usage begin") __VA_ARGS__ _Pragma("clang unsafe_buffer_usage end")
#define UNSAFE_TODO(...) UNSAFE_BUFFERS(__VA_ARGS__)
struct NullLogStream {
  template <typename T>
  NullLogStream& operator<<(const T&) {
    return *this;
  }
};
#define DLOG(X) NullLogStream()

namespace base {
template <typename Dest, typename Source>
Dest bit_cast(const Source& source) {
  static_assert(sizeof(Dest) == sizeof(Source));
  Dest dest;
  UNSAFE_TODO(std::memcpy(&dest, &source, sizeof(Dest)));
  return dest;
}
struct CommandLine {
  std::map<std::string, std::string> values;
  static CommandLine* ForCurrentProcess() {
    static CommandLine* c = new CommandLine();
    return c;
  }
  bool HasSwitch(const char* k) const { return values.count(k) > 0; }
  std::string GetSwitchValueASCII(const char* k) const {
    auto it = values.find(k);
    return it == values.end() ? std::string() : it->second;
  }
};
inline std::string ToLowerASCII(std::string_view s) {
  std::string out(s);
  std::transform(out.begin(), out.end(), out.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return out;
}
inline bool EqualsCaseInsensitiveASCII(std::string_view a, std::string_view b) {
  return ToLowerASCII(a) == ToLowerASCII(b);
}
}  // namespace base

namespace switches {
inline constexpr char kFingerprint[] = "fingerprint";
inline constexpr char kFingerprintPlatform[] = "fingerprint-platform";
inline constexpr char kDisableSpoofing[] = "disable-spoofing";
}  // namespace switches

struct String {
  std::string s;
  std::string Utf8() const { return s; }
};

static std::vector<std::string> GetPlatformFonts(const std::string& p) {
  if (p == "windows") return {"Segoe UI Variable", "Calibri"};
  if (p == "macos") return {"Menlo", "PingFang SC"};
  return {"Ubuntu"};
}
static bool IsBasicFont(const std::string& font_family) {
  static const std::set<std::string>& basic_fonts = *new std::set<std::string>{
      "-webkit-standard", "system-ui", "Arial", "Helvetica", "Helvetica Neue", "BlinkMacSystemFont"
  };
__BASIC_RETURN__
}

struct FontCache {
  static bool IsFontFamilyHiddenByFingerprint(const String& family);
};
__FC_028__

enum SkColorType {
  kRGBA_8888_SkColorType,
  kBGRA_8888_SkColorType,
  kGray_8_SkColorType,
  kRGB_565_SkColorType,
  kARGB_4444_SkColorType,
};

struct SkImageInfo {
  int w = 0;
  int h = 0;
  SkColorType ct = kRGBA_8888_SkColorType;
  int width() const { return w; }
  int height() const { return h; }
  SkColorType colorType() const { return ct; }
  size_t bytesPerPixel() const {
    if (ct == kRGBA_8888_SkColorType || ct == kBGRA_8888_SkColorType) return 4;
    if (ct == kRGB_565_SkColorType || ct == kARGB_4444_SkColorType) return 2;
    return 1;
  }
  size_t minRowBytes() const { return static_cast<size_t>(w) * bytesPerPixel(); }
};

constexpr unsigned SK_R32_SHIFT = 0;
constexpr unsigned SK_G32_SHIFT = 8;
constexpr unsigned SK_B32_SHIFT = 16;
constexpr unsigned SK_A32_SHIFT = 24;
constexpr unsigned SK_BGRA_B32_SHIFT = 0;
constexpr unsigned SK_BGRA_G32_SHIFT = 8;
constexpr unsigned SK_BGRA_R32_SHIFT = 16;
constexpr unsigned SK_BGRA_A32_SHIFT = 24;
inline uint32_t SkGetPackedR32(uint32_t p) { return (p >> SK_R32_SHIFT) & 0xFFu; }
inline uint32_t SkGetPackedG32(uint32_t p) { return (p >> SK_G32_SHIFT) & 0xFFu; }
inline uint32_t SkGetPackedB32(uint32_t p) { return (p >> SK_B32_SHIFT) & 0xFFu; }
inline uint32_t SkGetPackedA32(uint32_t p) { return (p >> SK_A32_SHIFT) & 0xFFu; }

constexpr unsigned SK_R16_MASK = 0x1F;
constexpr unsigned SK_G16_MASK = 0x3F;
constexpr unsigned SK_B16_MASK = 0x1F;
constexpr unsigned SK_R16_SHIFT = 11;
constexpr unsigned SK_G16_SHIFT = 5;
constexpr unsigned SK_B16_SHIFT = 0;
inline unsigned SkGetPackedR16(uint16_t p) { return (p >> SK_R16_SHIFT) & SK_R16_MASK; }
inline unsigned SkGetPackedG16(uint16_t p) { return (p >> SK_G16_SHIFT) & SK_G16_MASK; }
inline unsigned SkGetPackedB16(uint16_t p) { return (p >> SK_B16_SHIFT) & SK_B16_MASK; }
inline unsigned SkPacked16ToR32(uint16_t p) {
  unsigned r = SkGetPackedR16(p);
  return (r << 3) | (r >> 2);
}
inline unsigned SkPacked16ToG32(uint16_t p) {
  unsigned g = SkGetPackedG16(p);
  return (g << 2) | (g >> 4);
}
inline unsigned SkPacked16ToB32(uint16_t p) {
  unsigned b = SkGetPackedB16(p);
  return (b << 3) | (b >> 2);
}

constexpr unsigned SK_R4444_SHIFT = 12;
constexpr unsigned SK_G4444_SHIFT = 8;
constexpr unsigned SK_B4444_SHIFT = 4;
constexpr unsigned SK_A4444_SHIFT = 0;
inline unsigned SkGetPackedR4444(uint16_t p) { return (p >> SK_R4444_SHIFT) & 0xFu; }
inline unsigned SkGetPackedG4444(uint16_t p) { return (p >> SK_G4444_SHIFT) & 0xFu; }
inline unsigned SkGetPackedB4444(uint16_t p) { return (p >> SK_B4444_SHIFT) & 0xFu; }
inline unsigned SkGetPackedA4444(uint16_t p) { return (p >> SK_A4444_SHIFT) & 0xFu; }

__HAS_MULTI_FN__

#define writable_addr(T, p, stride, x, y) \
  UNSAFE_TODO(reinterpret_cast<T*>(const_cast<char*>(reinterpret_cast<const char*>(p)) + (y) * (stride) + (x) * sizeof(T)))

struct StaticBitmapImage {
  static void ShuffleSubchannelColorData(const void* addr, const SkImageInfo& info, int srcX, int srcY);
};
__SHUFFLE_FN__
#undef writable_addr

using GLenum = unsigned int;
constexpr GLenum GL_UNSIGNED_SHORT_5_5_5_1 = 0x8034;
constexpr GLenum GL_FLOAT = 0x1406;
constexpr GLenum GL_HALF_FLOAT = 0x140B;
__WGL_EXT_FN__

static uint32_t PackRGBA(uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
  return (static_cast<uint32_t>(a) << 24) | (static_cast<uint32_t>(b) << 16) |
         (static_cast<uint32_t>(g) << 8) | static_cast<uint32_t>(r);
}

int main() {
  auto* cmd = base::CommandLine::ForCurrentProcess();

  // 1. All 196 protected macOS core/system fonts stay visible across Pixelscan & verify seeds.
  const std::vector<std::string> core_probes = {
      "Euphemia UCAS", "euphemia ucas", "EUPHEMIA UCAS",
      "Al Bayan", "Avenir Next", "Avenir Next Condensed",
      "Courier", "Hiragino Kaku Gothic Pro", "Hiragino Kaku Gothic ProN",
      "Hiragino Kaku Gothic Std", "Kohinoor Gujarati", "Menlo", "Monaco",
      "Mukta Mahee", "Noto Sans Armenian", "Noto Sans Kannada",
      "Noto Sans Myanmar", "Noto Sans Oriya", "Noto Sans Zawgyi",
      "Noto Serif Myanmar", "Optima", "PingFang HK", "PingFang MO",
      "PingFang SC", "PingFang TC", "System Font", "Times New Roman",
      "Wingdings", "Wingdings 2", "Zapfino",
  };
  for (const std::string& seed : {"2044486887", "3003", "3006", "3", "9", "42"}) {
    cmd->values = {{"fingerprint", seed}, {"fingerprint-platform", "macos"}};
    for (const auto& entry : kProtectedMacOsCoreFonts) {
      const auto d = EvaluateFingerprintFont(std::string(entry));
      assert(!d.hide && d.substitute_family.empty());
    }
    for (const auto& probe : core_probes) {
      assert(!FontCache::IsFontFamilyHiddenByFingerprint({probe}));
    }
  }

  // 2. Non-core macOS supplemental fonts still vary per seed (3003 vs 3006) case-insensitively.
  const std::vector<std::string> supplemental = {
      "Academy Engraved LET", "Galvji", "Grantha Sangam MN",
      "Noto Sans Batak", "Noto Sans NKo", "Noto Sans Syriac",
      "Noto Sans Tagalog", "Party LET", "STIX Two Math", "STIX Two Text",
  };
  auto eval_mask = [&](const std::string& seed) {
    cmd->values = {{"fingerprint", seed}, {"fingerprint-platform", "macos"}};
    std::vector<bool> mask;
    for (const auto& fam : supplemental) {
      const bool h = FontCache::IsFontFamilyHiddenByFingerprint({fam});
      assert(h == FontCache::IsFontFamilyHiddenByFingerprint({base::ToLowerASCII(fam)}));
      std::string upper = fam;
      std::transform(upper.begin(), upper.end(), upper.begin(),
                     [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
      assert(h == FontCache::IsFontFamilyHiddenByFingerprint({upper}));
      mask.push_back(h);
    }
    return mask;
  };
  const auto m3003 = eval_mask("3003");
  const auto m3006 = eval_mask("3006");
  assert(std::count(m3003.begin(), m3003.end(), true) >= 1);
  assert(std::count(m3006.begin(), m3006.end(), true) >= 1);
  assert(m3003 != m3006);

  // 3. Cross-OS spoofing (macOS host pretending Windows) remains unchanged (~95% non-Windows hidden).
  cmd->values = {{"fingerprint", "3003"}, {"fingerprint-platform", "windows"}};
  auto win_dec = EvaluateFingerprintFont("Segoe UI Variable");
  assert(!win_dec.hide && (win_dec.substitute_family == "Menlo" || win_dec.substitute_family == "PingFang SC"));
  int cross_hidden = 0;
  for (int i = 0; i < 400; ++i) {
    if (FontCache::IsFontFamilyHiddenByFingerprint({"CrossFamily_" + std::to_string(i)})) {
      cross_hidden++;
    }
  }
  assert(cross_hidden > 340);

  // 4. Flat monochrome Pixelscan 256x24 red box (red over transparent), plus red over white/black, stay intact.
  cmd->values = {{"fingerprint", "2044486887"}, {"fingerprint-platform", "macos"}};
  auto test_flat_rgba = [&](uint32_t bg, uint32_t fg) {
    std::vector<uint32_t> buf(256 * 24, bg);
    for (int y = 3; y < 21; ++y) {
      for (int x = 32; x < 224; ++x) {
        buf[static_cast<size_t>(y) * 256 + static_cast<size_t>(x)] = fg;
      }
    }
    const auto orig = buf;
    StaticBitmapImage::ShuffleSubchannelColorData(
        buf.data(), SkImageInfo{256, 24, kRGBA_8888_SkColorType}, 0, 0);
    assert(buf == orig);
  };
  test_flat_rgba(PackRGBA(0, 0, 0, 0), PackRGBA(255, 0, 0, 255));
  test_flat_rgba(PackRGBA(255, 255, 255, 255), PackRGBA(255, 0, 0, 255));
  test_flat_rgba(PackRGBA(0, 0, 0, 255), PackRGBA(255, 0, 0, 255));
  test_flat_rgba(PackRGBA(120, 45, 200, 0), PackRGBA(255, 0, 0, 255));  // transparent non-zero RGB
  test_flat_rgba(PackRGBA(0, 0, 0, 0), PackRGBA(255, 0, 0, 128));      // flat translucent red

  // Flat 16-bit 565 / 4444 / Gray8 stay untouched.
  {
    std::vector<uint8_t> g8(32 * 16, 0);
    for (int y = 2; y < 14; ++y) {
      for (int x = 4; x < 28; ++x) g8[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = 128;
    }
    const auto g8_orig = g8;
    StaticBitmapImage::ShuffleSubchannelColorData(g8.data(), SkImageInfo{32, 16, kGray_8_SkColorType}, 0, 0);
    assert(g8 == g8_orig);

    for (const std::string& seed : {"2044486887", "2"}) {
      cmd->values["fingerprint"] = seed;
      for (uint16_t bg565 : {uint16_t{0x0000}, uint16_t{0xFFFF}}) {
        std::vector<uint16_t> p565(32 * 16, bg565);
        for (int y = 2; y < 14; ++y) {
          for (int x = 4; x < 28; ++x) p565[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = 0xF800;
        }
        const auto p565_orig = p565;
        StaticBitmapImage::ShuffleSubchannelColorData(p565.data(), SkImageInfo{32, 16, kRGB_565_SkColorType}, 0, 0);
        assert(p565 == p565_orig);
      }
    }
    auto run_mixed_565 = [&](const std::string& seed) {
      cmd->values["fingerprint"] = seed;
      std::vector<uint16_t> buf(32 * 16, 0xFFFF);
      for (int y = 2; y < 14; ++y) {
        for (int x = 4; x < 28; ++x) buf[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = 0xF800;
      }
      for (int y = 5; y < 11; ++y) {
        for (int x = 10; x < 22; ++x) buf[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = 0x0410;
      }
      buf[0] = 0x0000;
      const auto orig = buf;
      StaticBitmapImage::ShuffleSubchannelColorData(buf.data(), SkImageInfo{32, 16, kRGB_565_SkColorType}, 0, 0);
      assert(buf != orig);
      for (size_t i = 0; i < buf.size(); ++i) {
        if (orig[i] == 0xFFFF || orig[i] == 0x0000) {
          assert(buf[i] == orig[i]);
        } else if (buf[i] != orig[i]) {
          const int dr = static_cast<int>(SkGetPackedR16(buf[i])) - static_cast<int>(SkGetPackedR16(orig[i]));
          const int dg = static_cast<int>(SkGetPackedG16(buf[i])) - static_cast<int>(SkGetPackedG16(orig[i]));
          const int db = static_cast<int>(SkGetPackedB16(buf[i])) - static_cast<int>(SkGetPackedB16(orig[i]));
          assert(dr >= -1 && dr <= 1 && dg >= -1 && dg <= 1 && db >= -1 && db <= 1);
        }
      }
      return buf;
    };
    assert(run_mixed_565("2") != run_mixed_565("3"));
    cmd->values["fingerprint"] = "2044486887";

    std::vector<uint16_t> p4444(32 * 16, 0x7320);  // alpha == 0 with non-zero RGB
    for (int y = 2; y < 14; ++y) {
      for (int x = 4; x < 28; ++x) p4444[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = 0xF00F;
    }
    const auto p4444_orig = p4444;
    StaticBitmapImage::ShuffleSubchannelColorData(p4444.data(), SkImageInfo{32, 16, kARGB_4444_SkColorType}, 0, 0);
    assert(p4444 == p4444_orig);
  }

  // 5. Multi-color scenes (2+ valid colors) still get deterministic per-seed noise across seeds.
  auto run_multicolor_rgba = [&](const std::string& seed) {
    cmd->values["fingerprint"] = seed;
    std::vector<uint32_t> buf(32 * 32, PackRGBA(76, 153, 230, 255));
    for (int y = 0; y < 32; ++y) {
      for (int x = 0; x < 16; ++x) buf[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = PackRGBA(204, 51, 102, 255);
    }
    for (int y = 8; y < 24; ++y) {
      for (int x = 8; x < 24; ++x) buf[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = PackRGBA(51, 191, 115, 255);
    }
    const auto before = buf;
    StaticBitmapImage::ShuffleSubchannelColorData(buf.data(), SkImageInfo{32, 32, kRGBA_8888_SkColorType}, 0, 0);
    assert(buf != before);
    return buf;
  };
  assert(run_multicolor_rgba("2") != run_multicolor_rgba("3"));

  // 6. ShuffleExtendedWebGLPixels preserves flat 5551/FLOAT/HALF_FLOAT boxes while noising 2-color & gradients.
  {
    std::vector<uint16_t> flat5551(32 * 32, 0);
    for (int y = 4; y < 24; ++y) {
      for (int x = 4; x < 24; ++x) flat5551[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = 0xF801;
    }
    const auto flat5551_orig = flat5551;
    ShuffleExtendedWebGLPixels(reinterpret_cast<uint8_t*>(flat5551.data()), 32, 32, 64, 2, GL_UNSIGNED_SHORT_5_5_5_1, "2");
    assert(flat5551 == flat5551_orig);

    auto make_two_5551 = [&](const std::string& seed) {
      std::vector<uint16_t> buf(32 * 32, 0x4221);
      for (int y = 4; y < 24; ++y) {
        for (int x = 4; x < 24; ++x) buf[static_cast<size_t>(y) * 32 + static_cast<size_t>(x)] = 0xC111;
      }
      const auto orig = buf;
      ShuffleExtendedWebGLPixels(reinterpret_cast<uint8_t*>(buf.data()), 32, 32, 64, 2, GL_UNSIGNED_SHORT_5_5_5_1, seed);
      assert(buf != orig);
      return buf;
    };
    assert(make_two_5551("2") != make_two_5551("3"));

    std::vector<float> flat_flt(32 * 32 * 4, 0.0f);
    for (int y = 4; y < 24; ++y) {
      for (int x = 4; x < 24; ++x) {
        const size_t off = (static_cast<size_t>(y) * 32 + static_cast<size_t>(x)) * 4;
        flat_flt[off] = 1.0f;
        flat_flt[off + 3] = 1.0f;
      }
    }
    const auto flat_flt_orig = flat_flt;
    ShuffleExtendedWebGLPixels(reinterpret_cast<uint8_t*>(flat_flt.data()), 32, 32, 32 * 16, 16, GL_FLOAT, "2");
    assert(flat_flt == flat_flt_orig);

    auto make_grad_flt = [&](const std::string& seed) {
      std::vector<float> buf(32 * 32 * 4, 0.0f);
      for (int y = 0; y < 32; ++y) {
        for (int x = 0; x < 32; ++x) {
          const size_t off = (static_cast<size_t>(y) * 32 + static_cast<size_t>(x)) * 4;
          buf[off] = 0.2f + 0.6f * static_cast<float>(x) / 31.0f;
          buf[off + 1] = 0.3f + 0.5f * static_cast<float>(y) / 31.0f;
          buf[off + 2] = 0.7f;
          buf[off + 3] = 1.0f;
        }
      }
      const auto orig = buf;
      ShuffleExtendedWebGLPixels(reinterpret_cast<uint8_t*>(buf.data()), 32, 32, 32 * 16, 16, GL_FLOAT, seed);
      assert(buf != orig);
      return buf;
    };
    assert(make_grad_flt("2") != make_grad_flt("3"));

    std::vector<uint16_t> flat_hf(32 * 32 * 4, 0);
    for (int y = 4; y < 24; ++y) {
      for (int x = 4; x < 24; ++x) {
        const size_t off = (static_cast<size_t>(y) * 32 + static_cast<size_t>(x)) * 4;
        flat_hf[off] = FloatToHalf(0.5f);
        flat_hf[off + 3] = FloatToHalf(1.0f);
      }
    }
    const auto flat_hf_orig = flat_hf;
    ShuffleExtendedWebGLPixels(reinterpret_cast<uint8_t*>(flat_hf.data()), 32, 32, 32 * 8, 8, GL_HALF_FLOAT, "2");
    assert(flat_hf == flat_hf_orig);

    auto make_narrow_flt = [&](const std::string& seed) {
      std::vector<float> buf(32 * 32 * 4, 0.0f);
      for (int y = 0; y < 32; ++y) {
        for (int x = 0; x < 32; ++x) {
          const size_t off = (static_cast<size_t>(y) * 32 + static_cast<size_t>(x)) * 4;
          buf[off] = ((x + y) & 1) ? 0.5f : 0.50048828125f;
          buf[off + 3] = 1.0f;
        }
      }
      const auto orig = buf;
      ShuffleExtendedWebGLPixels(reinterpret_cast<uint8_t*>(buf.data()), 32, 32, 32 * 16, 16, GL_FLOAT, seed);
      assert(buf != orig);
      return buf;
    };
    assert(make_narrow_flt("2") != make_narrow_flt("3"));

    auto make_narrow_hf = [&](const std::string& seed) {
      std::vector<uint16_t> buf(32 * 32 * 4, 0);
      for (int y = 0; y < 32; ++y) {
        for (int x = 0; x < 32; ++x) {
          const size_t off = (static_cast<size_t>(y) * 32 + static_cast<size_t>(x)) * 4;
          buf[off] = FloatToHalf(((x + y) & 1) ? 0.5f : 0.50048828125f);
          buf[off + 3] = FloatToHalf(1.0f);
        }
      }
      const auto orig = buf;
      ShuffleExtendedWebGLPixels(reinterpret_cast<uint8_t*>(buf.data()), 32, 32, 32 * 8, 8, GL_HALF_FLOAT, seed);
      assert(buf != orig);
      return buf;
    };
    assert(make_narrow_hf("2") != make_narrow_hf("3"));
  }
  return 0;
}
"""
    cpp_src = (
        cpp_src.replace("__BASIC_RETURN__", fc_basic_ret)
        .replace("__FC_028__", fc_028_fn)
        .replace("__HAS_MULTI_FN__", has_multi_fn)
        .replace("__SHUFFLE_FN__", shuffle_fn)
        .replace("__WGL_EXT_FN__", wgl_ext_fn)
    )

    test_cc = tmp_path / "test_028.cc"
    test_cc.write_text(cpp_src)
    exe = tmp_path / "test_028"
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-fno-threadsafe-statics",
            "-Werror",
            "-Wthread-safety",
            "-Wexit-time-destructors",
            "-Wglobal-constructors",
            "-Wunsafe-buffer-usage",
            "-Wsign-compare",
            str(test_cc),
            "-o",
            str(exe),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, timeout=10)


def test_029_patch_applies_cleanly_and_compiles_under_strict_cpp20(tmp_path):
    import hashlib

    compiler = shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")

    expected_shas = {
        "022-native-gpu-realms.patch": "e197fd91382ccf39dd1f43b36b9b89042331f09021344ee7d661133e1bc3420c",
        "023-macos-antidetect-complete.patch": "98e2131461e38c290d67f929e0729cc187c741b31838c3515da1194ddbd57e4d",
        "024-webgl-readback-parity.patch": "78ff843b4586c5452b8987dc9ecea0b52b16e1ccbcdc5e472597dd77d36d21c3",
        "025-audio-clientrects-parity.patch": "9a369414029f7b3b7811d01a31d8cbfb915b381ad08876e4b39e87cf7128f881",
        "026-apple-silicon-device-coherence.patch": "558b84bbec096583a5253889a06b4c31f5c5efa31a79fde5d42e29fce908d75f",
        "027-macos-locale-speech-font-coherence.patch": "ec47709cbe9a96390cb78f76c329ec6d9e3636bf31f5a44812d1e0c69c2aa726",
        "028-pixelscan-noise-font-coherence.patch": "20a3147b46a11a8e27b8c912ba64bf2457ed5aa5b9c571c403a5b4a01a84de05",
        "029-macos-renderer-locale-pak-coherence.patch": "1344c45970b4ee5038fe550dd64fb60ada6261788e33fc90dee9dcdcfdd98f61",
        "109-canvas-pixel-addressing.patch": "df190001a4ab19a97acad9758a0c0dd03a800584e9b96c81c33484a5bedd6ce2",
    }
    for name, expected in expected_shas.items():
        assert hashlib.sha256((KIT / "patches" / name).read_bytes()).hexdigest() == expected

    patch027 = (KIT / "patches/027-macos-locale-speech-font-coherence.patch").read_text()
    patch029 = (KIT / "patches/029-macos-renderer-locale-pak-coherence.patch").read_text()

    touched_files = [
        line.removeprefix("+++ b/") for line in patch029.splitlines() if line.startswith("+++ b/")
    ]
    assert touched_files == [
        "content/browser/renderer_host/render_process_host_impl.cc",
        "third_party/blink/renderer/core/inspector/locale_controller.cc",
    ]
    assert all(path.endswith(".cc") for path in touched_files)

    def _apply_hunks(base_text: str, section_text: str) -> str:
        hunks = ("\n" + section_text).split("\n@@ ")[1:]
        assert hunks
        cur = base_text
        for raw_hunk in hunks:
            body = raw_hunk.splitlines()[1:]
            old_blk = "\n".join(line[1:] for line in body if line.startswith((" ", "-")))
            new_blk = "\n".join(line[1:] for line in body if line.startswith((" ", "+")))
            assert cur.count(old_blk) == 1, f"Hunk mismatch:\n{old_blk}"
            cur = cur.replace(old_blk, new_blk, 1)
        return cur

    # Reconstruct exact post-027 render_process_host_impl.cc context and apply 029 verbatim.
    rph_027_sec = patch027.split(
        "+++ b/content/browser/renderer_host/render_process_host_impl.cc\n", 1
    )[1].split("\ndiff --git ", 1)[0]
    rph_post027_body = "\n".join(
        line[1:]
        for line in rph_027_sec.splitlines()
        if line.startswith(("+", " ")) and not line.startswith("+++")
    )
    rph_post027 = (
        '#include "third_party/blink/public/public_buildflags.h"\n'
        '#include "third_party/perfetto/include/perfetto/tracing/traced_proto.h"\n'
        '#include "ui/accessibility/accessibility_switches.h"\n'
        '#include "ui/base/ui_base_features.h"\n'
        '#include "ui/base/ui_base_switches.h"\n'
        '#include "ui/display/display_switches.h"\n'
        "void RenderProcessHostImpl::AppendRendererCommandLine(\n"
        "    base::CommandLine* command_line) {\n" + rph_post027_body
    )
    rph_029_sec = patch029.split(
        "+++ b/content/browser/renderer_host/render_process_host_impl.cc\n", 1
    )[1].split("\ndiff --git ", 1)[0]
    rph_post029 = _apply_hunks(rph_post027, rph_029_sec)
    assert '#include "ui/base/l10n/l10n_util.h"' in rph_post029

    rph_start = rph_post029.index("  std::string locale =")
    rph_end_marker = "command_line->AppendSwitchASCII(switches::kLang, locale);"
    rph_end = rph_post029.index(rph_end_marker, rph_start) + len(rph_end_marker)
    rph_block = rph_post029[rph_start:rph_end]

    # Reconstruct exact post-027 locale_controller.cc Init() and apply 029 verbatim.
    lc_027_sec = patch027.split(
        "+++ b/third_party/blink/renderer/core/inspector/locale_controller.cc\n", 1
    )[1].split("\ndiff --git ", 1)[0]
    lc_027_added = "\n".join(
        line[1:]
        for line in lc_027_sec.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    lc_027_init = (
        "void LocaleController::Init()" + lc_027_added.split("void LocaleController::Init()", 1)[1]
    )
    lc_029_sec = patch029.split(
        "+++ b/third_party/blink/renderer/core/inspector/locale_controller.cc\n", 1
    )[1]
    lc_029_init = _apply_hunks(lc_027_init, lc_029_sec)

    cpp_src = r"""#include <algorithm>
#include <cassert>
#include <cctype>
#include <cstddef>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace base {
struct CommandLine {
  std::map<std::string, std::string> values;
  static CommandLine* ForCurrentProcess() {
    static CommandLine* c = new CommandLine();
    return c;
  }
  bool HasSwitch(const char* k) const { return values.count(k) > 0; }
  std::string GetSwitchValueASCII(const char* k) const {
    auto it = values.find(k);
    return it == values.end() ? std::string() : it->second;
  }
  void AppendSwitchASCII(const char* k, const std::string& v) { values[k] = v; }
};
inline std::string ToLowerASCII(std::string_view s) {
  std::string out(s);
  std::transform(out.begin(), out.end(), out.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return out;
}
inline char ToUpperASCII(char c) {
  return (c >= 'a' && c <= 'z') ? static_cast<char>(c - 'a' + 'A') : c;
}
inline bool EqualsCaseInsensitiveASCII(std::string_view a, std::string_view b) {
  return ToLowerASCII(a) == ToLowerASCII(b);
}
struct __attribute__((capability("mutex"))) Lock {};
struct __attribute__((scoped_lockable)) AutoLock {
  explicit AutoLock(Lock& m) __attribute__((acquire_capability(m))) {}
  ~AutoLock() __attribute__((release_capability())) {}
};
}  // namespace base

namespace switches {
inline constexpr char kFingerprint[] = "fingerprint";
inline constexpr char kDisableSpoofing[] = "disable-spoofing";
inline constexpr char kLang[] = "lang";
}  // namespace switches

namespace l10n_util {
enum class CheckLocaleMode {
  kVerifyLocalizationDataExists,
  kUseKnownLocalesList,
};

inline const std::set<std::string>& KnownMacPlatformLocales() {
  // Exact macOS official-build `locales_for_platform_list` from build/config/locales.gni
  // and ui/base/BUILD.gn (55 platform_pak_locales + "en").
  static const std::set<std::string>& locales = *new std::set<std::string>{
      "af",    "am",    "ar",    "bg",    "bn",    "ca",    "cs",    "da",
      "de",    "el",    "en",    "en-GB", "en-US", "es",    "es-419","et",
      "fa",    "fi",    "fil",   "fr",    "gu",    "he",    "hi",    "hr",
      "hu",    "id",    "it",    "ja",    "kn",    "ko",    "lt",    "lv",
      "ml",    "mr",    "ms",    "nb",    "nl",    "pl",    "pt-BR", "pt-PT",
      "ro",    "ru",    "sk",    "sl",    "sr",    "sv",    "sw",    "ta",
      "te",    "th",    "tr",    "uk",    "ur",    "vi",    "zh-CN", "zh-TW",
  };
  return locales;
}

inline std::string ToAppleBundleLproj(std::string_view locale) {
  if (locale == "en-US") return "en";
  std::string out(locale);
  std::replace(out.begin(), out.end(), '-', '_');
  return out;
}

inline bool HasStringsForLocale(std::string_view locale, CheckLocaleMode) {
  return KnownMacPlatformLocales().count(std::string(locale)) > 0;
}

inline std::string_view GetLanguage(std::string_view locale) {
  return locale.substr(0, locale.find('-'));
}

// Faithful mirror of Chromium 148.0.7778.215 ui/base/l10n/l10n_util.cc:408-489.
inline std::optional<std::string> CheckAndResolveLocale(
    std::string_view locale,
    CheckLocaleMode mode = CheckLocaleMode::kVerifyLocalizationDataExists) {
  if (HasStringsForLocale(locale, mode)) {
    return std::optional<std::string>(locale);
  }
  if (locale.find('@') != std::string::npos) {
    return std::nullopt;
  }
  const std::string_view lang = GetLanguage(locale);
  if (lang.size() < locale.size()) {
    const std::string_view region = locale.substr(lang.size() + 1);
    std::string tmp_locale(lang);
    if (base::EqualsCaseInsensitiveASCII(lang, "es") &&
        !base::EqualsCaseInsensitiveASCII(region, "es")) {
      tmp_locale.append("-419");
    } else if (base::EqualsCaseInsensitiveASCII(lang, "pt") &&
               !base::EqualsCaseInsensitiveASCII(region, "br")) {
      tmp_locale.append("-PT");
    } else if (base::EqualsCaseInsensitiveASCII(lang, "zh")) {
      if (base::EqualsCaseInsensitiveASCII(region, "hk") ||
          base::EqualsCaseInsensitiveASCII(region, "mo")) {
        tmp_locale.append("-TW");
      } else {
        tmp_locale.append("-CN");
      }
    } else if (base::EqualsCaseInsensitiveASCII(lang, "en")) {
      if (base::EqualsCaseInsensitiveASCII(region, "lr") ||
          base::EqualsCaseInsensitiveASCII(region, "ph")) {
        tmp_locale.append("-US");
      } else {
        tmp_locale.append("-GB");
      }
    }
    if (HasStringsForLocale(tmp_locale, mode)) {
      return tmp_locale;
    }
  }
  struct Alias {
    const char* source;
    const char* dest;
  };
  static constexpr Alias kAliasMap[] = {
      {"en", "en-US"}, {"iw", "he"},  {"no", "nb"},
      {"pt", "pt-BR"}, {"tl", "fil"}, {"zh", "zh-CN"},
  };
  for (const auto& alias : kAliasMap) {
    if (base::EqualsCaseInsensitiveASCII(lang, alias.source)) {
      if (HasStringsForLocale(alias.dest, mode)) {
        return std::optional<std::string>(alias.dest);
      }
    }
  }
  return std::nullopt;
}
}  // namespace l10n_util

struct AsciiWrap {
  std::string v;
  const char* data() const { return v.c_str(); }
};
struct String {
  std::string s;
  bool empty() const { return s.empty(); }
  std::string Utf8() const { return s; }
  AsciiWrap Ascii() const { return {s}; }
  static String FromUtf8(const std::string& in) { return {in}; }
  bool operator==(const String& o) const { return s == o.s; }
};
namespace icu {
struct Locale {
  std::string tag;
  explicit Locale(const char* t) : tag(t ? t : "") {}
  const char* getLanguage() const {
    if (tag.empty() || !std::isalpha(static_cast<unsigned char>(tag[0]))) return "";
    return tag.c_str();
  }
};
}  // namespace icu

struct LocaleController {
  base::Lock lock_;
  String embedder_locale_{"en-US"};
  String locale_override_ __attribute__((guarded_by(lock_)));
  std::vector<std::string> updates;
  void SetEmbedderLocale(const String& locale);
  void UpdateLocale(const String& l) { updates.push_back(l.s); }
  static LocaleController& instance() {
    static LocaleController* inst = new LocaleController();
    return *inst;
  }
  static void Init();
  void Reset() {
    base::AutoLock locker(lock_);
    embedder_locale_ = {"en-US"};
    locale_override_ = {};
    updates.clear();
  }
  void SetOverrideForTest(const String& l) {
    base::AutoLock locker(lock_);
    locale_override_ = l;
  }
};
__LC_029_INIT__

struct MockBrowserClient {
  std::string app_locale = "en-US";
  std::string GetApplicationLocale() const { return app_locale; }
};
struct MockContentClient {
  MockBrowserClient b;
  const MockBrowserClient* browser() const { return &b; }
};
static MockContentClient* GetContentClient() {
  static MockContentClient* c = new MockContentClient();
  return c;
}

static base::CommandLine BuildChildCommandLine(
    const base::CommandLine& browser_command_line) {
  base::CommandLine child;
  base::CommandLine* command_line = &child;
__RPH_BLOCK__
  return child;
}

int main() {
  // Bundled .lproj set derived from official Chromium 148 macOS locales.gni (55 dirs).
  std::set<std::string> bundled_lprojs;
  for (const auto& loc : l10n_util::KnownMacPlatformLocales()) {
    bundled_lprojs.insert(l10n_util::ToAppleBundleLproj(loc));
  }
  assert(bundled_lprojs.size() == 55);

  // 1. Without --fingerprint, with --disable-spoofing=locale, or with empty --lang:
  //    keeps GetApplicationLocale() and does not emit --fingerprint-locale.
  {
    base::CommandLine browser;
    auto child = BuildChildCommandLine(browser);
    assert(child.GetSwitchValueASCII(switches::kLang) == "en-US");
    assert(!child.HasSwitch("fingerprint-locale"));

    browser.values = {{"lang", "de-DE"}};
    child = BuildChildCommandLine(browser);
    assert(child.GetSwitchValueASCII(switches::kLang) == "en-US");
    assert(!child.HasSwitch("fingerprint-locale"));

    browser.values = {{"fingerprint", "3006"}, {"disable-spoofing", "locale"}, {"lang", "de-DE"}};
    child = BuildChildCommandLine(browser);
    assert(child.GetSwitchValueASCII(switches::kLang) == "en-US");
    assert(!child.HasSwitch("fingerprint-locale"));

    browser.values = {{"fingerprint", "3006"}, {"disable-spoofing", "locale,font"}, {"lang", "de-DE"}};
    child = BuildChildCommandLine(browser);
    assert(child.GetSwitchValueASCII(switches::kLang) == "en-US");
    assert(!child.HasSwitch("fingerprint-locale"));

    browser.values = {{"fingerprint", "3006"}, {"lang", ""}};
    child = BuildChildCommandLine(browser);
    assert(child.GetSwitchValueASCII(switches::kLang) == "en-US");
    assert(!child.HasSwitch("fingerprint-locale"));
  }

  // 2. End-to-end parent AppendRendererCommandLine -> child LocaleController::Init().
  struct Case {
    const char* input;
    const char* expected_pak_lang;
    const char* expected_blink_locale;
  };
  const Case cases[] = {
      {"de-DE", "de", "de-DE"},
      {"de_DE", "de", "de_DE"},
      {"DE-de", "de", "DE-de"},
      {"de", "de", "de"},
      {"vi-VN", "vi", "vi-VN"},
      {"ja-JP", "ja", "ja-JP"},
      {"fr-FR", "fr", "fr-FR"},
      {"ko-KR", "ko", "ko-KR"},
      {"en-US", "en-US", "en-US"},
      {"en-us", "en-US", "en-us"},
      {"en_US", "en-US", "en_US"},
      {"en-GB", "en-GB", "en-GB"},
      {"en-AU", "en-GB", "en-AU"},
      {"en-CA", "en-GB", "en-CA"},
      {"en-PH", "en-US", "en-PH"},
      {"en-001", "en-GB", "en-001"},
      {"en", "en", "en"},
      {"zh-CN", "zh-CN", "zh-CN"},
      {"zh-cn", "zh-CN", "zh-cn"},
      {"zh-SG", "zh-CN", "zh-SG"},
      {"zh-Hans-CN", "zh-CN", "zh-Hans-CN"},
      {"zh-TW", "zh-TW", "zh-TW"},
      {"zh-HK", "zh-TW", "zh-HK"},
      {"zh-MO", "zh-TW", "zh-MO"},
      {"es-MX", "es-419", "es-MX"},
      {"es-ES", "es", "es-ES"},
      {"es-419", "es-419", "es-419"},
      {"pt-BR", "pt-BR", "pt-BR"},
      {"pt-br", "pt-BR", "pt-br"},
      {"pt-PT", "pt-PT", "pt-PT"},
      {"pt-AO", "pt-PT", "pt-AO"},
      {"pt", "pt-BR", "pt"},
      {"nb-NO", "nb", "nb-NO"},
      {"no-NO", "nb", "no-NO"},
      {"he-IL", "he", "he-IL"},
      {"iw-IL", "he", "iw-IL"},
      {"tl-PH", "fil", "tl-PH"},
      {"fil-PH", "fil", "fil-PH"},
      {"xx-YY", "en-US", "xx-YY"},
      {"is-IS", "en-US", "is-IS"},
      {"../en", "en-US", "../en"},
      {"de-DE@foo", "en-US", "de-DE@foo"},
  };
  auto* child_cmd = base::CommandLine::ForCurrentProcess();
  auto& lc = LocaleController::instance();
  for (const Case& tc : cases) {
    base::CommandLine browser;
    browser.values = {{"fingerprint", "3006"}, {"lang", tc.input}};
    base::CommandLine child = BuildChildCommandLine(browser);
    const std::string pak_lang = child.GetSwitchValueASCII(switches::kLang);
    assert(pak_lang == tc.expected_pak_lang);
    assert(bundled_lprojs.count(l10n_util::ToAppleBundleLproj(pak_lang)) == 1);
    assert(child.GetSwitchValueASCII("fingerprint-locale") == tc.expected_blink_locale);

    child_cmd->values = child.values;
    child_cmd->values["fingerprint"] = "3006";
    lc.Reset();
    LocaleController::Init();
    if (std::isalpha(static_cast<unsigned char>(tc.input[0]))) {
      assert(lc.embedder_locale_.s == tc.expected_blink_locale);
      assert(lc.updates == std::vector<std::string>{tc.expected_blink_locale});
    } else {
      assert(lc.embedder_locale_.s == "en-US");
      assert(lc.updates.empty());
    }
  }

  // 3. LocaleController::Init() direct edge cases:
  //    prefers --fingerprint-locale over --lang, falls back to --lang when absent,
  //    ignores when spoofing disabled or un-fingerprinted, keeps CDP override active.
  lc.Reset();
  child_cmd->values = {{"fingerprint", "3003"}, {"lang", "en-US"}, {"fingerprint-locale", "vi-VN"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "vi-VN");
  assert(lc.updates == std::vector<std::string>{"vi-VN"});

  lc.Reset();
  child_cmd->values = {{"fingerprint", "3003"}, {"lang", "fr"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "fr");
  assert(lc.updates == std::vector<std::string>{"fr"});

  lc.Reset();
  child_cmd->values = {{"fingerprint", "3003"}, {"disable-spoofing", "locale"}, {"lang", "en-US"}, {"fingerprint-locale", "de-DE"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "en-US" && lc.updates.empty());

  lc.Reset();
  child_cmd->values = {{"lang", "de"}, {"fingerprint-locale", "de-DE"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "en-US" && lc.updates.empty());

  lc.Reset();
  lc.SetOverrideForTest({"ja-JP"});
  child_cmd->values = {{"fingerprint", "3003"}, {"lang", "de"}, {"fingerprint-locale", "de-DE"}};
  LocaleController::Init();
  assert(lc.embedder_locale_.s == "de-DE" && lc.updates.empty());

  return 0;
}
"""
    cpp_src = cpp_src.replace("__LC_029_INIT__", lc_029_init).replace("__RPH_BLOCK__", rph_block)

    test_cc = tmp_path / "test_029.cc"
    test_cc.write_text(cpp_src)
    exe = tmp_path / "test_029"
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-O2",
            "-fno-threadsafe-statics",
            "-Werror",
            "-Wthread-safety",
            "-Wexit-time-destructors",
            "-Wglobal-constructors",
            "-Wunsafe-buffer-usage",
            "-Wsign-compare",
            str(test_cc),
            "-o",
            str(exe),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, timeout=10)
