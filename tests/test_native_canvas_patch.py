"""Compile the exact upstream canvas macro, not a Python model of its precedence.

This is source-level evidence only: it neither builds nor accepts a browser core.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PATCH = (
    Path(__file__).resolve().parents[1]
    / "native/chromium-148/patches/109-canvas-pixel-addressing.patch"
)


@pytest.mark.parametrize(
    ("prefix", "expected"), [("-+", 1), ("++", 0)], ids=["upstream-bug", "corrected"]
)
def test_canvas_pixel_address_expressions(tmp_path, prefix, expected):
    compiler = shutil.which("clang++") or shutil.which("c++")
    if compiler is None:
        pytest.skip("C++ compiler required for native macro regression")
    macros = [
        line[2:]
        for line in PATCH.read_text().splitlines()
        if line.startswith(prefix + "#define writable_addr")
    ]
    assert len(macros) == 1
    source = tmp_path / "canvas_address.cc"
    source.write_text(
        "#include <cstdint>\n"
        + macros[0]
        + r"""
template<class T> bool correct() {
  alignas(8) unsigned char pixels[4096] = {};
  const int stride = 128;
  for (int y = 0; y < 8; ++y) {
    for (int x = 0; x < 8; ++x) {
      const auto* current = writable_addr(T, pixels, stride, x, y);
      const auto* right = writable_addr(T, pixels, stride, x + 1, y);
      const auto* bottom = writable_addr(T, pixels, stride, x, y + 1);
      const auto* padded = writable_addr(T, pixels, stride + 8, x + 1, y + 1);
      if (reinterpret_cast<const unsigned char*>(current) != pixels + y * stride + x * sizeof(T) ||
          reinterpret_cast<const unsigned char*>(right) != pixels + y * stride + (x + 1) * sizeof(T) ||
          reinterpret_cast<const unsigned char*>(bottom) != pixels + (y + 1) * stride + x * sizeof(T) ||
          reinterpret_cast<const unsigned char*>(padded) != pixels + (y + 1) * (stride + 8) + (x + 1) * sizeof(T)) return false;
    }
  }
  return true;
}
int main() {
  return correct<uint8_t>() && correct<uint16_t>() && correct<uint32_t>() ? 0 : 1;
}
"""
    )
    executable = tmp_path / "canvas_address"
    subprocess.run(
        [compiler, "-std=c++17", str(source), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stderr
