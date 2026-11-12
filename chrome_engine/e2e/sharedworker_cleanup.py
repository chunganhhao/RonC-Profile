"""Local DOM E2E through ChromeEngine; temporary profile, no accounts/network services.

Run: .venv/bin/python -m chrome_engine.e2e.sharedworker_cleanup --out artifacts/worker-cleanup
Add --hold for a bounded CDP/REA observation window (touch OUT/stop to finish).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import expect

from chrome_engine import engine as engine_mod
from chrome_engine.engine import ChromeEngine
from chrome_engine.persona import Persona
from chrome_engine.stealth import build_anti_cdp_init_script

HTML = """<!doctype html><title>SharedWorker cleanup E2E</title>
<h1>FoxProfile SharedWorker cleanup</h1>
<button id="run">Run cleanup checks</button><p id="status">Ready</p>
<pre id="result"></pre><div id="frames"></div>
<script>
const check=(ok,msg)=>{if(!ok)throw Error(msg)};
async function echo(port){return await new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(Error('Worker echo timeout')),3000);
  port.onmessage=e=>{clearTimeout(timer);resolve(e.data)};
  port.start();port.postMessage('echo');
})}
document.querySelector('#run').onclick=async()=>{
  const status=document.querySelector('#status');status.textContent='Running';
  try{
    document.querySelector('#frames').replaceChildren();
    const frames=[];
    for(let i=0;i<8;i++){
      const f=document.createElement('iframe');const loaded=new Promise(r=>f.onload=r);
      f.src='/frame';document.querySelector('#frames').append(f);await loaded;frames.push(f.contentWindow);
    }
    const child=frames[0];let unrelatedHooks=0;
    const savedClose=frames[7].MessagePort.prototype.close;
    frames[7].MessagePort.prototype.close=function(){unrelatedHooks++;return savedClose.apply(this,arguments)};
    window.__closeCalls=0;const c=new MessageChannel();const started=performance.now();c.port1.close();
    const plainCloseMs=performance.now()-started,plainCloseCalls=window.__closeCalls;
    check(unrelatedHooks===0,'unrelated iframe hook called');check(plainCloseCalls<=4,'close replay');
    frames[7].MessagePort.prototype.close=savedClose;
    for(let i=0;i<30;i++){
      const w=new SharedWorker('/shared.js',{name:'closed-'+i});await echo(w.port);
      child.MessagePort.prototype.close.call(w.port);
    }
    check(window.__workerRefs.length===0,'parent references leaked');
    const a=new SharedWorker('/shared.js',{name:'siblings'}),b=new SharedWorker('/shared.js',{name:'siblings'});
    await echo(a.port);await echo(b.port);child.MessagePort.prototype.close.call(a.port);
    check(await echo(b.port)==='echo','closing a broke sibling b');b.port.close();
    const reverse=new child.SharedWorker('/shared.js',{name:'child-owned'});await echo(reverse.port);
    MessagePort.prototype.close.call(reverse.port);
    check(child.__workerRefs.length===0,'child references leaked');
    // Changing the public method after setup must not affect a borrowed parent close.
    const reverse2=new child.SharedWorker('/shared.js',{name:'child-custom-close'});await echo(reverse2.port);
    const childClose=child.MessagePort.prototype.close;
    child.MessagePort.prototype.close=()=>{throw Error('unrelated public close invoked')};
    MessagePort.prototype.close.call(reverse2.port);child.MessagePort.prototype.close=childClose;
    check(child.__workerRefs.length===0,'custom child close leaked');
    let brand;try{child.MessagePort.prototype.close.call({})}catch(e){brand=e.name}
    check(brand==='TypeError','invalid receiver accepted');
    check(window.__workerRefs.length===0,'final parent references leaked');
    const result={connectionsClosed:30,parentRefs:window.__workerRefs.length,childRefs:child.__workerRefs.length,
      siblingAlive:true,unrelatedHooks,plainCloseCalls,plainCloseMs,brand};
    document.querySelector('#result').textContent=JSON.stringify(result,null,2);
    status.textContent='PASS';console.log('SharedWorker cleanup PASS',JSON.stringify(result));
  }catch(e){status.textContent='FAIL';document.querySelector('#result').textContent=e.stack;console.error(e)}
};
</script>"""


async def run(out: Path, hold: bool = False) -> dict:
    executable = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not executable.is_file():
        raise RuntimeError(
            "Local Google Chrome is required; no browser is downloaded automatically"
        )
    out.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            worker = self.path == "/shared.js"
            body = (
                "onconnect=e=>{const p=e.ports[0];p.onmessage=e=>p.postMessage(e.data);p.start()}"
                if worker
                else ("<title>Fixture frame</title>" if self.path == "/frame" else HTML)
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript" if worker else "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def observed_script(persona):
        # Observation only: expose existing retention array and count proxy entries.
        return (
            build_anti_cdp_init_script(persona)
            .replace(
                "const activeSharedRefs = [];",
                "const activeSharedRefs = []; globalThis.__workerRefs = activeSharedRefs;",
            )
            .replace(
                "const res = Reflect.apply(target, thisArg, args);",
                "globalThis.__closeCalls=(globalThis.__closeCalls||0)+1; const res = Reflect.apply(target, thisArg, args);",
            )
        )

    try:
        with (
            tempfile.TemporaryDirectory(prefix="foxprofile-worker-e2e-") as profile,
            patch.object(engine_mod, "build_anti_cdp_init_script", observed_script),
        ):
            engine = ChromeEngine(
                profile,
                executable=executable,
                headless=True,
                geoip=False,
                persona=Persona(seed=300, platform="macos", platform_version="15.6.1"),
                extra_args=(
                    "--remote-debugging-port=0",
                    "--remote-debugging-address=127.0.0.1",
                ),
            )
            async with engine as context:
                page = context.pages[0]
                await page.goto(origin)
                await page.get_by_role("button", name="Run cleanup checks").click()
                await expect(page.locator("#status")).to_have_text("PASS", timeout=15000)
                result = json.loads(await page.locator("#result").inner_text())
                result["browser"] = (
                    context.browser.version if context.browser else "persistent Chromium"
                )
                await page.screenshot(path=str(out / "e2e-pass.png"), full_page=True)
                port = (Path(profile) / "DevToolsActivePort").read_text().splitlines()[0]
                result["origin"] = origin
                result["cdp_endpoint"] = f"http://127.0.0.1:{port}"
                (out / "result.json").write_text(json.dumps(result, indent=2))
                print(json.dumps(result), flush=True)
                if hold:
                    for _ in range(600):
                        if (out / "stop").exists():
                            break
                        await asyncio.sleep(1)
                return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.out, args.hold))
