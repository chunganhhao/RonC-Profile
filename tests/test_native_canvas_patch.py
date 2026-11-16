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
READBACK_PATCH = (
    Path(__file__).resolve().parents[1]
    / "native/chromium-148/patches/024-webgl-readback-parity.patch"
)
AUDIO_RECTS_PATCH = (
    Path(__file__).resolve().parents[1]
    / "native/chromium-148/patches/025-audio-clientrects-parity.patch"
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


def _extract_added_function(patch_text: str, signature_prefix: str) -> str:
    added = [
        line[1:]
        for line in patch_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    start = next(i for i, line in enumerate(added) if line.startswith(signature_prefix))
    depth = 0
    started = False
    out = []
    for line in added[start:]:
        out.append(line)
        if "{" in line:
            depth += line.count("{")
            started = True
        if "}" in line:
            depth -= line.count("}")
        if started and depth == 0:
            break
    return "\n".join(out)


def _extract_added_block(patch_text: str, start_prefix: str, stop_prefix: str) -> str:
    added = [
        line[1:]
        for line in patch_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    start = next(i for i, line in enumerate(added) if line.startswith(start_prefix))
    out = []
    for line in added[start:]:
        out.append(line)
        if line.startswith(stop_prefix):
            break
    return "\n".join(out)


def test_compiled_read_fingerprint_pixels_parity_and_safety(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("c++")
    if compiler is None:
        pytest.skip("C++ compiler required for native readback parity regression")
    readback_text = READBACK_PATCH.read_text()
    anon_helpers = _extract_added_block(readback_text, "float HalfToFloat(", "}  // namespace")
    fn_code = _extract_added_function(
        readback_text, "bool WebGLRenderingContextBase::ReadFingerprintPixels("
    )
    harness = r"""
#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <string>
#include <unordered_set>
#include <vector>

#define UNSAFE_BUFFERS(...) \
  _Pragma("clang unsafe_buffer_usage begin") __VA_ARGS__ _Pragma("clang unsafe_buffer_usage end")
#define UNSAFE_TODO(...) UNSAFE_BUFFERS(__VA_ARGS__)

using GLenum = unsigned int;
using GLint = int;
using GLsizei = int;
using GLuint = unsigned int;
using GLintptr = int64_t;
using GLsizeiptr = int64_t;
using wtf_size_t = size_t;

template <typename T>
struct Vector : public std::vector<T> {
  explicit Vector(size_t n) : std::vector<T>(n) {}
};

enum {
  GL_NO_ERROR = 0,
  GL_NONE = 0,
  GL_BACK = 0x0405,
  GL_INVALID_ENUM = 0x0500,
  GL_INVALID_OPERATION = 0x0502,
  GL_OUT_OF_MEMORY = 0x0505,
  GL_RGB = 0x1907,
  GL_RGBA = 0x1908,
  GL_UNSIGNED_BYTE = 0x1401,
  GL_UNSIGNED_SHORT_4_4_4_4 = 0x8033,
  GL_UNSIGNED_SHORT_5_5_5_1 = 0x8034,
  GL_UNSIGNED_SHORT_5_6_5 = 0x8363,
  GL_FLOAT = 0x1406,
  GL_HALF_FLOAT = 0x140B,
  GL_HALF_FLOAT_OES = 0x8D61,
  GL_UNSIGNED_INT_10F_11F_11F_REV = 0x8C3B,
  GL_FRAMEBUFFER = 0x8D40,
  GL_READ_FRAMEBUFFER = 0x8CA8,
  GL_COLOR_ATTACHMENT0 = 0x8CE0,
  GL_FRAMEBUFFER_COMPLETE = 0x8CD5,
  GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE = 0x8CD0,
  GL_RENDERBUFFER = 0x8D41,
  GL_IMPLEMENTATION_COLOR_READ_FORMAT = 0x8B9B,
  GL_IMPLEMENTATION_COLOR_READ_TYPE = 0x8B9A,
  GL_READ_BUFFER = 0x0C02,
  GL_PIXEL_PACK_BUFFER = 0x88EB,
  GL_PACK_ALIGNMENT = 0x0D05,
  GL_PACK_ROW_LENGTH = 0x0D02,
  GL_PACK_SKIP_ROWS = 0x0D03,
  GL_PACK_SKIP_PIXELS = 0x0D04,
};

namespace switches {
inline const char* kFingerprint = "fingerprint";
inline const char* kDisableSpoofing = "disable-spoofing";
}

namespace base {
template <typename Dest, typename Source>
Dest bit_cast(const Source& source) {
  static_assert(sizeof(Dest) == sizeof(Source));
  Dest dest;
  UNSAFE_TODO(std::memcpy(&dest, &source, sizeof(Dest)));
  return dest;
}
template <typename T>
struct CheckedNumeric {
  T val;
  bool ok = true;
  CheckedNumeric(T v) : val(v) {}
  CheckedNumeric(T v, bool o) : val(v), ok(o) {}
  template <typename U>
  CheckedNumeric operator+(U rhs) const {
    if (!ok) return {0, false};
    if constexpr (std::is_signed_v<T>) {
      int64_t sum = static_cast<int64_t>(val) + static_cast<int64_t>(rhs);
      if (sum < std::numeric_limits<T>::min() || sum > std::numeric_limits<T>::max()) {
        return {0, false};
      }
      return {static_cast<T>(sum), true};
    } else {
      unsigned __int128 sum = static_cast<unsigned __int128>(val) + static_cast<unsigned __int128>(rhs);
      if (sum > std::numeric_limits<T>::max()) return {0, false};
      return {static_cast<T>(sum), true};
    }
  }
  template <typename U>
  CheckedNumeric operator*(U rhs) const {
    if (!ok) return {0, false};
    unsigned __int128 prod = static_cast<unsigned __int128>(val) * static_cast<unsigned __int128>(rhs);
    if (prod > std::numeric_limits<T>::max()) return {0, false};
    return {static_cast<T>(prod), true};
  }
  bool IsValid() const { return ok; }
  T ValueOrDie() const { assert(ok); return val; }
};
struct CommandLine {
  static const CommandLine* ForCurrentProcess() {
    static CommandLine cmd;
    return &cmd;
  }
  bool HasSwitch(const char* sw) const { return std::string(sw) == "fingerprint"; }
  std::string GetSwitchValueASCII(const char* sw) const {
    if (std::string(sw) == "fingerprint") return fingerprint;
    return disable_spoof;
  }
  inline static std::string fingerprint = "2";
  inline static std::string disable_spoof;
};
}  // namespace base

enum SkColorType { kRGBA_8888_SkColorType, kRGB_565_SkColorType, kARGB_4444_SkColorType };
enum SkAlphaType { kOpaque_SkAlphaType, kUnpremul_SkAlphaType };

struct SkImageInfo {
  int w, h;
  SkColorType ct;
  static SkImageInfo Make(int w, int h, SkColorType ct, SkAlphaType) { return {w, h, ct}; }
  static bool ByteSizeOverflowed(size_t s) { return s == std::numeric_limits<size_t>::max(); }
  bool isEmpty() const { return w <= 0 || h <= 0; }
  size_t bytesPerPixel() const { return ct == kRGBA_8888_SkColorType ? 4 : 2; }
  size_t minRowBytes() const { return static_cast<size_t>(w) * bytesPerPixel(); }
  size_t computeMinByteSize() const {
    if (w <= 0 || h <= 0) return 0;
    return static_cast<size_t>(w) * static_cast<size_t>(h) * bytesPerPixel();
  }
};

struct WebGLImageConversion {
  struct PixelStoreParams {
    int alignment = 4;
    int row_length = 0;
    int skip_pixels = 0;
    int skip_rows = 0;
  };
  static GLenum ComputeImageSizeInBytes(GLenum format, GLenum type, GLsizei width, GLsizei height,
                                        GLsizei, const PixelStoreParams& p, unsigned* bytes,
                                        unsigned* padding_out, unsigned* skip_out) {
    unsigned bpp = 2u;
    if (format == GL_RGBA && type == GL_UNSIGNED_BYTE) bpp = 4u;
    else if (format == GL_RGBA && (type == GL_HALF_FLOAT || type == GL_HALF_FLOAT_OES)) bpp = 8u;
    else if (format == GL_RGBA && type == GL_FLOAT) bpp = 16u;
    unsigned rl = p.row_length > 0 ? static_cast<unsigned>(p.row_length) : static_cast<unsigned>(width);
    unsigned row_b = rl * bpp;
    unsigned align = static_cast<unsigned>(p.alignment);
    unsigned pad = (align - (row_b % align)) % align;
    unsigned stride = row_b + pad;
    unsigned last = static_cast<unsigned>(width) * bpp;
    *bytes = height > 0 ? (static_cast<unsigned>(height) - 1u) * stride + last : 0u;
    if (padding_out) *padding_out = pad;
    if (skip_out) *skip_out = static_cast<unsigned>(p.skip_rows) * stride +
                              static_cast<unsigned>(p.skip_pixels) * bpp;
    return GL_NO_ERROR;
  }
};

struct WebGLFramebuffer {
  GLenum read_buffer = GL_COLOR_ATTACHMENT0;
  bool opaque = false;
  int dummy_att = 1;
  void* att_obj = &dummy_att;
  GLenum GetReadBuffer() const { return read_buffer; }
  bool Opaque() const { return opaque; }
  void* GetAttachmentObject(GLenum) const { return att_obj; }
};

struct FakeGL {
  int fbo_w = 24, fbo_h = 20;
  int impl_fmt = GL_RGBA, impl_type = GL_UNSIGNED_BYTE;
  GLenum att_type = GL_RENDERBUFFER;
  int pack_align = 4, pack_row_length = 0, pack_skip_pixels = 0, pack_skip_rows = 0;
  GLuint bound_pbo = 0;
  bool fail_read = false;
  std::vector<uint8_t> source_bottom_up;
  std::vector<uint8_t> pbo_mem;
  int read_calls = 0;

  GLenum CheckFramebufferStatus(GLenum) { return GL_FRAMEBUFFER_COMPLETE; }
  void GetIntegerv(GLenum pname, GLint* out) {
    if (pname == GL_IMPLEMENTATION_COLOR_READ_FORMAT) *out = impl_fmt;
    else if (pname == GL_IMPLEMENTATION_COLOR_READ_TYPE) *out = impl_type;
    else if (pname == GL_READ_BUFFER) *out = GL_BACK;
  }
  void GetFramebufferAttachmentParameteriv(GLenum, GLenum, GLenum pname, GLint* out) {
    if (pname == 0x1000) *out = fbo_w;
    else if (pname == 0x1001) *out = fbo_h;
    else if (pname == GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE) *out = static_cast<GLint>(att_type);
  }
  void BindBuffer(GLenum target, GLuint buf) {
    if (target == GL_PIXEL_PACK_BUFFER) bound_pbo = buf;
  }
  void PixelStorei(GLenum pname, GLint v) {
    if (pname == GL_PACK_ALIGNMENT) pack_align = v;
    else if (pname == GL_PACK_ROW_LENGTH) pack_row_length = v;
    else if (pname == GL_PACK_SKIP_PIXELS) pack_skip_pixels = v;
    else if (pname == GL_PACK_SKIP_ROWS) pack_skip_rows = v;
  }
  void ReadPixels(GLint x, GLint y, GLsizei w, GLsizei h, GLenum format, GLenum type, void* dst) {
    read_calls++;
    assert(bound_pbo == 0 && pack_align == 1 && pack_row_length == 0 &&
           pack_skip_pixels == 0 && pack_skip_rows == 0);
    if (fail_read) return;
    size_t bpp = 2u;
    if (format == GL_RGBA && type == GL_UNSIGNED_BYTE) bpp = 4u;
    else if (format == GL_RGBA && (type == GL_HALF_FLOAT || type == GL_HALF_FLOAT_OES)) bpp = 8u;
    else if (format == GL_RGBA && type == GL_FLOAT) bpp = 16u;
    if (x == fbo_w - 1 && y == fbo_h - 1 && w == 1 && h == 1) {
      size_t off = (static_cast<size_t>(y) * static_cast<size_t>(fbo_w) + static_cast<size_t>(x)) * bpp;
      UNSAFE_TODO(std::memcpy(dst, source_bottom_up.data() + off, bpp));
      return;
    }
    assert(x == 0 && y == 0 && w == fbo_w && h == fbo_h);
    UNSAFE_TODO(std::memcpy(dst, source_bottom_up.data(), source_bottom_up.size()));
  }
  void BufferSubData(GLenum target, GLintptr off, GLsizeiptr sz, const void* data) {
    assert(target == GL_PIXEL_PACK_BUFFER && bound_pbo != 0);
    assert(off >= 0 && static_cast<size_t>(off + sz) <= pbo_mem.size());
    UNSAFE_TODO(std::memcpy(pbo_mem.data() + off, data, static_cast<size_t>(sz)));
  }
};

struct StaticBitmapImage {
  static void ShuffleSubchannelColorData(const void* addr, const SkImageInfo& info, int, int) {
    // Edge-sensitive top-down marker to expose flip/crop bugs across pack paths.
    auto* p = static_cast<uint8_t*>(const_cast<void*>(addr));
    size_t bpp = info.bytesPerPixel();
    UNSAFE_TODO(p[0] ^= 0x01);  // top-left in top-down = (x=0, y=h-1 in GL)
    size_t bottom_edge = static_cast<size_t>(info.h - 1) * info.minRowBytes() + bpp;
    UNSAFE_TODO(p[bottom_edge] ^= 0x02);  // (x=1, y=0 in GL)
  }
};

struct WebGLRenderingContextBase {
  FakeGL gl;
  bool is_webgl2 = true;
  int def_w = 32, def_h = 32;
  WebGLImageConversion::PixelStoreParams user_pack;
  GLuint user_pbo = 0;
  GLenum last_error = GL_NO_ERROR;

  FakeGL* ContextGL() { return &gl; }
  bool IsWebGL2() const { return is_webgl2; }
  int drawingBufferWidth() const { return def_w; }
  int drawingBufferHeight() const { return def_h; }
  WebGLImageConversion::PixelStoreParams GetPackPixelStoreParams() const { return user_pack; }
  void SynthesizeGLError(GLenum err, const char*, const char*) { last_error = err; }
  void DrawingBufferClientRestorePixelPackParameters() {
    gl.pack_align = user_pack.alignment;
    gl.pack_row_length = user_pack.row_length;
    gl.pack_skip_pixels = user_pack.skip_pixels;
    gl.pack_skip_rows = user_pack.skip_rows;
  }
  void DrawingBufferClientRestorePixelPackBufferBinding() { gl.bound_pbo = user_pbo; }
  bool ReadFingerprintPixels(WebGLFramebuffer*, GLint, GLint, GLsizei, GLsizei,
                             GLenum, GLenum, uint8_t*, size_t, GLuint = 0, int64_t = 0);
};
"""
    source = tmp_path / "readback_parity.cc"
    source.write_text(
        harness
        + "\nnamespace {\n"
        + anon_helpers
        + "\n"
        + fn_code
        + r"""
int main() {
  WebGLRenderingContextBase ctx;
  WebGLFramebuffer fbo;
  const int W = 17, H = 13;
  ctx.gl.fbo_w = W;
  ctx.gl.fbo_h = H;
  ctx.gl.source_bottom_up.resize(W * H * 4);
  for (int sy = 0; sy < H; ++sy) {
    for (int sx = 0; sx < W; ++sx) {
      size_t off = (static_cast<size_t>(sy) * W + sx) * 4;
      UNSAFE_TODO(ctx.gl.source_bottom_up[off + 0] = static_cast<uint8_t>(sx));
      UNSAFE_TODO(ctx.gl.source_bottom_up[off + 1] = static_cast<uint8_t>(sy));
      UNSAFE_TODO(ctx.gl.source_bottom_up[off + 2] = 0x50);
      UNSAFE_TODO(ctx.gl.source_bottom_up[off + 3] = 0xFF);
    }
  }
  std::vector<uint8_t> full1(W * H * 4);
  ctx.user_pack = {1, 0, 0, 0};
  assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, full1.data(), full1.size()));
  // Verify top-left (y=H-1, x=0) got ^0x01 and bottom (y=0, x=1) got ^0x02.
  assert(full1[((H - 1) * W + 0) * 4] == (0 ^ 0x01));
  assert(full1[(0 * W + 1) * 4] == (1 ^ 0x02));

  // Verify 1x1 tiled readback and alignments 1/2/4/8 match full1 exactly.
  std::vector<uint8_t> tiled(W * H * 4);
  for (int y = 0; y < H; ++y) {
    for (int x = 0; x < W; ++x) {
      uint8_t px[4] = {};
      assert(ctx.ReadFingerprintPixels(&fbo, x, y, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, px, sizeof(px)));
      UNSAFE_TODO(std::memcpy(&tiled[(static_cast<size_t>(y) * W + x) * 4], px, 4));
    }
  }
  assert(tiled == full1);

  for (int align : {1, 2, 4, 8}) {
    ctx.user_pack = {align, 0, 0, 0};
    size_t row_b = static_cast<size_t>(W) * 4;
    size_t pad = (static_cast<size_t>(align) - (row_b % static_cast<size_t>(align))) %
                 static_cast<size_t>(align);
    size_t row_stride = row_b + pad;
    std::vector<uint8_t> packed(static_cast<size_t>(H) * row_stride + 32, 0xCC);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, packed.data(), packed.size()));
    for (int r = 0; r < H; ++r) {
      size_t src_off = static_cast<size_t>(r) * row_b;
      size_t dst_off = static_cast<size_t>(r) * row_stride;
      assert(std::equal(full1.begin() + src_off, full1.begin() + src_off + row_b,
                        packed.begin() + dst_off));
      if (r < H - 1) {
        for (size_t p = row_b; p < row_stride; ++p) assert(packed[dst_off + p] == 0xCC);
      }
    }
    assert(ctx.gl.pack_align == align);
  }

  // Negative offset + WebGL2 PBO with row_length=9, skip_pixels=2, skip_rows=1, align=8, offset=12.
  ctx.user_pack = {8, 9, 2, 1};
  ctx.user_pbo = 7;
  ctx.gl.bound_pbo = 7;
  ctx.gl.pbo_mem.assign(512, 0xA5);
  assert(ctx.ReadFingerprintPixels(&fbo, -1, 0, 4, 3, GL_RGBA, GL_UNSIGNED_BYTE, nullptr, 500, 7, 12));
  assert(ctx.gl.bound_pbo == 7 && ctx.gl.pack_align == 8 && ctx.gl.pack_row_length == 9);
  size_t stride = 9 * 4 + 4;  // 36 padded to align 8 = 40
  size_t base_off = 12 + 1 * stride + 2 * 4;
  for (size_t i = 0; i < base_off; ++i) assert(ctx.gl.pbo_mem[i] == 0xA5);
  for (int r = 0; r < 3; ++r) {
    size_t row_off = base_off + static_cast<size_t>(r) * stride;
    // col 0 is x=-1 -> out of bounds -> 0
    for (int b = 0; b < 4; ++b) assert(ctx.gl.pbo_mem[row_off + static_cast<size_t>(b)] == 0);
    // cols 1..3 are x=0..2 -> match full1(r, 0..2)
    for (int b = 0; b < 12; ++b) {
      assert(ctx.gl.pbo_mem[row_off + 4 + static_cast<size_t>(b)] == full1[(static_cast<size_t>(r) * W * 4) + static_cast<size_t>(b)]);
    }
    if (r < 2) {
      for (size_t b = 16; b < stride; ++b) assert(ctx.gl.pbo_mem[row_off + b] == 0xA5);
    }
  }
  for (size_t i = base_off + 2 * stride + 16; i < ctx.gl.pbo_mem.size(); ++i) {
    assert(ctx.gl.pbo_mem[i] == 0xA5);
  }

  // RGB565 FBO read as RGBA/UNSIGNED_BYTE must still be intercepted.
  ctx.gl.impl_fmt = GL_RGB;
  ctx.gl.impl_type = GL_UNSIGNED_SHORT_5_6_5;
  ctx.user_pack = {8, 0, 0, 0};
  ctx.user_pbo = 0;
  ctx.gl.bound_pbo = 0;
  size_t row_b565 = static_cast<size_t>(W) * 4;
  size_t stride565 = ((row_b565 + 7) / 8) * 8;
  std::vector<uint8_t> rgba_from_565(static_cast<size_t>(H) * stride565, 0xCC);
  assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, rgba_from_565.data(), rgba_from_565.size()));
  for (int r = 0; r < H; ++r) {
    assert(std::equal(full1.begin() + static_cast<size_t>(r) * row_b565,
                      full1.begin() + static_cast<size_t>(r + 1) * row_b565,
                      rgba_from_565.begin() + static_cast<size_t>(r) * stride565));
  }

  // FLOAT attachment still rejects UNSIGNED_BYTE reads (returns false -> driver error).
  ctx.gl.impl_type = GL_FLOAT;
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, W, H, GL_RGBA, GL_UNSIGNED_BYTE, rgba_from_565.data(), rgba_from_565.size()));
  // RGBA32F attachment rejects HALF_FLOAT reads (returns false -> driver INVALID_OPERATION).
  std::vector<uint16_t> hf_rej(W * H * 4, 0);
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, W, H, GL_RGBA, GL_HALF_FLOAT, reinterpret_cast<uint8_t*>(hf_rej.data()), hf_rej.size() * 2));
  // FLOAT odd PBO offset (2) delegates to driver validation.
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGBA, GL_FLOAT, nullptr, 128, 7, 2));

  // Verify HalfToFloat <-> FloatToHalf exact round-trip across all 65,536 bit patterns.
  for (uint32_t hv = 0; hv <= 0xFFFFu; ++hv) {
    const uint16_t h = static_cast<uint16_t>(hv);
    const float f = HalfToFloat(h);
    const uint16_t rt = FloatToHalf(f);
    if ((h & 0x7FFFu) > 0x7C00u) {
      assert(std::isnan(f) && (rt & 0x7FFFu) > 0x7C00u);
    } else {
      assert(rt == h);
    }
  }

  // Verify FLOAT gradient, solid fill preservation, HDR out-of-gamut preservation, and 16B sentinel probe.
  {
    const int FW = 16, FH = 16;
    ctx.gl.fbo_w = FW;
    ctx.gl.fbo_h = FH;
    ctx.gl.impl_fmt = GL_RGBA;
    ctx.gl.impl_type = GL_FLOAT;
    ctx.user_pack = {4, 0, 0, 0};
    ctx.gl.source_bottom_up.resize(static_cast<size_t>(FW) * FH * 16);
    auto write_px = [&](int x, int y, float r, float g, float b, float a) {
      size_t off = (static_cast<size_t>(y) * FW + x) * 16;
      float px[4] = {r, g, b, a};
      UNSAFE_TODO(std::memcpy(ctx.gl.source_bottom_up.data() + off, px, 16));
    };
    for (int y = 0; y < FH; ++y) {
      for (int x = 0; x < FW; ++x) {
        write_px(x, y, 0.25f, 0.50f, 0.75f, 1.0f);
      }
    }
    std::vector<float> solid_f(static_cast<size_t>(FW) * FH * 4);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_FLOAT, reinterpret_cast<uint8_t*>(solid_f.data()), solid_f.size() * 4));
    for (size_t i = 0; i < solid_f.size(); i += 4) {
      assert(solid_f[i] == 0.25f && solid_f[i + 1] == 0.50f && solid_f[i + 2] == 0.75f && solid_f[i + 3] == 1.0f);
    }
    // Draw HDR pixels (-0.5, 2.25) plus in-gamut gradient edge pixels.
    for (int y = 0; y < FH; ++y) {
      for (int x = 0; x < FW; ++x) {
        if (x == 0 && y == 0) {
          write_px(x, y, -0.5f, 2.25f, 1.5f, 1.0f);
        } else {
          write_px(x, y, static_cast<float>(20 + x * 4) / 255.0f, static_cast<float>(40 + y * 4) / 255.0f, 0.5f, 1.0f);
        }
      }
    }
    base::CommandLine::fingerprint = "2";
    std::vector<float> f_s2(static_cast<size_t>(FW) * FH * 4);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_FLOAT, reinterpret_cast<uint8_t*>(f_s2.data()), f_s2.size() * 4));
    assert(f_s2[0] == -0.5f && f_s2[1] == 2.25f && f_s2[2] == 1.5f && f_s2[3] == 1.0f);
    base::CommandLine::fingerprint = "3";
    std::vector<float> f_s3(static_cast<size_t>(FW) * FH * 4);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_FLOAT, reinterpret_cast<uint8_t*>(f_s3.data()), f_s3.size() * 4));
    assert(f_s2 != f_s3);
    // Genuine 0xA5 in all 16 bytes of bottom-right FLOAT pixel exercises probe[16] safely.
    UNSAFE_TODO(std::memset(ctx.gl.source_bottom_up.data() + ctx.gl.source_bottom_up.size() - 16, 0xA5, 16));
    std::vector<uint8_t> one_f16(16, 0x11);
    assert(ctx.ReadFingerprintPixels(&fbo, FW - 1, FH - 1, 1, 1, GL_RGBA, GL_FLOAT, one_f16.data(), 16));
    for (uint8_t b : one_f16) assert(b == 0xA5);

    // RGBA16F FBO accepts BOTH HALF_FLOAT, HALF_FLOAT_OES, and FLOAT readback.
    ctx.gl.impl_type = GL_HALF_FLOAT;
    ctx.gl.source_bottom_up.resize(static_cast<size_t>(FW) * FH * 8);
    for (int y = 0; y < FH; ++y) {
      for (int x = 0; x < FW; ++x) {
        uint16_t px[4] = {
            FloatToHalf(static_cast<float>(24 + x * 4) / 255.0f),
            FloatToHalf(static_cast<float>(44 + y * 4) / 255.0f),
            FloatToHalf(0.5f),
            FloatToHalf(1.0f),
        };
        UNSAFE_TODO(std::memcpy(ctx.gl.source_bottom_up.data() + (static_cast<size_t>(y) * FW + x) * 8, px, 8));
      }
    }
    base::CommandLine::fingerprint = "2";
    std::vector<uint16_t> h_s2(static_cast<size_t>(FW) * FH * 4);
    std::vector<uint16_t> h_oes(static_cast<size_t>(FW) * FH * 4);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_HALF_FLOAT, reinterpret_cast<uint8_t*>(h_s2.data()), h_s2.size() * 2));
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_HALF_FLOAT_OES, reinterpret_cast<uint8_t*>(h_oes.data()), h_oes.size() * 2));
    assert(h_s2 == h_oes);
    base::CommandLine::fingerprint = "3";
    std::vector<uint16_t> h_s3(static_cast<size_t>(FW) * FH * 4);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_HALF_FLOAT, reinterpret_cast<uint8_t*>(h_s3.data()), h_s3.size() * 2));
    assert(h_s2 != h_s3);

    // RGBA5551 FBO gradient differs across seeds and preserves solid fills.
    ctx.gl.impl_type = GL_UNSIGNED_SHORT_5_5_5_1;
    ctx.gl.source_bottom_up.resize(static_cast<size_t>(FW) * FH * 2);
    for (int y = 0; y < FH; ++y) {
      for (int x = 0; x < FW; ++x) {
        uint16_t p = static_cast<uint16_t>(((4 + (x % 20)) << 11) | ((8 + (y % 15)) << 6) | (16 << 1) | 1);
        UNSAFE_TODO(std::memcpy(ctx.gl.source_bottom_up.data() + (static_cast<size_t>(y) * FW + x) * 2, &p, 2));
      }
    }
    base::CommandLine::fingerprint = "2";
    std::vector<uint16_t> u5551_s2(static_cast<size_t>(FW) * FH);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_UNSIGNED_SHORT_5_5_5_1, reinterpret_cast<uint8_t*>(u5551_s2.data()), u5551_s2.size() * 2));
    base::CommandLine::fingerprint = "3";
    std::vector<uint16_t> u5551_s3(static_cast<size_t>(FW) * FH);
    assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, FW, FH, GL_RGBA, GL_UNSIGNED_SHORT_5_5_5_1, reinterpret_cast<uint8_t*>(u5551_s3.data()), u5551_s3.size() * 2));
    assert(u5551_s2 != u5551_s3);

    ctx.gl.fbo_w = W;
    ctx.gl.fbo_h = H;
    ctx.gl.source_bottom_up.resize(W * H * 4);
    for (int sy = 0; sy < H; ++sy) {
      for (int sx = 0; sx < W; ++sx) {
        size_t off = (static_cast<size_t>(sy) * W + sx) * 4;
        UNSAFE_TODO(ctx.gl.source_bottom_up[off + 0] = static_cast<uint8_t>(sx));
        UNSAFE_TODO(ctx.gl.source_bottom_up[off + 1] = static_cast<uint8_t>(sy));
        UNSAFE_TODO(ctx.gl.source_bottom_up[off + 2] = 0x50);
        UNSAFE_TODO(ctx.gl.source_bottom_up[off + 3] = 0xFF);
      }
    }
  }
  base::CommandLine::fingerprint = "2";

  // Reset impl format/type to RGBA8 for error/validation regressions.
  ctx.gl.impl_fmt = GL_RGBA;
  ctx.gl.impl_type = GL_UNSIGNED_BYTE;
  ctx.user_pack = {4, 0, 0, 0};

  // 1. Failed canonical ReadPixels preserves destination array & PBO sentinels.
  ctx.gl.fail_read = true;
  std::vector<uint8_t> fail_arr(16, 0xA5);
  assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGBA, GL_UNSIGNED_BYTE, fail_arr.data(), fail_arr.size()));
  for (uint8_t v : fail_arr) assert(v == 0xA5);
  ctx.user_pbo = 7;
  ctx.gl.bound_pbo = 7;
  ctx.gl.pbo_mem.assign(64, 0xA5);
  assert(ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGBA, GL_UNSIGNED_BYTE, nullptr, 56, 7, 8));
  for (uint8_t v : ctx.gl.pbo_mem) assert(v == 0xA5);
  assert(ctx.gl.bound_pbo == 7 && ctx.gl.pack_align == 4);
  ctx.gl.fail_read = false;
  ctx.user_pbo = 0;
  ctx.gl.bound_pbo = 0;

  // 2. Genuine 0xA5A5A5A5 bottom-right pixel succeeds via 1x1 0x5A probe.
  size_t last_off = (static_cast<size_t>(H - 1) * W + (W - 1)) * 4;
  std::fill_n(ctx.gl.source_bottom_up.begin() + static_cast<std::ptrdiff_t>(last_off), 4, 0xA5);
  std::vector<uint8_t> last_px(4, 0x11);
  assert(ctx.ReadFingerprintPixels(&fbo, W - 1, H - 1, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, last_px.data(), 4));
  for (uint8_t v : last_px) assert(v == 0xA5);

  // 3. Odd PBO offset for 16-bit packed types (565 / 4444) must delegate to driver validation.
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGB, GL_UNSIGNED_SHORT_5_6_5, nullptr, 64, 7, 1));
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGBA, GL_UNSIGNED_SHORT_4_4_4_4, nullptr, 64, 7, 1));

  // 4. Invalid pack combinations (skip_pixels + width > data_store_width) delegate to driver.
  ctx.user_pack = {4, 0, 2, 0};
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGBA, GL_UNSIGNED_BYTE, fail_arr.data(), fail_arr.size()));
  ctx.user_pack = {4, 2, 0, 0};
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, 4, 2, GL_RGBA, GL_UNSIGNED_BYTE, fail_arr.data(), fail_arr.size()));
  ctx.user_pack = {4, 0, 0, 0};

  // 5. x + width signed overflow delegates to driver.
  assert(!ctx.ReadFingerprintPixels(&fbo, std::numeric_limits<int32_t>::max(), 0, 2, 2, GL_RGBA, GL_UNSIGNED_BYTE, fail_arr.data(), fail_arr.size()));

  // 6. Multisampled sentinel (-1 extent) and unattached user FBO delegate to driver.
  ctx.gl.fbo_w = -1;
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGBA, GL_UNSIGNED_BYTE, fail_arr.data(), fail_arr.size()));
  ctx.gl.fbo_w = W;
  fbo.att_obj = nullptr;
  assert(!ctx.ReadFingerprintPixels(&fbo, 0, 0, 2, 2, GL_RGBA, GL_UNSIGNED_BYTE, fail_arr.data(), fail_arr.size()));
  return 0;
}
"""
    )
    executable = tmp_path / "readback_parity"
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-Werror",
            "-Wunsafe-buffer-usage",
            "-Wsign-compare",
            str(source),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    res = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stderr


def test_compiled_audio_and_client_rects_helpers(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("c++")
    if compiler is None:
        pytest.skip("C++ compiler required for native audio/rect regression")
    patch_text = AUDIO_RECTS_PATCH.read_text()
    perturb_fn = _extract_added_function(patch_text, "float PerturbAudioSample(")
    rect_fn = _extract_added_function(patch_text, "void OffsetNonEmptyClientRect(")
    source = tmp_path / "audio_rects_parity.cc"
    source.write_text(
        r"""
#include <cassert>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>

namespace gfx {
struct RectF {
  float x_, y_, w_, h_;
  bool IsEmpty() const { return w_ <= 0.0f || h_ <= 0.0f; }
  float x() const { return x_; }
  float y() const { return y_; }
  void Offset(float dx, float dy) { x_ += dx; y_ += dy; }
};
}  // namespace gfx

"""
        + perturb_fn
        + "\n"
        + rect_fn
        + r"""
static uint64_t SeedHash(const std::string& s) {
  uint64_t h = 14695981039346656037ULL;
  for (unsigned char c : s) {
    h ^= c;
    h *= 1099511628211ULL;
  }
  return h ? h : 1u;
}

int main() {
  const uint64_t s2 = SeedHash("2");
  const uint64_t s3 = SeedHash("3");
  const uint64_t s6 = SeedHash("6");
  // Silent, NaN, Inf, and non-audio >= 16.0f control values stay bit-exact.
  assert(PerturbAudioSample(0.0f, s2, 100) == 0.0f);
  assert(std::isnan(PerturbAudioSample(std::numeric_limits<float>::quiet_NaN(), s2, 100)));
  assert(PerturbAudioSample(std::numeric_limits<float>::infinity(), s2, 100) ==
         std::numeric_limits<float>::infinity());
  assert(PerturbAudioSample(440.0f, s2, 100) == 440.0f);
  assert(PerturbAudioSample(-16.0f, s2, 100) == -16.0f);
  // Audible range stays bounded (<= 4 * 2^-20) and differs deterministically per seed.
  const float a2 = PerturbAudioSample(0.35f, s2, 4500);
  const float a3 = PerturbAudioSample(0.35f, s3, 4500);
  const float a6 = PerturbAudioSample(0.35f, s6, 4500);
  assert(a2 != 0.35f && a3 != 0.35f && a6 != 0.35f);
  assert(a2 != a3 && a2 != a6 && a3 != a6);
  assert(PerturbAudioSample(0.35f, s2, 4500) == a2);
  assert(std::fabs(a2 - 0.35f) <= 4.0f * 9.5367431640625e-7f + 1e-12f);

  // Empty 0x0 rects are never shifted; (0,0) rects never go negative.
  gfx::RectF empty{0.0f, 0.0f, 0.0f, 0.0f};
  OffsetNonEmptyClientRect(empty, -0.0007, -0.0008);
  assert(empty.x_ == 0.0f && empty.y_ == 0.0f);
  gfx::RectF zero_origin{0.0f, 0.0f, 40.0f, 20.0f};
  OffsetNonEmptyClientRect(zero_origin, -0.0007, -0.0008);
  assert(zero_origin.x_ > 0.0f && zero_origin.y_ > 0.0f);
  gfx::RectF interior{10.0f, 20.0f, 40.0f, 20.0f};
  OffsetNonEmptyClientRect(interior, -0.0007, -0.0008);
  assert(interior.x_ < 10.0f && interior.y_ < 20.0f);
  return 0;
}
"""
    )
    executable = tmp_path / "audio_rects_parity"
    subprocess.run(
        [
            compiler,
            "-std=c++20",
            "-Werror",
            "-Wunsafe-buffer-usage",
            "-Wsign-compare",
            str(source),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    res = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stderr
