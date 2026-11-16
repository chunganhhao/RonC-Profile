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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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
        }
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
                    context = await browser.new_context()
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
                    await page.goto(f"http://127.0.0.1:{server.server_port}/strict")
                    check_sample(await page.evaluate(STRICT), vendor, arch)
                finally:
                    await browser.close()
        if (
            report["cases"]["macos:2"]["window"]["textMetrics"]
            == report["cases"]["macos:3"]["window"]["textMetrics"]
        ):
            raise ValueError("measureText metrics did not vary across seeds")
        if report["cases"]["macos:2"]["webglDigest"] == report["cases"]["macos:3"]["webglDigest"]:
            raise ValueError("WebGL noise digest did not vary across seeds")
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
