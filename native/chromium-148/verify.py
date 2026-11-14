"""Fail-closed native-core acceptance in disposable contexts on loopback only.

No app init script is installed: worker protection must come from the built core.
Passing this matrix is not permission to replace a production core/profile.
"""

import argparse
import asyncio
import hashlib
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
  ctx.font='16px sans-serif';
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
  const mctx = mc.getContext('2d'); mctx.font = '16px sans-serif';
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
    with executable.open("rb") as binary:
        binary_sha256 = hashlib.file_digest(binary, "sha256").hexdigest()
    report = {"binary_sha256": binary_sha256, "acceptance": "FAILED_OR_INCOMPLETE", "cases": {}}
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
                    await page.goto(f"http://127.0.0.1:{server.server_port}/strict")
                    check_sample(await page.evaluate(STRICT), vendor, arch)
                finally:
                    await browser.close()
        if (
            report["cases"]["macos:2"]["window"]["textMetrics"]
            == report["cases"]["macos:3"]["window"]["textMetrics"]
        ):
            raise ValueError("measureText metrics did not vary across seeds")
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
