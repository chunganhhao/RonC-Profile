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


@pytest.fixture
def sample():
    return {
        "gpu": {"vendor": "amd", "architecture": "gcn-5", "device": "", "description": ""},
        "cpu": 20,
        "memory": 8,
        "stackReads": 0,
        "globals": [],
        "solidChanged": 0,
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
