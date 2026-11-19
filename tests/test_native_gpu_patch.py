"""Compiled shared-selector regression and fail-closed native acceptance checks.

The C++ boundary uses small base API stubs, not a full Blink/Chromium build.
"""

import importlib.util
import re
import shutil
import subprocess
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

    _write_026_cpp_mock_headers(tmp_path, patch026)

    test_cc = tmp_path / "test_p103_p204.cc"
    test_cc.write_text(
        f"""#include <algorithm>
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
#include "components/ungoogled/fingerprint_display.h"

namespace media {{ struct VideoCaptureControlSupport {{}}; }}
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
  auto* cmd = base::CommandLine::ForCurrentProcess();
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
    subprocess.run([str(exe)], check=True, capture_output=True, timeout=10)
