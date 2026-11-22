"""Fail-closed native-core acceptance in disposable contexts on loopback only.

No app init script is installed: worker protection must come from the built core.
Passing this matrix is not permission to replace a production core/profile.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def _protected_macos_fonts() -> frozenset[str]:
    patch_text = (
        Path(__file__).with_name("patches") / "028-pixelscan-noise-font-coherence.patch"
    ).read_text(encoding="utf-8")
    block = patch_text.split("kProtectedMacOsCoreFonts", 1)[1].split(
        "IsProtectedMacOsCoreFontsSorted", 1
    )[0]
    fonts = frozenset(re.findall(r'\+\s+"([^"]+)"', block))
    if len(fonts) != 196:
        raise ValueError(f"Expected 196 protected macOS core fonts in 028, got {len(fonts)}")
    return fonts


SAMPLE = """async function sample() {
  const adapter = await navigator.gpu?.requestAdapter();
  let stackReads = 0;
  const error = new Error('native acceptance');
  Object.defineProperty(error, 'stack', {get() {stackReads++; return 'probe';}});
  for (const name of ['debug','log','info','warn','error','table','dir']) console[name](error);
  await new Promise(resolve => setTimeout(resolve, 50));
  const canvas = new OffscreenCanvas(16,16); const ctx=canvas.getContext('2d');
  ctx.fillStyle='rgb(100,100,100)'; ctx.fillRect(0,0,16,16);
  const pixels=ctx.getImageData(0,0,16,16).data;
  ctx.font='16px Helvetica';
  const tm=ctx.measureText('Hello World 0123');
  return {
    gpu: adapter ? {vendor:adapter.info.vendor, architecture:adapter.info.architecture,
      device:adapter.info.device, description:adapter.info.description} : null,
    cpu: navigator.hardwareConcurrency, memory: navigator.deviceMemory, stackReads,
    solidChanged:Array.from(pixels).filter((v,i)=>v!==(i%4===3?255:100)).length,
    textMetrics: [tm.width, tm.actualBoundingBoxLeft, tm.actualBoundingBoxRight],
    leakedFakeShadow: typeof Element !== 'undefined' && 'fakeShadowRoot' in Element.prototype,
    globals: Object.getOwnPropertyNames(globalThis).filter(
      key => /^(\\$?cdc_|__playwright|__pw|__webdriver|__selenium|domAutomation)/i.test(key))
  };
} """
WORKER_SOURCE = SAMPLE + "self.onmessage=async()=>self.postMessage(await sample());"
COLLECT = """async () => {
  const sharedPorts = [];
  const timeout = promise => Promise.race([promise, new Promise((_,reject) =>
    setTimeout(() => reject(new Error('realm probe timeout')), 10000))]);
  const worker = async (url, options, Ctor=Worker) => {
    const w = new Ctor(url, options);
    try { return await timeout(new Promise((resolve,reject) => {
      w.onmessage=e=>resolve(e.data); w.onerror=e=>reject(new Error(e.message)); w.postMessage('go');
    })); } finally { w.terminate(); }
  };
  const shared = async options => {
    const w = new SharedWorker('/shared.js?sharing', options);
    sharedPorts.push(w.port);
    return await timeout(new Promise((resolve,reject) => {
      w.port.onmessage=e=>resolve(e.data); w.onerror=e=>reject(new Error(e.message));
      w.port.start(); w.port.postMessage('go');
    }));
  };
  const results = {window: await sample()};
  results.classic = await worker('/worker.js');
  results.module = await worker('/worker.js', {type:'module'});
  const first = await shared(null); results.shared = first.sample;
  const a = await shared({}); const b = await shared('');
  if (a.connection !== first.connection+1 || b.connection !== a.connection+1)
    throw new Error('SharedWorker default-name identity changed');
  let syncTypeError = false;
  try { new SharedWorker('/shared.js', {type:'invalid'}); }
  catch(e) {syncTypeError=e instanceof TypeError;}
  if (!syncTypeError) throw new Error('WorkerOptions validation is not synchronous');
  const frame = document.createElement('iframe'); frame.src='/';
  const loaded = new Promise(resolve=>frame.onload=resolve);
  document.body.append(frame); await timeout(loaded);
  const adapter = await navigator.gpu?.requestAdapter();
  if (!adapter || !frame.contentWindow.GPUAdapterInfo) throw new Error('GPU unavailable');
  const get = name => Object.getOwnPropertyDescriptor(
    frame.contentWindow.GPUAdapterInfo.prototype,name).get.call(adapter.info);
  results.borrowed = {vendor:get('vendor'), architecture:get('architecture')};
  const blob = URL.createObjectURL(new Blob([WORKER_SOURCE],{type:'text/javascript'}));
  try {
    results.blob = await worker(blob);
    results.iframeOwnedBlob = await worker(blob, {}, frame.contentWindow.Worker);
  } finally {URL.revokeObjectURL(blob);frame.remove();}
  const reg = await navigator.serviceWorker.register('/sw.js');
  try {
    await timeout(navigator.serviceWorker.ready);
    const channel = new MessageChannel();
    results.service = await timeout(new Promise(resolve=>{
      channel.port1.onmessage=e=>resolve(e.data);reg.active.postMessage('go',[channel.port2]);
    })); channel.port1.close();
  } finally {await reg.unregister();}
  const canvas = document.createElement('canvas'); canvas.width=canvas.height=16;
  const ctx=canvas.getContext('2d'); ctx.fillStyle='rgb(100,100,100)';ctx.fillRect(0,0,16,16);
  const pixels=ctx.getImageData(0,0,16,16).data;
  results.solidChanged=Array.from(pixels).filter((v,i)=>v!==(i%4===3?255:100)).length;
  for (let i=0;i<8;i++){ctx.fillText('😀',1,12);ctx.getImageData(0,0,16,16);canvas.toDataURL();}
  const drawScene = c => {
    const g = c.createLinearGradient(0,0,100,40);
    g.addColorStop(0,'#246'); g.addColorStop(1,'#c83');
    c.fillStyle = g; c.fillRect(0,0,100,40);
    c.fillStyle = '#fff'; c.font = '14px sans-serif'; c.fillText('Fox148 😀', 6, 26);
  };
  const mc = document.createElement('canvas'); mc.width = mc.height = 16;
  const mctx = mc.getContext('2d'); mctx.font = '16px Helvetica';
  const htm = mctx.measureText('Hello World 0123');
  results.htmlTextMetrics = [htm.width, htm.actualBoundingBoxLeft, htm.actualBoundingBoxRight];
  const tc = document.createElement('canvas'); tc.width = 100; tc.height = 40;
  const tctx = tc.getContext('2d'); drawScene(tctx);
  const full = tctx.getImageData(0,0,100,40).data;
  const sub = tctx.getImageData(10,10,80,20).data;
  let subDiff = 0;
  for (let y=0;y<20;y++) for (let x=0;x<80;x++) for (let ch=0;ch<4;ch++) {
    if (sub[(y*80+x)*4+ch] !== full[((y+10)*100+(x+10))*4+ch]) subDiff++;
  }
  results.subRectDiff = subDiff;
  const oc = new OffscreenCanvas(100,40); drawScene(oc.getContext('2d'));
  const bin = atob(tc.toDataURL('image/png').split(',')[1]);
  const urlBlob = new Blob([Uint8Array.from(bin, c => c.charCodeAt(0))], {type: 'image/png'});
  const cBlob = await timeout(new Promise(r => tc.toBlob(r, 'image/png')));
  const oBlob = await timeout(oc.convertToBlob({type: 'image/png'}));
  const decodeBlob = async b => {
    const bmp = await createImageBitmap(b);
    const out = new OffscreenCanvas(100,40).getContext('2d');
    out.drawImage(bmp, 0, 0); bmp.close();
    return out.getImageData(0,0,100,40).data;
  };
  const [dUrl, dBlob, dOff] = await Promise.all([decodeBlob(urlBlob), decodeBlob(cBlob), decodeBlob(oBlob)]);
  let blobDiff = 0;
  for (let i=0;i<dUrl.length;i++) if (dUrl[i] !== dBlob[i] || dUrl[i] !== dOff[i]) blobDiff++;
  results.blobMismatch = blobDiff;
  let webglOob = 0;
  for (const [w, h] of [[16,16],[17,16]]) {
    const gc = document.createElement('canvas'); gc.width = w; gc.height = h;
    const gl = gc.getContext('webgl');
    if (!gl) { webglOob++; continue; }
    while (gl.getError() !== gl.NO_ERROR) {}
    const drawGl = (fbo=null) => {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, w, h);
      gl.clearColor(0.3, 0.6, 0.9, 1.0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.SCISSOR_TEST); gl.scissor(0, 0, 8, h);
      gl.clearColor(0.8, 0.2, 0.4, 1.0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.disable(gl.SCISSOR_TEST);
    };
    drawGl(null);
    gl.pixelStorei(gl.PACK_ALIGNMENT, 1);
    const n8 = w * h * 4;
    const u8 = new Uint8Array(n8 + 16); u8.fill(0xA5, n8);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, u8);
    if (gl.getError() !== gl.NO_ERROR) webglOob++;
    if (!u8.subarray(0, n8).some(v => v !== 0)) webglOob++;
    for (let i=n8;i<n8+16;i++) if (u8[i] !== 0xA5) webglOob++;
    for (const [intFmt, fmt, typ] of [
      [gl.RGB565, gl.RGB, gl.UNSIGNED_SHORT_5_6_5],
      [gl.RGBA4, gl.RGBA, gl.UNSIGNED_SHORT_4_4_4_4],
    ]) {
      const fbo = gl.createFramebuffer();
      const rb = gl.createRenderbuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.bindRenderbuffer(gl.RENDERBUFFER, rb);
      gl.renderbufferStorage(gl.RENDERBUFFER, intFmt, w, h);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, rb);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
        webglOob++; gl.deleteRenderbuffer(rb); gl.deleteFramebuffer(fbo); continue;
      }
      drawGl(fbo);
      for (const align of [1, 4]) {
        gl.pixelStorei(gl.PACK_ALIGNMENT, align);
        const row = align === 4 && (w & 1) ? w + 1 : w;
        const total = row * (h - 1) + w;
        const buf = new Uint16Array(total + 8); buf.fill(0xBEEF, total);
        gl.readPixels(0, 0, w, h, fmt, typ, buf);
        if (gl.getError() !== gl.NO_ERROR) webglOob++;
        if (!buf.subarray(0, w).some(v => v !== 0)) webglOob++;
        for (let i=total;i<total+8;i++) if (buf[i] !== 0xBEEF) webglOob++;
      }
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.deleteRenderbuffer(rb); gl.deleteFramebuffer(fbo);
    }
  }
  results.webglOob = webglOob;
  let webglParityMismatch = 0;
  const sha256Hex = async bytes => Array.from(
    new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
  ).map(b => b.toString(16).padStart(2, '0')).join('');
  const drawParityScene = (gl, w, h, fbo=null) => {
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.viewport(0, 0, w, h);
    gl.disable(gl.SCISSOR_TEST);
    gl.clearColor(0.3, 0.6, 0.9, 1.0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.SCISSOR_TEST);
    gl.scissor(0, 0, Math.floor(w / 2), h);
    gl.clearColor(0.8, 0.2, 0.4, 1.0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.scissor(Math.floor(w / 4), Math.floor(h / 4), Math.max(2, Math.floor(w / 2)), Math.max(2, Math.floor(h / 2)));
    gl.clearColor(0.2, 0.75, 0.45, 1.0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.disable(gl.SCISSOR_TEST);
  };
  const pc1 = document.createElement('canvas'); pc1.width = pc1.height = 32;
  const gl1 = pc1.getContext('webgl', {preserveDrawingBuffer: true, antialias: false});
  if (!gl1) {
    webglParityMismatch++;
    results.webglDigest = '';
  } else {
    while (gl1.getError() !== gl1.NO_ERROR) {}
    drawParityScene(gl1, 32, 32, null);
    gl1.pixelStorei(gl1.PACK_ALIGNMENT, 4);
    const full4 = new Uint8Array(32 * 32 * 4);
    gl1.readPixels(0, 0, 32, 32, gl1.RGBA, gl1.UNSIGNED_BYTE, full4);
    results.webglDigest = await sha256Hex(full4);
    gl1.pixelStorei(gl1.PACK_ALIGNMENT, 8);
    const pack8 = new Uint8Array(32 * 32 * 4);
    gl1.readPixels(0, 0, 32, 32, gl1.RGBA, gl1.UNSIGNED_BYTE, pack8);
    gl1.pixelStorei(gl1.PACK_ALIGNMENT, 1);
    const pack1 = new Uint8Array(32 * 32 * 4);
    gl1.readPixels(0, 0, 32, 32, gl1.RGBA, gl1.UNSIGNED_BYTE, pack1);
    const tiny = new Uint8Array(32 * 32 * 4);
    const px = new Uint8Array(4);
    for (let y = 0; y < 32; y++) for (let x = 0; x < 32; x++) {
      gl1.readPixels(x, y, 1, 1, gl1.RGBA, gl1.UNSIGNED_BYTE, px);
      tiny.set(px, (y * 32 + x) * 4);
    }
    for (let i = 0; i < full4.length; i++) {
      if (pack8[i] !== full4[i] || pack1[i] !== full4[i] || tiny[i] !== full4[i]) webglParityMismatch++;
    }
    for (const align of [4, 8]) {
      gl1.pixelStorei(gl1.PACK_ALIGNMENT, align);
      const sub = new Uint8Array(24 * 24 * 4);
      gl1.readPixels(4, 4, 24, 24, gl1.RGBA, gl1.UNSIGNED_BYTE, sub);
      for (let r = 0; r < 24; r++) for (let c = 0; c < 24 * 4; c++) {
        if (sub[r * 24 * 4 + c] !== full4[(r + 4) * 32 * 4 + 4 * 4 + c]) webglParityMismatch++;
      }
    }
    gl1.pixelStorei(gl1.PACK_ALIGNMENT, 4);
    const neg = new Uint8Array(8 * 8 * 4);
    neg.fill(0x5A);
    gl1.readPixels(-2, -2, 8, 8, gl1.RGBA, gl1.UNSIGNED_BYTE, neg);
    for (let r = 0; r < 8; r++) for (let c = 0; c < 8; c++) {
      const dst = (r * 8 + c) * 4;
      if (r < 2 || c < 2) {
        if (neg[dst] !== 0 || neg[dst + 1] !== 0 || neg[dst + 2] !== 0 || neg[dst + 3] !== 0) webglParityMismatch++;
      } else {
        const src = ((r - 2) * 32 + (c - 2)) * 4;
        for (let ch = 0; ch < 4; ch++) if (neg[dst + ch] !== full4[src + ch]) webglParityMismatch++;
      }
    }
    if (gl1.getError() !== gl1.NO_ERROR) webglParityMismatch++;
  }
  const pc2 = document.createElement('canvas'); pc2.width = pc2.height = 32;
  const gl2 = pc2.getContext('webgl2', {preserveDrawingBuffer: true, antialias: false});
  if (!gl2) {
    webglParityMismatch++;
  } else {
    while (gl2.getError() !== gl2.NO_ERROR) {}
    const checkFboParity = (w, h, intFmt, fmt, typ, Arr, chPerPx, fboSetup) => {
      const fbo = gl2.createFramebuffer();
      const cleanup = fboSetup(fbo);
      try {
        if (gl2.checkFramebufferStatus(gl2.FRAMEBUFFER) !== gl2.FRAMEBUFFER_COMPLETE) {
          webglParityMismatch++; return;
        }
        drawParityScene(gl2, w, h, fbo);
        gl2.pixelStorei(gl2.PACK_ALIGNMENT, 4);
        const a4 = new Arr(w * h * chPerPx);
        gl2.readPixels(0, 0, w, h, fmt, typ, a4);
        gl2.pixelStorei(gl2.PACK_ALIGNMENT, 8);
        const a8 = new Arr(w * h * chPerPx);
        gl2.readPixels(0, 0, w, h, fmt, typ, a8);
        for (let i = 0; i < a4.length; i++) if (a8[i] !== a4[i]) webglParityMismatch++;
        const sx = Math.min(2, w - 1), sy = Math.min(3, h - 1);
        const sw = Math.max(1, w - 7), sh = Math.max(1, h - 6);
        const elemBytes = Arr.BYTES_PER_ELEMENT;
        const pxBytes = chPerPx * elemBytes;
        const rowElems = sw * chPerPx;
        for (const align of [4, 8]) {
          gl2.pixelStorei(gl2.PACK_ALIGNMENT, align);
          const rowBytes = sw * pxBytes;
          const strideBytes = Math.ceil(rowBytes / align) * align;
          const strideElems = strideBytes / elemBytes;
          const neededBytes = (sh - 1) * strideBytes + rowBytes;
          const sub = new Arr(Math.ceil(neededBytes / elemBytes));
          sub.fill(0x55);
          gl2.readPixels(sx, sy, sw, sh, fmt, typ, sub);
          for (let r = 0; r < sh; r++) {
            for (let c = 0; c < rowElems; c++) {
              if (sub[r * strideElems + c] !== a4[(r + sy) * w * chPerPx + sx * chPerPx + c]) {
                webglParityMismatch++;
              }
            }
            if (r < sh - 1) {
              for (let c = rowElems; c < strideElems; c++) {
                if (sub[r * strideElems + c] !== 0x55) webglParityMismatch++;
              }
            }
          }
        }
        gl2.pixelStorei(gl2.PACK_ALIGNMENT, 4);
        const one = new Arr(chPerPx);
        gl2.readPixels(sx + 1, sy + 1, 1, 1, fmt, typ, one);
        for (let ch = 0; ch < chPerPx; ch++) {
          if (one[ch] !== a4[((sy + 1) * w + sx + 1) * chPerPx + ch]) webglParityMismatch++;
        }
        if (fmt === gl2.RGB && typ === gl2.UNSIGNED_SHORT_5_6_5) {
          gl2.pixelStorei(gl2.PACK_ALIGNMENT, 4);
          const r4 = new Uint8Array(w * h * 4);
          gl2.readPixels(0, 0, w, h, gl2.RGBA, gl2.UNSIGNED_BYTE, r4);
          gl2.pixelStorei(gl2.PACK_ALIGNMENT, 8);
          const r8 = new Uint8Array(w * h * 4);
          gl2.readPixels(0, 0, w, h, gl2.RGBA, gl2.UNSIGNED_BYTE, r8);
          for (let i = 0; i < r4.length; i++) if (r8[i] !== r4[i]) webglParityMismatch++;
        }
      } finally {
        gl2.pixelStorei(gl2.PACK_ALIGNMENT, 4);
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, null);
        cleanup();
        gl2.deleteFramebuffer(fbo);
      }
    };
    checkFboParity(24, 20, gl2.RGBA8, gl2.RGBA, gl2.UNSIGNED_BYTE, Uint8Array, 4, fbo => {
      const rb = gl2.createRenderbuffer();
      gl2.bindFramebuffer(gl2.FRAMEBUFFER, fbo);
      gl2.bindRenderbuffer(gl2.RENDERBUFFER, rb);
      gl2.renderbufferStorage(gl2.RENDERBUFFER, gl2.RGBA8, 24, 20);
      gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, rb);
      return () => gl2.deleteRenderbuffer(rb);
    });
    checkFboParity(24, 20, gl2.RGBA8, gl2.RGBA, gl2.UNSIGNED_BYTE, Uint8Array, 4, fbo => {
      const tex = gl2.createTexture();
      gl2.bindFramebuffer(gl2.FRAMEBUFFER, fbo);
      gl2.bindTexture(gl2.TEXTURE_2D, tex);
      gl2.texImage2D(gl2.TEXTURE_2D, 0, gl2.RGBA8, 24, 20, 0, gl2.RGBA, gl2.UNSIGNED_BYTE, null);
      gl2.framebufferTexture2D(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.TEXTURE_2D, tex, 0);
      return () => gl2.deleteTexture(tex);
    });
    checkFboParity(16, 16, gl2.RGBA8, gl2.RGBA, gl2.UNSIGNED_BYTE, Uint8Array, 4, fbo => {
      const cube = gl2.createTexture();
      gl2.bindFramebuffer(gl2.FRAMEBUFFER, fbo);
      gl2.bindTexture(gl2.TEXTURE_CUBE_MAP, cube);
      for (let f = 0; f < 6; f++) {
        gl2.texImage2D(gl2.TEXTURE_CUBE_MAP_POSITIVE_X + f, 0, gl2.RGBA8, 16, 16, 0, gl2.RGBA, gl2.UNSIGNED_BYTE, null);
      }
      gl2.framebufferTexture2D(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.TEXTURE_CUBE_MAP_POSITIVE_Y, cube, 0);
      return () => gl2.deleteTexture(cube);
    });
    for (const [intFmt, fmt, typ] of [
      [gl2.RGB565, gl2.RGB, gl2.UNSIGNED_SHORT_5_6_5],
      [gl2.RGBA4, gl2.RGBA, gl2.UNSIGNED_SHORT_4_4_4_4],
    ]) {
      checkFboParity(24, 20, intFmt, fmt, typ, Uint16Array, 1, fbo => {
        const rb = gl2.createRenderbuffer();
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, fbo);
        gl2.bindRenderbuffer(gl2.RENDERBUFFER, rb);
        gl2.renderbufferStorage(gl2.RENDERBUFFER, intFmt, 24, 20);
        gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, rb);
        return () => gl2.deleteRenderbuffer(rb);
      });
    }
    drawParityScene(gl2, 32, 32, null);
    gl2.pixelStorei(gl2.PACK_ALIGNMENT, 4);
    gl2.pixelStorei(gl2.PACK_ROW_LENGTH, 0);
    gl2.pixelStorei(gl2.PACK_SKIP_PIXELS, 0);
    gl2.pixelStorei(gl2.PACK_SKIP_ROWS, 0);
    const full2 = new Uint8Array(32 * 32 * 4);
    gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.UNSIGNED_BYTE, full2);
    const pbo = gl2.createBuffer();
    const initBuf = new Uint8Array(2048); initBuf.fill(0xA5);
    gl2.bindBuffer(gl2.PIXEL_PACK_BUFFER, pbo);
    gl2.bufferData(gl2.PIXEL_PACK_BUFFER, initBuf, gl2.STREAM_READ);
    gl2.pixelStorei(gl2.PACK_ALIGNMENT, 8);
    gl2.pixelStorei(gl2.PACK_ROW_LENGTH, 17);
    gl2.pixelStorei(gl2.PACK_SKIP_PIXELS, 2);
    gl2.pixelStorei(gl2.PACK_SKIP_ROWS, 1);
    gl2.readPixels(4, 4, 15, 10, gl2.RGBA, gl2.UNSIGNED_BYTE, 16);
    if (gl2.getError() !== gl2.NO_ERROR) webglParityMismatch++;
    if (gl2.getParameter(gl2.PACK_ALIGNMENT) !== 8 ||
        gl2.getParameter(gl2.PACK_ROW_LENGTH) !== 17 ||
        gl2.getParameter(gl2.PACK_SKIP_PIXELS) !== 2 ||
        gl2.getParameter(gl2.PACK_SKIP_ROWS) !== 1 ||
        gl2.getParameter(gl2.PIXEL_PACK_BUFFER_BINDING) !== pbo) {
      webglParityMismatch++;
    }
    const pboOut = new Uint8Array(2048);
    gl2.getBufferSubData(gl2.PIXEL_PACK_BUFFER, 0, pboOut);
    const startOff = 16 + 1 * 72 + 2 * 4;
    for (let i = 0; i < startOff; i++) if (pboOut[i] !== 0xA5) webglParityMismatch++;
    for (let r = 0; r < 10; r++) {
      const rowOff = startOff + r * 72;
      for (let b = 0; b < 60; b++) {
        if (pboOut[rowOff + b] !== full2[(r + 4) * 32 * 4 + 4 * 4 + b]) webglParityMismatch++;
      }
      if (r < 9) {
        for (let b = 60; b < 72; b++) if (pboOut[rowOff + b] !== 0xA5) webglParityMismatch++;
      }
    }
    const endOff = startOff + 9 * 72 + 60;
    for (let i = endOff; i < pboOut.length; i++) if (pboOut[i] !== 0xA5) webglParityMismatch++;
    gl2.bindBuffer(gl2.PIXEL_PACK_BUFFER, null);
    gl2.deleteBuffer(pbo);
    gl2.pixelStorei(gl2.PACK_ALIGNMENT, 4);
    gl2.pixelStorei(gl2.PACK_ROW_LENGTH, 0);
    gl2.pixelStorei(gl2.PACK_SKIP_PIXELS, 0);
    gl2.pixelStorei(gl2.PACK_SKIP_ROWS, 0);
    const bad = new Uint16Array(32 * 32);
    gl2.readPixels(0, 0, 32, 32, gl2.RGB, gl2.UNSIGNED_SHORT_5_6_5, bad);
    if (gl2.getError() !== gl2.INVALID_OPERATION) webglParityMismatch++;
    const msSamples = Math.min(4, gl2.getParameter(gl2.MAX_SAMPLES));
    if (msSamples >= 2) {
      const fMs = gl2.createFramebuffer(), rMs = gl2.createRenderbuffer();
      const fSs = gl2.createFramebuffer(), rSs = gl2.createRenderbuffer();
      const bMs = gl2.createBuffer();
      try {
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, fSs);
        gl2.bindRenderbuffer(gl2.RENDERBUFFER, rSs);
        gl2.renderbufferStorage(gl2.RENDERBUFFER, gl2.RGBA8, 16, 16);
        gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, rSs);
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, fMs);
        gl2.bindRenderbuffer(gl2.RENDERBUFFER, rMs);
        gl2.renderbufferStorageMultisample(gl2.RENDERBUFFER, msSamples, gl2.RGBA8, 16, 16);
        gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, rMs);
        while (gl2.getError() !== gl2.NO_ERROR) {}
        const msArr = new Uint8Array(16).fill(0xA5);
        gl2.readPixels(0, 0, 2, 2, gl2.RGBA, gl2.UNSIGNED_BYTE, msArr);
        if (gl2.getError() !== gl2.INVALID_OPERATION || msArr.some(v => v !== 0xA5)) webglParityMismatch++;
        gl2.bindFramebuffer(gl2.READ_FRAMEBUFFER, fMs);
        gl2.bindFramebuffer(gl2.DRAW_FRAMEBUFFER, fSs);
        gl2.bindBuffer(gl2.PIXEL_PACK_BUFFER, bMs);
        gl2.bufferData(gl2.PIXEL_PACK_BUFFER, new Uint8Array(64).fill(0xA5), gl2.STREAM_READ);
        gl2.readPixels(0, 0, 2, 2, gl2.RGBA, gl2.UNSIGNED_BYTE, 8);
        const msPboErr = gl2.getError();
        const msPboOut = new Uint8Array(64);
        gl2.getBufferSubData(gl2.PIXEL_PACK_BUFFER, 0, msPboOut);
        if (msPboErr !== gl2.INVALID_OPERATION || msPboOut.some(v => v !== 0xA5)) webglParityMismatch++;
        gl2.bindBuffer(gl2.PIXEL_PACK_BUFFER, null);
      } finally {
        gl2.bindBuffer(gl2.PIXEL_PACK_BUFFER, null);
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, null);
        gl2.deleteBuffer(bMs);
        gl2.deleteRenderbuffer(rMs);
        gl2.deleteFramebuffer(fMs);
        gl2.deleteRenderbuffer(rSs);
        gl2.deleteFramebuffer(fSs);
      }
    }
    for (const [intFmt, fmt, typ] of [
      [gl2.RGB565, gl2.RGB, gl2.UNSIGNED_SHORT_5_6_5],
      [gl2.RGBA4, gl2.RGBA, gl2.UNSIGNED_SHORT_4_4_4_4],
    ]) {
      const f = gl2.createFramebuffer(), r = gl2.createRenderbuffer(), b = gl2.createBuffer();
      try {
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, f);
        gl2.bindRenderbuffer(gl2.RENDERBUFFER, r);
        gl2.renderbufferStorage(gl2.RENDERBUFFER, intFmt, 16, 16);
        gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, r);
        gl2.clearColor(0.7, 0.3, 0.5, 1.0); gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.bindBuffer(gl2.PIXEL_PACK_BUFFER, b);
        gl2.bufferData(gl2.PIXEL_PACK_BUFFER, new Uint8Array(64).fill(0xA5), gl2.STREAM_READ);
        while (gl2.getError() !== gl2.NO_ERROR) {}
        gl2.readPixels(0, 0, 2, 2, fmt, typ, 1);
        const oddErr = gl2.getError();
        const oddOut = new Uint8Array(64);
        gl2.getBufferSubData(gl2.PIXEL_PACK_BUFFER, 0, oddOut);
        if (oddErr !== gl2.INVALID_OPERATION || oddOut.some(v => v !== 0xA5)) webglParityMismatch++;
      } finally {
        gl2.bindBuffer(gl2.PIXEL_PACK_BUFFER, null);
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, null);
        gl2.deleteBuffer(b);
        gl2.deleteRenderbuffer(r);
        gl2.deleteFramebuffer(f);
      }
    }
    {
      const f = gl2.createFramebuffer(), r = gl2.createRenderbuffer();
      try {
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, f);
        gl2.bindRenderbuffer(gl2.RENDERBUFFER, r);
        gl2.renderbufferStorage(gl2.RENDERBUFFER, gl2.RGBA8, 8, 8);
        gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, r);
        while (gl2.getError() !== gl2.NO_ERROR) {}
        const invPack = new Uint8Array(64).fill(0xA5);
        gl2.pixelStorei(gl2.PACK_SKIP_PIXELS, 2);
        gl2.pixelStorei(gl2.PACK_ROW_LENGTH, 0);
        gl2.readPixels(0, 0, 2, 2, gl2.RGBA, gl2.UNSIGNED_BYTE, invPack);
        if (gl2.getError() !== gl2.INVALID_OPERATION || invPack.some(v => v !== 0xA5)) webglParityMismatch++;
        gl2.pixelStorei(gl2.PACK_SKIP_PIXELS, 0);
        gl2.pixelStorei(gl2.PACK_ROW_LENGTH, 2);
        gl2.readPixels(0, 0, 4, 2, gl2.RGBA, gl2.UNSIGNED_BYTE, invPack);
        if (gl2.getError() !== gl2.INVALID_OPERATION || invPack.some(v => v !== 0xA5)) webglParityMismatch++;
        gl2.pixelStorei(gl2.PACK_ROW_LENGTH, 0);
        const ovArr = new Uint8Array(16).fill(0xA5);
        gl2.readPixels(2147483647, 0, 2, 2, gl2.RGBA, gl2.UNSIGNED_BYTE, ovArr);
        if (gl2.getError() !== gl2.INVALID_OPERATION || ovArr.some(v => v !== 0xA5)) webglParityMismatch++;
        gl2.enable(0x1234);
        gl2.readPixels(0, 0, 2, 2, gl2.RGBA, gl2.UNSIGNED_BYTE, ovArr);
        if (gl2.getError() !== gl2.INVALID_ENUM) webglParityMismatch++;
      } finally {
        gl2.pixelStorei(gl2.PACK_SKIP_PIXELS, 0);
        gl2.pixelStorei(gl2.PACK_ROW_LENGTH, 0);
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, null);
        gl2.deleteRenderbuffer(r);
        gl2.deleteFramebuffer(f);
      }
    }
  }
  results.webglParityMismatch = webglParityMismatch;
  let extParityMismatch = 0;
  let digest5551 = '';
  let digestFloat = '';
  let digestHalfFloat = '';
  if (gl2) {
    const extCBF = gl2.getExtension('EXT_color_buffer_float');
    // 1) Packed RGBA5551 FBO readback parity & solid fill preservation.
    {
      const f = gl2.createFramebuffer();
      const r = gl2.createRenderbuffer();
      gl2.bindFramebuffer(gl2.FRAMEBUFFER, f);
      gl2.bindRenderbuffer(gl2.RENDERBUFFER, r);
      gl2.renderbufferStorage(gl2.RENDERBUFFER, gl2.RGB5_A1, 32, 32);
      gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, r);
      if (gl2.checkFramebufferStatus(gl2.FRAMEBUFFER) === gl2.FRAMEBUFFER_COMPLETE) {
        gl2.viewport(0, 0, 32, 32);
        gl2.disable(gl2.SCISSOR_TEST);
        gl2.clearColor(0.25, 0.5, 0.75, 1.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        const solidA = new Uint16Array(32 * 32);
        gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.UNSIGNED_SHORT_5_5_5_1, solidA);
        const first5551 = solidA[0];
        if (!solidA.every(v => v === first5551)) extParityMismatch++;
        gl2.clearColor(0.0, 0.0, 0.0, 0.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.enable(gl2.SCISSOR_TEST);
        gl2.scissor(4, 4, 20, 20);
        gl2.clearColor(1.0, 0.0, 0.0, 1.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.disable(gl2.SCISSOR_TEST);
        const mono5551 = new Uint16Array(32 * 32);
        gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.UNSIGNED_SHORT_5_5_5_1, mono5551);
        for (let row = 0; row < 32; row++) {
          for (let col = 0; col < 32; col++) {
            const expected = (row >= 4 && row < 24 && col >= 4 && col < 24) ? 0xF801 : 0;
            if (mono5551[row * 32 + col] !== expected) {
              extParityMismatch++;
              row = 32;
              break;
            }
          }
        }
        gl2.clearColor(0.25, 0.5, 0.75, 1.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.enable(gl2.SCISSOR_TEST);
        gl2.scissor(4, 4, 20, 20);
        gl2.clearColor(0.75, 0.25, 0.5, 1.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.disable(gl2.SCISSOR_TEST);
        const full5551 = new Uint16Array(32 * 32);
        const sub5551 = new Uint16Array(16 * 16);
        gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.UNSIGNED_SHORT_5_5_5_1, full5551);
        gl2.readPixels(6, 6, 16, 16, gl2.RGBA, gl2.UNSIGNED_SHORT_5_5_5_1, sub5551);
        for (let row = 0; row < 16; row++) {
          for (let col = 0; col < 16; col++) {
            if (sub5551[row * 16 + col] !== full5551[(row + 6) * 32 + col + 6]) {
              extParityMismatch++;
            }
          }
        }
        if (gl2.getError() !== gl2.NO_ERROR) extParityMismatch++;
        digest5551 = await sha256Hex(full5551);
      } else {
        extParityMismatch++;
      }
      gl2.bindFramebuffer(gl2.FRAMEBUFFER, null);
      gl2.deleteRenderbuffer(r);
      gl2.deleteFramebuffer(f);
    }
    // 2) Float / HalfFloat FBO readback parity, RGBA8->RGBA32F blit, and HDR preservation.
    if (extCBF) {
      const mkFbo = (intFmt, w, h) => {
        const f = gl2.createFramebuffer();
        const r = gl2.createRenderbuffer();
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, f);
        gl2.bindRenderbuffer(gl2.RENDERBUFFER, r);
        gl2.renderbufferStorage(gl2.RENDERBUFFER, intFmt, w, h);
        gl2.framebufferRenderbuffer(gl2.FRAMEBUFFER, gl2.COLOR_ATTACHMENT0, gl2.RENDERBUFFER, r);
        return {f, r};
      };
      const src = mkFbo(gl2.RGBA8, 32, 32);
      const dst32 = mkFbo(gl2.RGBA32F, 32, 32);
      const dst16 = mkFbo(gl2.RGBA16F, 32, 32);
      const prog = gl2.createProgram();
      const vs = gl2.createShader(gl2.VERTEX_SHADER);
      const fs = gl2.createShader(gl2.FRAGMENT_SHADER);
      gl2.shaderSource(vs, 'attribute vec2 p; varying vec2 v; void main(){v=p*0.5+0.5;gl_Position=vec4(p,0.,1.);}');
      gl2.shaderSource(fs, 'precision mediump float; varying vec2 v; void main(){gl_FragColor=vec4(0.2+0.6*v.x, 0.3+0.5*v.y, 0.7, 1.0);}');
      gl2.compileShader(vs);
      gl2.compileShader(fs);
      gl2.attachShader(prog, vs);
      gl2.attachShader(prog, fs);
      gl2.linkProgram(prog);
      const vb = gl2.createBuffer();
      gl2.bindBuffer(gl2.ARRAY_BUFFER, vb);
      gl2.bufferData(gl2.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl2.STATIC_DRAW);
      gl2.useProgram(prog);
      const loc = gl2.getAttribLocation(prog, 'p');
      gl2.enableVertexAttribArray(loc);
      gl2.vertexAttribPointer(loc, 2, gl2.FLOAT, false, 0, 0);
      try {
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, src.f);
        gl2.viewport(0, 0, 32, 32);
        gl2.drawArrays(gl2.TRIANGLE_STRIP, 0, 4);
        gl2.bindFramebuffer(gl2.READ_FRAMEBUFFER, src.f);
        gl2.bindFramebuffer(gl2.DRAW_FRAMEBUFFER, dst32.f);
        gl2.blitFramebuffer(0, 0, 32, 32, 0, 0, 32, 32, gl2.COLOR_BUFFER_BIT, gl2.NEAREST);
        gl2.bindFramebuffer(gl2.DRAW_FRAMEBUFFER, dst16.f);
        gl2.blitFramebuffer(0, 0, 32, 32, 0, 0, 32, 32, gl2.COLOR_BUFFER_BIT, gl2.NEAREST);
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, dst32.f);
        const f32 = new Float32Array(32 * 32 * 4);
        const f32Sub = new Float32Array(12 * 12 * 4);
        gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.FLOAT, f32);
        gl2.readPixels(8, 8, 12, 12, gl2.RGBA, gl2.FLOAT, f32Sub);
        for (let r = 0; r < 12; r++) {
          for (let c = 0; c < 12 * 4; c++) {
            if (f32Sub[r * 12 * 4 + c] !== f32[(r + 8) * 32 * 4 + 8 * 4 + c]) extParityMismatch++;
          }
        }
        digestFloat = await sha256Hex(f32);
        // Ensure RGBA32F -> HALF_FLOAT remains rejected cleanly by driver validation.
        gl2.readPixels(0, 0, 4, 4, gl2.RGBA, gl2.HALF_FLOAT, new Uint16Array(4 * 4 * 4));
        if (gl2.getError() !== gl2.INVALID_OPERATION) extParityMismatch++;
        // Out-of-gamut HDR solid/edge pixels (<0 / >1) must stay untouched.
        gl2.clearColor(-0.5, 1.5, 2.25, 1.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.enable(gl2.SCISSOR_TEST);
        gl2.scissor(2, 2, 16, 16);
        gl2.clearColor(-0.25, 3.5, 1.75, 1.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.disable(gl2.SCISSOR_TEST);
        const hdr = new Float32Array(32 * 32 * 4);
        gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.FLOAT, hdr);
        if (hdr[0] !== -0.5 || hdr[1] !== 1.5 || hdr[(2 * 32 + 2) * 4] !== -0.25 || hdr[(2 * 32 + 2) * 4 + 1] !== 3.5) {
          extParityMismatch++;
        }
        gl2.clearColor(0.0, 0.0, 0.0, 0.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.enable(gl2.SCISSOR_TEST);
        gl2.scissor(4, 4, 20, 20);
        gl2.clearColor(1.0, 0.0, 0.0, 1.0);
        gl2.clear(gl2.COLOR_BUFFER_BIT);
        gl2.disable(gl2.SCISSOR_TEST);
        const monoF32 = new Float32Array(32 * 32 * 4);
        gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.FLOAT, monoF32);
        for (let row = 0; row < 32; row++) {
          for (let col = 0; col < 32; col++) {
            const inside = row >= 4 && row < 24 && col >= 4 && col < 24;
            const idx = (row * 32 + col) * 4;
            if (monoF32[idx] !== (inside ? 1 : 0) || monoF32[idx + 1] !== 0 ||
                monoF32[idx + 2] !== 0 || monoF32[idx + 3] !== (inside ? 1 : 0)) {
              extParityMismatch++;
              row = 32;
              break;
            }
          }
        }
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, dst16.f);
        const h16 = new Uint16Array(32 * 32 * 4);
        gl2.readPixels(0, 0, 32, 32, gl2.RGBA, gl2.HALF_FLOAT, h16);
        if (gl2.getError() !== gl2.NO_ERROR) extParityMismatch++;
        digestHalfFloat = await sha256Hex(h16);
      } finally {
        gl2.bindFramebuffer(gl2.FRAMEBUFFER, null);
        for (const pair of [src, dst32, dst16]) {
          gl2.deleteRenderbuffer(pair.r);
          gl2.deleteFramebuffer(pair.f);
        }
        gl2.deleteBuffer(vb);
        gl2.deleteProgram(prog);
        gl2.deleteShader(vs);
        gl2.deleteShader(fs);
      }
    } else {
      extParityMismatch++;
    }
  }
  results.webglExtendedParityMismatch = extParityMismatch;
  results.webgl5551Digest = digest5551;
  results.webglFloatDigest = digestFloat;
  results.webglHalfFloatDigest = digestHalfFloat;
  {
    const rc = document.createElement('canvas');
    rc.width = 256;
    rc.height = 24;
    const rgl = rc.getContext('webgl', {preserveDrawingBuffer: true});
    let redBoxDigest = '';
    if (rgl) {
      const idx = [3, 2, 1, 3, 1, 0];
      const vb = rgl.createBuffer();
      const ib = rgl.createBuffer();
      const vs = rgl.createShader(rgl.VERTEX_SHADER);
      const fs = rgl.createShader(rgl.FRAGMENT_SHADER);
      const prog = rgl.createProgram();
      try {
        rgl.bindBuffer(rgl.ARRAY_BUFFER, vb);
        rgl.bufferData(rgl.ARRAY_BUFFER, new Float32Array([-0.75, 0.75, 0, -0.75, -0.75, 0, 0.75, -0.75, 0, 0.75, 0.75, 0]), rgl.STATIC_DRAW);
        rgl.bindBuffer(rgl.ARRAY_BUFFER, null);
        rgl.bindBuffer(rgl.ELEMENT_ARRAY_BUFFER, ib);
        rgl.bufferData(rgl.ELEMENT_ARRAY_BUFFER, new Uint16Array(idx), rgl.STATIC_DRAW);
        rgl.bindBuffer(rgl.ELEMENT_ARRAY_BUFFER, null);
        rgl.shaderSource(vs, 'attribute vec3 coordinates;void main(void) { gl_Position = vec4(coordinates, 1.0);}');
        rgl.compileShader(vs);
        rgl.shaderSource(fs, 'void main(void) { gl_FragColor = vec4(255, 0, 0, 1);}');
        rgl.compileShader(fs);
        rgl.attachShader(prog, vs);
        rgl.attachShader(prog, fs);
        rgl.linkProgram(prog);
        rgl.useProgram(prog);
        rgl.bindBuffer(rgl.ARRAY_BUFFER, vb);
        rgl.bindBuffer(rgl.ELEMENT_ARRAY_BUFFER, ib);
        const loc = rgl.getAttribLocation(prog, 'coordinates');
        rgl.vertexAttribPointer(loc, 3, rgl.FLOAT, false, 0, 0);
        rgl.enableVertexAttribArray(loc);
        rgl.clearColor(0, 0, 0, 0);
        rgl.enable(rgl.DEPTH_TEST);
        rgl.clear(rgl.COLOR_BUFFER_BIT);
        rgl.viewport(0, 0, rc.width, rc.height);
        rgl.drawElements(rgl.TRIANGLES, idx.length, rgl.UNSIGNED_SHORT, 0);
        const pixels = new Uint8Array(rgl.drawingBufferWidth * rgl.drawingBufferHeight * 4);
        rgl.readPixels(0, 0, rgl.drawingBufferWidth, rgl.drawingBufferHeight, rgl.RGBA, rgl.UNSIGNED_BYTE, pixels);
        redBoxDigest = await sha256Hex(pixels);
      } finally {
        rgl.deleteBuffer(vb);
        rgl.deleteBuffer(ib);
        rgl.deleteProgram(prog);
        rgl.deleteShader(vs);
        rgl.deleteShader(fs);
      }
    }
    results.webglRedBoxDigest = redBoxDigest;
  }
  {
    const fc = document.createElement('canvas');
    fc.width = 64;
    fc.height = 24;
    const fctx = fc.getContext('2d');
    fctx.clearRect(0, 0, 64, 24);
    fctx.fillStyle = 'rgb(255, 0, 0)';
    fctx.fillRect(8, 3, 48, 18);
    const raw = fctx.getImageData(0, 0, 64, 24).data;
    const pngBin = atob(fc.toDataURL('image/png').split(',')[1]);
    const pngBlob = new Blob([Uint8Array.from(pngBin, ch => ch.charCodeAt(0))], {type: 'image/png'});
    const pngBmp = await createImageBitmap(pngBlob);
    const decCtx = new OffscreenCanvas(64, 24).getContext('2d');
    decCtx.drawImage(pngBmp, 0, 0);
    pngBmp.close();
    const dec = decCtx.getImageData(0, 0, 64, 24).data;
    let flatChanged = 0;
    for (let y = 0; y < 24; y++) {
      for (let x = 0; x < 64; x++) {
        const inside = x >= 8 && x < 56 && y >= 3 && y < 21;
        const off = (y * 64 + x) * 4;
        const er = inside ? 255 : 0;
        const ea = inside ? 255 : 0;
        if (raw[off] !== er || raw[off + 1] !== 0 || raw[off + 2] !== 0 || raw[off + 3] !== ea) flatChanged++;
        if (dec[off] !== er || dec[off + 1] !== 0 || dec[off + 2] !== 0 || dec[off + 3] !== ea) flatChanged++;
      }
    }
    results.flatMonochromeChanged = flatChanged;
  }
  // 3) OfflineAudioContext exact sampleRate, deterministic seed digest, and silent preservation.
  {
    const ac = new OfflineAudioContext(1, 8192, 44100);
    results.audioSampleRate = ac.sampleRate;
    const osc = ac.createOscillator();
    osc.type = 'triangle';
    osc.frequency.value = 10000;
    const comp = ac.createDynamicsCompressor();
    osc.connect(comp);
    comp.connect(ac.destination);
    osc.start(0);
    const rendered = await ac.startRendering();
    results.audioDigest = await sha256Hex(rendered.getChannelData(0).subarray(4500, 5000));
    const silentCtx = new OfflineAudioContext(1, 512, 44100);
    const zeroBuf = silentCtx.createBuffer(1, 512, 44100);
    const bs = silentCtx.createBufferSource();
    bs.buffer = zeroBuf;
    bs.connect(silentCtx.destination);
    bs.start(0);
    const silentRendered = await silentCtx.startRendering();
    let maxSilent = 0;
    const sc = silentRendered.getChannelData(0);
    for (let i = 0; i < sc.length; i++) maxSilent = Math.max(maxSilent, Math.abs(sc[i]));
    results.audioSilentMax = maxSilent;
  }
  // 4) ClientRects: 0x0 Element/Range stay un-shifted, and (0,0) elements never go negative.
  if (typeof document !== 'undefined' && document.body) {
    let rectMismatch = 0;
    const prevMargin = document.body.style.margin;
    const prevPadding = document.body.style.padding;
    document.body.style.margin = '0';
    document.body.style.padding = '0';
    const wrap = document.createElement('div');
    wrap.innerHTML = '<div id="rEmptyBox" style="width:0;height:0"></div><div id="rZeroBox" style="width:40px;height:20px">A</div><div style="position:absolute;left:0;top:0;width:100px;font:16px monospace;line-height:20px;text-indent:20px"><span id="rMultiSpan">abcdefgh ijklmnop qrstuvwx yz abcde</span></div>';
    document.body.prepend(wrap);
    try {
      const zBox = document.getElementById('rZeroBox');
      const eBox = document.getElementById('rEmptyBox');
      const mSpan = document.getElementById('rMultiSpan');
      const zr = zBox.getBoundingClientRect();
      const zrs = zBox.getClientRects()[0];
      const er = eBox.getBoundingClientRect();
      if (!(zr.x >= 0 && zr.y >= 0 && zr.x < 0.002 && zr.y < 0.002)) rectMismatch++;
      if (zr.x !== zrs.x || zr.y !== zrs.y) rectMismatch++;
      if (er.x !== 0 || er.y !== 0 || er.width !== 0 || er.height !== 0) rectMismatch++;
      const range = document.createRange();
      range.setStart(zBox.firstChild, 0);
      range.collapse(true);
      const rr = range.getBoundingClientRect();
      if (rr.x !== 0 || rr.y !== 0 || rr.width !== 0) rectMismatch++;
      const emptyRange = document.createRange();
      emptyRange.setStart(eBox, 0);
      emptyRange.collapse(true);
      const rr0 = emptyRange.getBoundingClientRect();
      if (rr0.x !== 0 || rr0.y !== 0 || rr0.width !== 0 || rr0.height !== 0) rectMismatch++;
      const checkUnionParity = (bbox, frags) => {
        const nonEmpty = Array.from(frags).filter(f => f.width > 0 && f.height > 0);
        if (nonEmpty.length < 2) { rectMismatch++; return; }
        let ux = Infinity, uy = Infinity, ur = -Infinity, ub = -Infinity;
        for (const f of nonEmpty) {
          if (f.x < 0 || f.y < 0) rectMismatch++;
          ux = Math.min(ux, f.x);
          uy = Math.min(uy, f.y);
          ur = Math.max(ur, f.right);
          ub = Math.max(ub, f.bottom);
        }
        if (bbox.x < 0 || bbox.y < 0) rectMismatch++;
        if (Math.abs(bbox.x - ux) > 1e-5 || Math.abs(bbox.y - uy) > 1e-5 ||
            Math.abs(bbox.right - ur) > 1e-5 || Math.abs(bbox.bottom - ub) > 1e-5) {
          rectMismatch++;
        }
      };
      checkUnionParity(mSpan.getBoundingClientRect(), mSpan.getClientRects());
      const mRange = document.createRange();
      mRange.selectNodeContents(mSpan);
      checkUnionParity(mRange.getBoundingClientRect(), mRange.getClientRects());
    } finally {
      wrap.remove();
      document.body.style.margin = prevMargin;
      document.body.style.padding = prevPadding;
    }
    results.clientRectMismatch = rectMismatch;
  }
  sharedPorts.forEach(port=>port.close());
  return results;
} """
STRICT = """async () => {
  if (!Worker.toString().includes('[native code]') || Worker.prototype.constructor!==Worker)
    throw new Error('Worker constructor is wrapped');
  const result = await new Promise((resolve,reject)=>{
    const w = new Worker('/worker.js');
    const timer = setTimeout(()=>{w.terminate();reject(new Error('strict CSP timeout'));},10000);
    w.onmessage=e=>{clearTimeout(timer);w.terminate();resolve(e.data)};
    w.onerror=e=>{clearTimeout(timer);w.terminate();reject(new Error(e.message))};w.postMessage('go');
  });return result;
} """


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        scripts = {
            "/worker.js": WORKER_SOURCE,
            "/shared.js": SAMPLE
            + "let connections=0;self.onconnect=e=>{const connection=++connections;"
            "const p=e.ports[0];p.onmessage=async()=>p.postMessage({connection,sample:await sample()});p.start()};",
            "/sw.js": SAMPLE + "self.onmessage=async e=>e.ports[0].postMessage(await sample());",
            "/locale-worker.js": "self.onmessage=()=>self.postMessage({"
            "dtf:Intl.DateTimeFormat().resolvedOptions().locale,"
            "nf:Intl.NumberFormat().resolvedOptions().locale,"
            "num:(123456.789).toLocaleString(),"
            "langs:Array.from(navigator.languages||[])});",
            "/locale-shared.js": "self.onconnect=e=>{const p=e.ports[0];p.onmessage=()=>p.postMessage({"
            "dtf:Intl.DateTimeFormat().resolvedOptions().locale,"
            "nf:Intl.NumberFormat().resolvedOptions().locale,"
            "num:(123456.789).toLocaleString(),"
            "langs:Array.from(navigator.languages||[])});p.start();};",
            "/locale-sw.js": "self.onmessage=e=>e.ports[0].postMessage({"
            "dtf:Intl.DateTimeFormat().resolvedOptions().locale,"
            "nf:Intl.NumberFormat().resolvedOptions().locale,"
            "num:(123456.789).toLocaleString(),"
            "langs:Array.from(navigator.languages||[])});",
        }
        if path == "/locale-popup":
            body = (
                "<!doctype html><head><script>"
                "window.__firstInline={"
                "lang:navigator.language,"
                "langs:Array.from(navigator.languages||[]),"
                "dtf:Intl.DateTimeFormat().resolvedOptions().locale,"
                "nf:Intl.NumberFormat().resolvedOptions().locale,"
                "num:(123456.789).toLocaleString()};"
                "window.__seenLocales=[window.__firstInline.dtf];"
                "setInterval(()=>{const c=Intl.DateTimeFormat().resolvedOptions().locale;"
                "if(window.__seenLocales[window.__seenLocales.length-1]!==c)"
                "window.__seenLocales.push(c);},1);"
                "</script></head><body>ok</body>"
            )
        else:
            body = scripts.get(path, "<!doctype html><title>Native acceptance</title><body></body>")
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if path in scripts else "text/html")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "worker-src 'self'" if path == "/strict" else "worker-src 'self' blob:",
        )
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_):
        pass


def check_sample(value: dict, vendor: str, architecture: str) -> None:
    if not isinstance(value, dict) or value.get("gpu") != {
        "vendor": vendor,
        "architecture": architecture,
        "device": "",
        "description": "",
    }:
        raise ValueError("Native GPU identity mismatch/unavailable")
    if value.get("cpu") != 20 or value.get("memory") != 8:
        raise ValueError("Native CPU/deviceMemory mismatch")
    if value.get("stackReads") != 0 or value.get("globals") != []:
        raise ValueError("Native CDP side-channel/global probe failed")
    if value.get("solidChanged") != 0:
        raise ValueError("Native OffscreenCanvas solid fill changed")
    if value.get("leakedFakeShadow"):
        raise ValueError("Element.prototype leaked fakeShadowRoot")
    metrics = value.get("textMetrics")
    if (
        not isinstance(metrics, list)
        or len(metrics) != 3
        or not all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
            for x in metrics
        )
        or metrics[0] <= 0
    ):
        raise ValueError("Native measureText metrics missing")


async def verify(executable: Path, output: Path) -> None:
    from playwright.async_api import async_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resolved_executable = executable.resolve()
    with resolved_executable.open("rb") as binary:
        binary_sha256 = hashlib.file_digest(binary, "sha256").hexdigest()
    report = {"binary_sha256": binary_sha256, "acceptance": "FAILED_OR_INCOMPLETE", "cases": {}}
    if resolved_executable.parts[-4:-1] == ("Chromium.app", "Contents", "MacOS"):
        # Load native_bundle directly so CI need not install full app runtime deps (aiohttp/src).
        bundle_mod_path = Path(__file__).resolve().parents[2] / "chrome_engine/native_bundle.py"
        spec = importlib.util.spec_from_file_location(
            "chrome_engine_native_bundle", bundle_mod_path
        )
        assert spec and spec.loader
        bundle_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bundle_mod)
        report["bundle_sha256"] = bundle_mod.bundle_sha256(resolved_executable.parents[2])
    try:
        async with async_playwright() as pw:
            for os_name, seed, vendor, arch in [
                ("macos", 2, "apple", "metal-3"),
                ("macos", 3, "apple", "metal-3"),
                ("macos", 6, "apple", "metal-3"),
            ]:
                browser = await pw.chromium.launch(
                    executable_path=str(executable),
                    headless=True,
                    ignore_default_args=["--enable-automation"],
                    args=[
                        f"--fingerprint={seed}",
                        f"--fingerprint-platform={os_name}",
                        "--fingerprint-hardware-concurrency=20",
                        "--disable-background-networking",
                    ],
                )
                try:
                    if not browser.version.startswith("148."):
                        raise ValueError("Acceptance requires the pinned Chromium148 core")
                    context = await browser.new_context(color_scheme="null")
                    await context.route(
                        "**/*",
                        lambda route: (
                            route.continue_()
                            if route.request.url.startswith(
                                f"http://127.0.0.1:{server.server_port}/"
                            )
                            else route.abort()
                        ),
                    )
                    page = await context.new_page()
                    await page.goto(f"http://127.0.0.1:{server.server_port}/")
                    # Explicit diagnostics in disposable contexts, never an application bypass.
                    await page.evaluate("() => {" + SAMPLE + "globalThis.sample=sample;}")
                    await page.evaluate("source=>globalThis.WORKER_SOURCE=source", WORKER_SOURCE)
                    result = await page.evaluate(COLLECT)
                    report["cases"][f"{os_name}:{seed}"] = result
                    for name in [
                        "window",
                        "classic",
                        "module",
                        "shared",
                        "blob",
                        "iframeOwnedBlob",
                        "service",
                    ]:
                        check_sample(result[name], vendor, arch)
                    if result["borrowed"] != {"vendor": vendor, "architecture": arch}:
                        raise ValueError("Cross-realm native getter mismatch")
                    if result["solidChanged"] != 0:
                        raise ValueError("Solid canvas changed")
                    if (
                        len(
                            {tuple(result["htmlTextMetrics"])}
                            | {
                                tuple(result[n]["textMetrics"])
                                for n in [
                                    "window",
                                    "classic",
                                    "module",
                                    "shared",
                                    "blob",
                                    "iframeOwnedBlob",
                                    "service",
                                ]
                            }
                        )
                        != 1
                    ):
                        raise ValueError("Window/Worker measureText parity mismatch")
                    if result["subRectDiff"] != 0:
                        raise ValueError("Sub-rect getImageData deviated from full-canvas crop")
                    if result["blobMismatch"] != 0:
                        raise ValueError("Canvas toBlob/convertToBlob deviated from toDataURL")
                    if result["webglOob"] != 0:
                        raise ValueError("WebGL packed readPixels overflowed caller buffer")
                    if result.get("webglParityMismatch") != 0:
                        raise ValueError("WebGL readPixels pack/subrect/PBO parity mismatch")
                    digest = result.get("webglDigest")
                    if not isinstance(digest, str) or len(digest) != 64:
                        raise ValueError("WebGL parity digest missing")
                    if result.get("webglExtendedParityMismatch") != 0:
                        raise ValueError("WebGL 5551/FLOAT/HALF_FLOAT parity or HDR check failed")
                    for key in (
                        "webgl5551Digest",
                        "webglFloatDigest",
                        "webglHalfFloatDigest",
                        "audioDigest",
                    ):
                        val = result.get(key)
                        if not isinstance(val, str) or len(val) != 64:
                            raise ValueError(f"Missing digest {key}")
                    if result.get("audioSampleRate") != 44100:
                        raise ValueError(
                            f"Unexpected OfflineAudioContext.sampleRate: {result.get('audioSampleRate')}"
                        )
                    if result.get("audioSilentMax") != 0:
                        raise ValueError(
                            f"Silent WebAudio buffer perturbed: {result.get('audioSilentMax')}"
                        )
                    if result.get("clientRectMismatch") != 0:
                        raise ValueError(
                            "ClientRects 0x0, (0,0), or multiline union contract failed"
                        )
                    if (
                        result.get("webglRedBoxDigest")
                        != "bf9da7959d914298f9ce9e41a480fd66f76fac5c6f5e0a9b5a99b18cfc6fd997"
                    ):
                        raise ValueError("028 WebGL solid red box hash mismatch")
                    if result.get("flatMonochromeChanged") != 0:
                        raise ValueError("028 flat monochrome canvas perturbed")
                    await page.goto(f"http://127.0.0.1:{server.server_port}/strict")
                    check_sample(await page.evaluate(STRICT), vendor, arch)
                finally:
                    await browser.close()
            if (
                report["cases"]["macos:2"]["window"]["textMetrics"]
                == report["cases"]["macos:3"]["window"]["textMetrics"]
            ):
                raise ValueError("measureText metrics did not vary across seeds")
            if (
                report["cases"]["macos:2"]["webglDigest"]
                == report["cases"]["macos:3"]["webglDigest"]
            ):
                raise ValueError("WebGL noise digest did not vary across seeds")
            for key in ("webgl5551Digest", "webglFloatDigest", "webglHalfFloatDigest"):
                if report["cases"]["macos:2"][key] == report["cases"]["macos:3"][key]:
                    raise ValueError(f"{key} did not vary across seeds")
            audio_digests = {report["cases"][f"macos:{s}"]["audioDigest"] for s in (2, 3, 6)}
            if len(audio_digests) != 3:
                raise ValueError(
                    "Audio fingerprint digest did not vary cleanly across seeds 2, 3, 6"
                )

            async def _verify_apple_silicon_profile(
                seed: int,
                expected_renderer: str,
                expected_cpu: int,
                width: int,
                height: int,
                avail_top: int,
                avail_height: int,
                dpr: float,
                color_depth: int,
                gamut: str,
                internal: int,
                scheme: str,
            ) -> dict:
                browser = await pw.chromium.launch(
                    executable_path=str(executable),
                    headless=True,
                    ignore_default_args=["--enable-automation"],
                    args=[
                        f"--fingerprint={seed}",
                        "--fingerprint-platform=macos",
                        f"--fingerprint-screen-width={width}",
                        f"--fingerprint-screen-height={height}",
                        f"--fingerprint-device-scale-factor={dpr}",
                        f"--fingerprint-display-profile=avail_top={avail_top},"
                        f"avail_height={avail_height},color_depth={color_depth},"
                        f"gamut={gamut},internal={internal},scheme={scheme}",
                        f"--window-size={width - 4},{avail_height - 90}",
                        "--disable-background-networking",
                    ],
                )
                try:
                    context = await browser.new_context(color_scheme="null")
                    await context.route(
                        "**/*",
                        lambda route: (
                            route.continue_()
                            if route.request.url.startswith(
                                f"http://127.0.0.1:{server.server_port}/"
                            )
                            else route.abort()
                        ),
                    )
                    page = await context.new_page()
                    await page.goto(f"http://127.0.0.1:{server.server_port}/")
                    data = await page.evaluate(
                        """async () => {
                        const mk = (id) => {
                          const c = document.createElement('canvas');
                          c.width = 16; c.height = 16;
                          return c.getContext(id);
                        };
                        const gl1 = mk('webgl');
                        const gl2 = mk('webgl2');
                        const pre = {
                          gl1DrawBufVal: gl1.getParameter(0x8824),
                          gl1DrawBufErr: gl1.getError(),
                          gl1AnisoVal: gl1.getParameter(0x84FF),
                          gl1AnisoErr: gl1.getError(),
                          gl2AnisoVal: gl2.getParameter(0x84FF),
                          gl2AnisoErr: gl2.getError(),
                          gl2DrawBufVal: gl2.getParameter(0x8824),
                        };
                        const hasDraw = Boolean(gl1.getExtension('WEBGL_draw_buffers'));
                        const hasAniso1 = Boolean(gl1.getExtension('EXT_texture_filter_anisotropic'));
                        const hasAniso2 = Boolean(gl2.getExtension('EXT_texture_filter_anisotropic'));
                        const dbg = gl2.getExtension('WEBGL_debug_renderer_info');
                        const post = {
                          hasDraw, hasAniso1, hasAniso2,
                          gl1DrawBufVal: gl1.getParameter(0x8824),
                          gl1AnisoVal: gl1.getParameter(0x84FF),
                          gl2AnisoVal: gl2.getParameter(0x84FF),
                          renderer: dbg ? gl2.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
                          vendor: dbg ? gl2.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
                        };
                        const devs = await navigator.mediaDevices.enumerateDevices();
                        return {
                          cpu: navigator.hardwareConcurrency,
                          memory: navigator.deviceMemory,
                          dpr: window.devicePixelRatio,
                          outerHeight: window.outerHeight,
                          innerHeight: window.innerHeight,
                          screen: {
                            w: screen.width, h: screen.height,
                            availTop: screen.availTop, availHeight: screen.availHeight,
                            colorDepth: screen.colorDepth, isExtended: screen.isExtended,
                          },
                          media: {
                            p3: matchMedia('(color-gamut: p3)').matches,
                            srgb: matchMedia('(color-gamut: srgb)').matches,
                            color10: matchMedia('(color: 10)').matches,
                            dark: matchMedia('(prefers-color-scheme: dark)').matches,
                            light: matchMedia('(prefers-color-scheme: light)').matches,
                            devW: matchMedia(`(device-width: ${screen.width}px)`).matches,
                            devH: matchMedia(`(device-height: ${screen.height}px)`).matches,
                          },
                          deviceKinds: devs.map(d => d.kind),
                          pre, post,
                        };
                    }"""
                    )
                finally:
                    await browser.close()

                if data["cpu"] != expected_cpu or data["memory"] != 8:
                    raise ValueError(f"Apple Silicon seed {seed} cpu/memory mismatch: {data}")
                if data["post"]["renderer"] != expected_renderer:
                    raise ValueError(f"Apple Silicon seed {seed} GPU mismatch: {data['post']}")
                pre = data["pre"]
                if (
                    pre["gl1DrawBufVal"] is not None
                    or pre["gl1DrawBufErr"] != 1280
                    or pre["gl1AnisoVal"] is not None
                    or pre["gl1AnisoErr"] != 1280
                    or pre["gl2AnisoVal"] is not None
                    or pre["gl2AnisoErr"] != 1280
                    or pre["gl2DrawBufVal"] != 8
                ):
                    raise ValueError(f"WebGL extension-gating leak on seed {seed}: {pre}")
                post = data["post"]
                if post["hasDraw"] and post["gl1DrawBufVal"] != 8:
                    raise ValueError(f"Post-extension gl1 MAX_DRAW_BUFFERS mismatch: {post}")
                if post["hasAniso1"] and post["gl1AnisoVal"] != 16:
                    raise ValueError(f"Post-extension gl1 ANISOTROPY mismatch: {post}")
                if post["hasAniso2"] and post["gl2AnisoVal"] != 16:
                    raise ValueError(f"Post-extension gl2 ANISOTROPY mismatch: {post}")
                if data["screen"] != {
                    "w": width,
                    "h": height,
                    "availTop": avail_top,
                    "availHeight": avail_height,
                    "colorDepth": color_depth,
                    "isExtended": False,
                }:
                    raise ValueError(f"Screen override mismatch on seed {seed}: {data['screen']}")
                if data["dpr"] != dpr or data["outerHeight"] > avail_height:
                    raise ValueError(f"DPR/outerHeight mismatch on seed {seed}: {data}")
                if (
                    not data["media"]["devW"]
                    or not data["media"]["devH"]
                    or data["media"]["p3"] != (gamut == "p3")
                    or data["media"]["color10"] != (color_depth == 30)
                    or data["media"]["dark"] != (scheme == "dark")
                    or data["media"]["light"] != (scheme == "light")
                ):
                    raise ValueError(
                        f"CSS media coherence mismatch on seed {seed}: {data['media']}"
                    )
                if not internal and "videoinput" in data["deviceKinds"]:
                    raise ValueError(
                        f"MediaDevices desktop camera suppression failed on seed {seed}: "
                        f"{data['deviceKinds']}"
                    )
                return data

            report["cases"]["apple_silicon_m3_laptop"] = await _verify_apple_silicon_profile(
                seed=3,
                expected_renderer="ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)",
                expected_cpu=8,
                width=1512,
                height=982,
                avail_top=38,
                avail_height=874,
                dpr=2,
                color_depth=30,
                gamut="p3",
                internal=1,
                scheme="dark",
            )
            report["cases"]["apple_silicon_m2_max_desktop"] = await _verify_apple_silicon_profile(
                seed=9,
                expected_renderer=(
                    "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Max, Unspecified Version)"
                ),
                expected_cpu=12,
                width=2560,
                height=1440,
                avail_top=25,
                avail_height=1345,
                dpr=2,
                color_depth=30,
                gamut="p3",
                internal=0,
                scheme="light",
            )

            async def _verify_locale_speech_font_027(
                seed: int,
                candidate_families: list[str] | tuple[str, ...] = (),
                *,
                unfiltered: bool = False,
            ) -> dict:
                with tempfile.TemporaryDirectory(prefix="verify-027-") as tmp_profile_dir:
                    context = await pw.chromium.launch_persistent_context(
                        tmp_profile_dir,
                        executable_path=str(executable),
                        headless=True,
                        color_scheme="null",
                        no_viewport=True,
                        ignore_default_args=[
                            "--enable-automation",
                            "--disable-extensions",
                            "--disable-component-extensions-with-background-pages",
                        ],
                        args=[
                            f"--fingerprint={seed}",
                            "--fingerprint-platform=macos",
                            "--fingerprint-brand=Chrome",
                            "--lang=de-DE",
                            "--accept-lang=de-DE,de,en-US,en",
                            "--disable-background-networking",
                            *(["--disable-spoofing=font"] if unfiltered else []),
                        ],
                    )
                    try:
                        allowed_origins = (
                            f"http://127.0.0.1:{server.server_port}/",
                            f"http://localhost:{server.server_port}/",
                        )
                        await context.route(
                            "**/*",
                            lambda route: (
                                route.continue_()
                                if route.request.url.startswith(allowed_origins)
                                else route.abort()
                            ),
                        )
                        if hasattr(context, "grant_permissions"):
                            await context.grant_permissions(
                                ["local-fonts"], origin=f"http://127.0.0.1:{server.server_port}"
                            )
                        page = (
                            context.pages[0]
                            if getattr(context, "pages", None)
                            else await context.new_page()
                        )
                        await page.goto(f"http://127.0.0.1:{server.server_port}/")
                        if unfiltered:
                            families = await page.evaluate(
                                """async () => Array.from(new Set(
                                    (await window.queryLocalFonts()).map(f => f.family)))"""
                            )
                            return {"localFontFamilies": families}
                        main_payload = await page.evaluate(
                            """async (candidateFamilies) => {
                        const timeout = (p) => Promise.race([
                          p,
                          new Promise((_, rej) => setTimeout(() => rej(new Error('027 timeout')), 10000)),
                        ]);
                        const w = new Worker('/locale-worker.js');
                        const worker = await timeout(new Promise((res, rej) => {
                          w.onmessage = e => res(e.data);
                          w.onerror = e => rej(new Error(e.message));
                          w.postMessage('go');
                        }));
                        w.terminate();
                        const sh = new SharedWorker('/locale-shared.js');
                        const shared = await timeout(new Promise((res, rej) => {
                          sh.port.onmessage = e => res(e.data);
                          sh.port.onerror = e => rej(new Error(e.message));
                          sh.port.start();
                          sh.port.postMessage('go');
                        }));
                        sh.port.close();
                        const reg = await navigator.serviceWorker.register('/locale-sw.js');
                        let service;
                        try {
                          await timeout(navigator.serviceWorker.ready);
                          const ch = new MessageChannel();
                          service = await timeout(new Promise((res) => {
                            ch.port1.onmessage = e => res(e.data);
                            reg.active.postMessage('go', [ch.port2]);
                          }));
                          ch.port1.close();
                        } finally {
                          await reg.unregister();
                        }
                        const voices = await new Promise((res) => {
                          const init = speechSynthesis.getVoices();
                          if (init.length > 0) return res(init);
                          const done = () => res(speechSynthesis.getVoices());
                          speechSynthesis.onvoiceschanged = done;
                          setTimeout(done, 500);
                        });
                        const fonts = await window.queryLocalFonts();
                        const families = Array.from(new Set(fonts.map(f => f.family))).sort();
                        const visibleLower = new Set(families.map(f => f.toLowerCase()));
                        const c = new OffscreenCanvas(400, 60);
                        const g = c.getContext('2d');
                        const sampleText = 'mmmmmmmmmmlliWW0123';
                        g.font = '32px monospace';
                        const wMono = g.measureText(sampleText).width;
                        g.font = '32px serif';
                        const wSerif = g.measureText(sampleText).width;
                        const coreQueryProbes = [
                          'Al Bayan', 'Avenir Next', 'Euphemia UCAS',
                          'Kohinoor Gujarati', 'Menlo', 'Monaco', 'Mukta Mahee',
                          'Noto Sans Kannada', 'Noto Sans Myanmar', 'Noto Sans Oriya',
                          'Noto Serif Myanmar', 'Optima', 'PingFang HK', 'PingFang MO',
                          'PingFang SC', 'Zapfino'
                        ];
                        const coreCanvasProbes = [
                          'Avenir Next', 'Euphemia UCAS', 'Menlo',
                          'Monaco', 'Optima', 'PingFang HK', 'PingFang SC', 'Zapfino'
                        ];
                        let coreFontMissingCount = 0;
                        for (const fam of coreQueryProbes) {
                          if (!visibleLower.has(fam.toLowerCase())) coreFontMissingCount++;
                        }
                        for (const fam of coreCanvasProbes) {
                          g.font = '32px "' + fam + '", monospace';
                          const mw = g.measureText(sampleText).width;
                          g.font = '32px "' + fam + '", serif';
                          const sw = g.measureText(sampleText).width;
                          if (Math.abs(mw - wMono) <= 0.01 && Math.abs(sw - wSerif) <= 0.01) {
                            coreFontMissingCount++;
                          }
                        }
                        const hiddenSample = (candidateFamilies || [])
                          .filter(f => !visibleLower.has(f.toLowerCase()))
                          .slice(0, 5);
                        let caseRenderLeaks = 0;
                        if (hiddenSample.length > 0) {
                          for (const fam of hiddenSample) {
                            for (const variant of [fam, fam.toLowerCase(), fam.toUpperCase()]) {
                              g.font = '32px "' + variant + '", monospace';
                              const mw = g.measureText(sampleText).width;
                              g.font = '32px "' + variant + '", serif';
                              const sw = g.measureText(sampleText).width;
                              if (Math.abs(mw - wMono) > 0.01 || Math.abs(sw - wSerif) > 0.01) {
                                caseRenderLeaks++;
                              }
                            }
                          }
                        }
                        return {
                          window: {
                            dtf: Intl.DateTimeFormat().resolvedOptions().locale,
                            nf: Intl.NumberFormat().resolvedOptions().locale,
                            num: (123456.789).toLocaleString(),
                            langs: Array.from(navigator.languages || []),
                          },
                          worker,
                          shared,
                          service,
                          googleVoices: voices.filter(v => v.name.startsWith('Google')).map(v => v.name),
                          localVoiceCount: voices.filter(v => !v.name.startsWith('Google') && v.localService).length,
                          localFontFamilies: families,
                          coreFontMissingCount,
                          hiddenSampleCount: hiddenSample.length,
                          caseRenderLeaks,
                        };
                    }""",
                            list(candidate_families),
                        )
                        async with context.expect_page() as noopener_info:
                            await page.evaluate(
                                f"window.open('http://localhost:{server.server_port}/locale-popup?noopener=1', '_blank', 'noopener')"
                            )
                        noopener_page = await noopener_info.value
                        await noopener_page.wait_for_load_state()
                        noopener_popup = await noopener_page.evaluate(
                            "() => ({first: window.__firstInline, seen: window.__seenLocales.slice()})"
                        )
                        await noopener_page.close()

                        nav_opener = await context.new_page()
                        await nav_opener.goto(f"http://127.0.0.1:{server.server_port}/")
                        async with context.expect_page() as nav_pop_info:
                            await nav_opener.evaluate("window.open('/locale-popup?same=crossnav')")
                        nav_pop = await nav_pop_info.value
                        await nav_pop.wait_for_load_state()
                        nav_had_opener = await nav_pop.evaluate(
                            "() => Boolean(window.opener && window.opener.location.origin === location.origin)"
                        )
                        await nav_opener.goto(f"http://localhost:{server.server_port}/")
                        await nav_pop.evaluate("() => new Promise(r => setTimeout(r, 50))")
                        same_origin_cross_nav_popup = await nav_pop.evaluate(
                            "(hadOpener) => ({hadSameOriginOpener: hadOpener, first: window.__firstInline, seen: window.__seenLocales.slice()})",
                            nav_had_opener,
                        )
                        await nav_pop.close()
                        await nav_opener.close()

                        close_opener = await context.new_page()
                        await close_opener.goto(f"http://127.0.0.1:{server.server_port}/")
                        async with context.expect_page() as close_pop_info:
                            await close_opener.evaluate("window.open('/locale-popup?same=close')")
                        close_pop = await close_pop_info.value
                        await close_pop.wait_for_load_state()
                        close_had_opener = await close_pop.evaluate(
                            "() => Boolean(window.opener && window.opener.location.origin === location.origin)"
                        )
                        await close_opener.close()
                        await close_pop.evaluate("() => new Promise(r => setTimeout(r, 50))")
                        same_origin_close_popup = await close_pop.evaluate(
                            "(hadOpener) => ({hadSameOriginOpener: hadOpener, openerClosed: window.opener === null || window.opener.closed, first: window.__firstInline, seen: window.__seenLocales.slice()})",
                            close_had_opener,
                        )
                        await close_pop.close()

                        main_payload["noopenerPopup"] = noopener_popup
                        main_payload["sameOriginCrossNavPopup"] = same_origin_cross_nav_popup
                        main_payload["sameOriginClosePopup"] = same_origin_close_popup
                        return main_payload
                    finally:
                        await context.close()

            protected_fonts = _protected_macos_fonts()
            unfiltered_fonts = await _verify_locale_speech_font_027(3006, unfiltered=True)
            installed_core_fonts = {
                f.lower() for f in unfiltered_fonts["localFontFamilies"]
            } & protected_fonts
            if not installed_core_fonts:
                raise ValueError("028 unfiltered protected macOS core font inventory empty")
            r3006 = await _verify_locale_speech_font_027(3006)
            supplemental_candidates = sorted(
                f for f in r3006["localFontFamilies"] if f.lower() not in protected_fonts
            )
            r3003 = await _verify_locale_speech_font_027(
                3003, candidate_families=supplemental_candidates
            )
            expected_popup_first = {
                "lang": "de-DE",
                "langs": ["de-DE", "de", "en-US", "en"],
                "dtf": "de-DE",
                "nf": "de-DE",
                "num": "123.456,789",
            }
            for label, payload in (("3003", r3003), ("3006", r3006)):
                for realm in ("window", "worker", "shared", "service"):
                    row = payload[realm]
                    if (
                        row.get("dtf") != "de-DE"
                        or row.get("nf") != "de-DE"
                        or row.get("num") != "123.456,789"
                        or row.get("langs") != ["de-DE", "de", "en-US", "en"]
                    ):
                        raise ValueError(f"027 locale mismatch ({label}/{realm}): {row}")
                for pkey in ("noopenerPopup", "sameOriginCrossNavPopup", "sameOriginClosePopup"):
                    popup = payload.get(pkey) or {}
                    require_same_opener = pkey != "noopenerPopup"
                    require_closed = pkey == "sameOriginClosePopup"
                    if (
                        popup.get("first") != expected_popup_first
                        or popup.get("seen") != ["de-DE"]
                        or (require_same_opener and popup.get("hadSameOriginOpener") is not True)
                        or (require_closed and popup.get("openerClosed") is not True)
                    ):
                        raise ValueError(
                            f"027 popup locale continuity mismatch ({label}/{pkey}): {popup}"
                        )
                gvoices = payload.get("googleVoices", [])
                if gvoices:
                    raise ValueError(
                        f"027 unexpected broken Google Network Speech voices ({label}): {gvoices}"
                    )
                if payload.get("localVoiceCount", 0) <= 0:
                    raise ValueError(f"027 missing local system speech voices ({label})")
                fams = payload.get("localFontFamilies", [])
                if len(fams) <= 50 or "Arial" not in fams or "Helvetica" not in fams:
                    raise ValueError(
                        f"027 queryLocalFonts unexpected family set ({label}): {len(fams)}"
                    )
                if payload.get("coreFontMissingCount") != 0:
                    raise ValueError(f"028 protected macOS core font hidden ({label}): {payload}")
            core_3003 = {f.lower() for f in r3003["localFontFamilies"]} & protected_fonts
            core_3006 = {f.lower() for f in r3006["localFontFamilies"]} & protected_fonts
            for seed, visible in ((3003, core_3003), (3006, core_3006)):
                missing = installed_core_fonts - visible
                if missing:
                    raise ValueError(
                        f"028 installed protected macOS core font hidden (seed {seed}): "
                        f"{sorted(missing)}"
                    )
            if core_3003 != core_3006:
                raise ValueError(
                    "028 protected macOS system font varied across seeds: "
                    f"{sorted(core_3003 ^ core_3006)}"
                )
            non_core_3003 = {f.lower() for f in r3003["localFontFamilies"]} - protected_fonts
            non_core_3006 = {f.lower() for f in r3006["localFontFamilies"]} - protected_fonts
            if non_core_3003 == non_core_3006:
                raise ValueError(
                    "027 queryLocalFonts did not reflect per-seed supplemental/user FontCache filtering"
                )
            if r3003.get("hiddenSampleCount", 0) <= 0 or r3003.get("caseRenderLeaks", 0) != 0:
                raise ValueError(f"027 hidden font case-insensitivity check failed: {r3003}")
            report["cases"]["locale_speech_font_de"] = {
                "seed_3003_font_count": len(r3003["localFontFamilies"]),
                "seed_3006_font_count": len(r3006["localFontFamilies"]),
                "local_voice_count": r3003["localVoiceCount"],
                "google_voice_count": 0,
                "core_font_missing_count": r3003["coreFontMissingCount"],
                "installed_protected_font_count": len(installed_core_fonts),
                "hidden_case_checked": r3003["hiddenSampleCount"],
                "window": r3003["window"],
                "worker": r3003["worker"],
                "shared": r3003["shared"],
                "service": r3003["service"],
                "noopener_popup": r3003["noopenerPopup"],
                "same_origin_cross_nav_popup": r3003["sameOriginCrossNavPopup"],
                "same_origin_close_popup": r3003["sameOriginClosePopup"],
            }
        report["acceptance"] = "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(verify(args.executable, args.output), timeout=180))
