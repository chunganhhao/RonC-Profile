"""Anti-CDP leak shield, WebGPU/Client-Hints coherence & humanized input stealth.

Provides two layers of protection for Chromium (`fingerprint-chromium`) and page
control actions:
1. `build_anti_cdp_init_script(persona)`:
   - Removes residual CDP / WebDriver / Playwright automation globals (`cdc_*`,
     `$cdc_*`, `__playwright*`, `__pw*`, `domAutomation*`) from `globalThis` and `document`.
   - Neutralizes the `Runtime.enable` / `Console.enable` `Error.stack` getter
     side-channel across `console.debug`, `console.log`, `console.info`,
     `console.warn`, `console.error`, `console.table`, and `console.dir`.
   - Clamps `navigator.deviceMemory` on `Navigator.prototype` and
     `WorkerNavigator.prototype` to Chromium's `ApproximatedDeviceMemory` cap (`<= 8`)
     even if an explicit unaligned seed (`seed % 3 != 0`) was supplied.
   - Ensures `window.chrome` (`app`, `runtime`) is populated in headless mode.
   - Patches `GPUAdapterInfo.prototype` getters (`vendor`, `architecture`, `device`,
     `description`) while keeping the native `GPUAdapterInfo` receiver intact (so
     `subgroupMinSize`, `subgroupMaxSize`, `isFallbackAdapter`, etc. work natively
     without `TypeError: Illegal invocation`), and propagates the bootstrap into
     `Worker` / `SharedWorker` realms so Window and Worker WebGPU identities agree.
2. Behavioral input humanizer (`bezier_curve`, `keystroke_delays_ms`,
   `human_click_at`, `human_type`, `human_scroll`):
   - Replaces 0-ms CDP cursor teleports and flat keystroke bursts with smooth
     cubic Bezier trajectories and proportionally-scaled natural typing cadence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import secrets
from random import Random
from typing import Any

from .persona import Persona

# Expected WebGPU adapter metadata per persona platform so cross-OS personas
# never expose a contradicting host GPU adapter (e.g. Apple Metal on Windows).
_WEBGPU_PROFILES: dict[str, dict[str, str]] = {
    "windows": {
        "vendor": "intel",
        "architecture": "gen-12lp",
        "device": "uhd-graphics",
        "description": "Intel(R) UHD Graphics",
    },
    "macos": {
        "vendor": "apple",
        "architecture": "metal-3",
        "device": "apple-m2",
        "description": "Apple M2",
    },
    "linux": {
        "vendor": "intel",
        "architecture": "gen-12lp",
        "device": "mesa-intel",
        "description": "Mesa Intel(R) UHD Graphics",
    },
}


def webgpu_profile_for(platform: str, seed: int = 3) -> dict[str, str]:
    """Deterministic, platform-coherent WebGPU adapter info for `platform`."""
    rng = Random(f"webgpu:{platform}:{seed}")
    if platform == "macos":
        chip = rng.choice(("Apple M1", "Apple M2", "Apple M3", "Apple M4"))
        return {
            "vendor": "apple",
            "architecture": "metal-3",
            "device": chip.lower().replace(" ", "-"),
            "description": chip,
        }
    if platform == "windows":
        choice = rng.choice(
            (
                ("intel", "gen-12lp", "uhd-graphics-770", "Intel(R) Iris(R) Xe Graphics"),
                ("nvidia", "ampere", "rtx-3060", "NVIDIA GeForce RTX 3060"),
                ("amd", "rdna-2", "rx-6600", "AMD Radeon RX 6600"),
            )
        )
        return {
            "vendor": choice[0],
            "architecture": choice[1],
            "device": choice[2],
            "description": choice[3],
        }
    return dict(_WEBGPU_PROFILES["linux"])


def build_anti_cdp_init_script(persona: Persona) -> str:
    """Return a self-contained JS init script that hardens CDP, deviceMemory, and WebGPU across realms."""
    gpu_info = webgpu_profile_for(persona.platform, persona.seed)
    cfg_json = json.dumps(
        {
            "platform": persona.platform,
            "seed": persona.seed,
            "gpu": gpu_info,
            "portCloseKey": secrets.token_hex(32),
        }
    )
    return f"""(() => {{
  function installRealmStealth(CFG) {{
    const RE_LEAK = /^(\\$?cdc_|__playwright|__pw|__webdriver|__selenium|__fxdriver|domAutomation)/i;

    const purgeLeaks = (target) => {{
      if (!target) return;
      try {{
        for (const key of Object.getOwnPropertyNames(target)) {{
          if (RE_LEAK.test(key)) {{
            try {{ delete target[key]; }} catch (_) {{}}
          }}
        }}
      }} catch (_) {{}}
    }};
    purgeLeaks(globalThis);
    if (typeof document !== 'undefined') purgeLeaks(document);

    // Neutralize CDP Runtime.enable / Console.enable Error.stack getter probe
    if (typeof console !== 'undefined') {{
      const sanitizeArg = (arg) => {{
        if (arg instanceof Error || (arg && typeof arg === 'object')) {{
          try {{
            const own = Object.getOwnPropertyDescriptor(arg, 'stack');
            if (own && typeof own.get === 'function') {{
              return arg.message ? String(arg.message) : '[Error]';
            }}
          }} catch (_) {{}}
        }}
        return arg;
      }};
      for (const method of ['debug', 'log', 'info', 'warn', 'error', 'table', 'dir']) {{
        const orig = console[method];
        if (typeof orig === 'function') {{
          const wrapped = new Proxy(orig, {{
            apply(target, thisArg, args) {{
              return Reflect.apply(target, thisArg, Array.from(args, sanitizeArg));
            }}
          }});
          try {{
            Object.defineProperty(console, method, {{
              value: wrapped,
              writable: true,
              configurable: true,
            }});
          }} catch (_) {{}}
        }}
      }}
    }}

    // Clamp navigator.deviceMemory to Chromium's ApproximatedDeviceMemory cap (8 GB)
    // ponytail: temporary bypass pending upstream https://github.com/adryfish/fingerprint-chromium/issues/100
    const clampDeviceMemory = (proto) => {{
      if (!proto) return;
      try {{
        const desc = Object.getOwnPropertyDescriptor(proto, 'deviceMemory');
        if (desc && typeof desc.get === 'function') {{
          const origGet = desc.get;
          Object.defineProperty(proto, 'deviceMemory', {{
            get: new Proxy(origGet, {{
              apply(target, thisArg, args) {{
                const val = Reflect.apply(target, thisArg, args);
                return (typeof val === 'number' && val > 8) ? 8 : val;
              }}
            }}),
            enumerable: desc.enumerable,
            configurable: desc.configurable,
          }});
        }}
      }} catch (_) {{}}
    }};
    if (typeof Navigator !== 'undefined') clampDeviceMemory(Navigator.prototype);
    if (typeof WorkerNavigator !== 'undefined') clampDeviceMemory(WorkerNavigator.prototype);

    // Ensure window.chrome desktop objects exist in headless mode
    if (typeof window !== 'undefined') {{
      try {{
        if (!window.chrome) {{
          Object.defineProperty(window, 'chrome', {{
            value: {{}},
            writable: true,
            enumerable: true,
            configurable: true,
          }});
        }}
        if (window.chrome && !window.chrome.runtime) {{
          window.chrome.runtime = {{}};
        }}
        if (window.chrome && !window.chrome.app) {{
          window.chrome.app = {{
            isInstalled: false,
            InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
            RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
          }};
        }}
      }} catch (_) {{}}
    }}

    // Receiver-preserving WebGPU GPUAdapterInfo.prototype patch (B3-F1)
    const patchedProtos = typeof WeakSet === 'function' ? new WeakSet() : null;
    const patchGpuAdapterInfoProto = (proto) => {{
      if (!proto || (patchedProtos && patchedProtos.has(proto))) return;
      try {{
        const vDesc = Object.getOwnPropertyDescriptor(proto, 'vendor');
        const aDesc = Object.getOwnPropertyDescriptor(proto, 'architecture');
        if (!vDesc || typeof vDesc.get !== 'function') return;
        const origVendorGet = vDesc.get;
        const origArchGet = aDesc && typeof aDesc.get === 'function' ? aDesc.get : null;
        const shouldRewrite = (receiver) => {{
          const rawV = String(Reflect.apply(origVendorGet, receiver, []) || '').toLowerCase();
          const rawA = origArchGet ? String(Reflect.apply(origArchGet, receiver, []) || '').toLowerCase() : '';
          return (CFG.platform === 'macos' && rawV !== 'apple' && !rawA.includes('metal')) ||
                 (CFG.platform !== 'macos' && (rawV === 'apple' || rawA.includes('metal') || !rawV));
        }};
        for (const key of ['vendor', 'architecture', 'device', 'description']) {{
          const desc = Object.getOwnPropertyDescriptor(proto, key);
          if (!desc || typeof desc.get !== 'function') continue;
          const origGet = desc.get;
          Object.defineProperty(proto, key, {{
            get: new Proxy(origGet, {{
              apply(target, thisArg, args) {{
                const nativeVal = Reflect.apply(target, thisArg, args);
                if (shouldRewrite(thisArg)) {{
                  return CFG.gpu[key] || '';
                }}
                return nativeVal;
              }}
            }}),
            enumerable: desc.enumerable,
            configurable: desc.configurable,
          }});
        }}
        if (patchedProtos) patchedProtos.add(proto);
      }} catch (_) {{}}
    }};

    if (typeof GPUAdapterInfo !== 'undefined' && GPUAdapterInfo.prototype) {{
      patchGpuAdapterInfoProto(GPUAdapterInfo.prototype);
    }}
    if (typeof navigator !== 'undefined' && navigator.gpu && typeof navigator.gpu.requestAdapter === 'function') {{
      try {{
        const origRequestAdapter = navigator.gpu.requestAdapter;
        navigator.gpu.requestAdapter = new Proxy(origRequestAdapter, {{
          async apply(target, thisArg, args) {{
            const adapter = await Reflect.apply(target, thisArg, args);
            if (adapter && adapter.info) {{
              patchGpuAdapterInfoProto(Object.getPrototypeOf(adapter.info));
            }}
            return adapter;
          }}
        }});
      }} catch (_) {{}}
    }}

    // Preserve original worker base URL semantics when booted from a Blob wrapper (R2-B3-F2a)
    if (CFG.workerBaseUrl && !CFG.workerBaseUrl.startsWith('blob:') && !CFG.workerBaseUrl.startsWith('data:')) {{
      const resolveRelUrl = (input) => {{
        if (typeof input === 'string' && !input.startsWith('data:') && !input.startsWith('blob:')) {{
          try {{ return new URL(input, CFG.workerBaseUrl).href; }} catch (_) {{}}
        }}
        return input;
      }};
      if (typeof importScripts === 'function') {{
        const origImportScripts = importScripts;
        const wrappedImportScripts = new Proxy(origImportScripts, {{
          apply(target, thisArg, args) {{
            return Reflect.apply(target, thisArg, Array.from(args, (u) => resolveRelUrl(String(u))));
          }}
        }});
        try {{
          if (typeof WorkerGlobalScope !== 'undefined' && WorkerGlobalScope.prototype) {{
            Object.defineProperty(WorkerGlobalScope.prototype, 'importScripts', {{
              value: wrappedImportScripts,
              writable: true,
              enumerable: true,
              configurable: true,
            }});
          }}
          globalThis.importScripts = wrappedImportScripts;
        }} catch (_) {{}}
      }}
      if (typeof WorkerLocation !== 'undefined' && WorkerLocation.prototype) {{
        try {{
          const parsedLoc = new URL(CFG.workerBaseUrl);
          for (const key of ['href', 'origin', 'protocol', 'host', 'hostname', 'port', 'pathname', 'search', 'hash']) {{
            const desc = Object.getOwnPropertyDescriptor(WorkerLocation.prototype, key);
            if (desc && typeof desc.get === 'function') {{
              const origGet = desc.get;
              Object.defineProperty(WorkerLocation.prototype, key, {{
                get: new Proxy(origGet, {{
                  apply(target, thisArg, args) {{
                    Reflect.apply(target, thisArg, args);
                    return String(parsedLoc[key]);
                  }}
                }}),
                enumerable: desc.enumerable,
                configurable: desc.configurable,
              }});
            }}
          }}
          if (typeof WorkerLocation.prototype.toString === 'function') {{
            const origToStr = WorkerLocation.prototype.toString;
            Object.defineProperty(WorkerLocation.prototype, 'toString', {{
              value: new Proxy(origToStr, {{
                apply(target, thisArg, args) {{
                  Reflect.apply(target, thisArg, args);
                  return parsedLoc.href;
                }}
              }}),
              writable: true,
              enumerable: true,
              configurable: true,
            }});
          }}
        }} catch (_) {{}}
      }}
      if (typeof fetch === 'function') {{
        const origFetch = fetch;
        globalThis.fetch = new Proxy(origFetch, {{
          apply(target, thisArg, args) {{
            const nextArgs = args.slice();
            if (nextArgs.length > 0) nextArgs[0] = resolveRelUrl(nextArgs[0]);
            return Reflect.apply(target, thisArg, nextArgs);
          }}
        }});
      }}
      if (typeof Request === 'function') {{
        const OrigRequest = Request;
        const WrappedRequest = new Proxy(OrigRequest, {{
          construct(target, args, newTarget) {{
            const nextArgs = args.slice();
            if (nextArgs.length > 0 && typeof nextArgs[0] !== 'object') {{
              const u = `${{nextArgs[0]}}`;
              nextArgs[0] = resolveRelUrl(u);
            }}
            const actualNewTarget = (newTarget === OrigRequest) ? WrappedRequest : newTarget;
            return Reflect.construct(target, nextArgs, actualNewTarget);
          }}
        }});
        if (OrigRequest.prototype) {{
          Object.defineProperty(OrigRequest.prototype, 'constructor', {{
            value: WrappedRequest,
            writable: true,
            enumerable: false,
            configurable: true,
          }});
        }}
        Object.defineProperty(globalThis, 'Request', {{
          value: WrappedRequest,
          writable: true,
          enumerable: false,
          configurable: true,
        }});
      }}
      if (typeof EventSource === 'function') {{
        const OrigEventSource = EventSource;
        const WrappedEventSource = new Proxy(OrigEventSource, {{
          construct(target, args, newTarget) {{
            const nextArgs = args.slice();
            if (nextArgs.length > 0) {{
              const u = `${{nextArgs[0]}}`;
              nextArgs[0] = resolveRelUrl(u);
            }}
            const actualNewTarget = (newTarget === OrigEventSource) ? WrappedEventSource : newTarget;
            return Reflect.construct(target, nextArgs, actualNewTarget);
          }}
        }});
        if (OrigEventSource.prototype) {{
          Object.defineProperty(OrigEventSource.prototype, 'constructor', {{
            value: WrappedEventSource,
            writable: true,
            enumerable: false,
            configurable: true,
          }});
        }}
        Object.defineProperty(globalThis, 'EventSource', {{
          value: WrappedEventSource,
          writable: true,
          enumerable: false,
          configurable: true,
        }});
      }}
      if (typeof XMLHttpRequest === 'function' && XMLHttpRequest.prototype && typeof XMLHttpRequest.prototype.open === 'function') {{
        const origOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = new Proxy(origOpen, {{
          apply(target, thisArg, args) {{
            const nextArgs = args.slice();
            if (nextArgs.length > 1) nextArgs[1] = resolveRelUrl(nextArgs[1]);
            return Reflect.apply(target, thisArg, nextArgs);
          }}
        }});
      }}
    }}

    // Propagate realm stealth bootstrap into DedicatedWorker and SharedWorker realms (B3-F2, R2-B3-F2a/b/c, R3-B3-F2c)
    try {{
      const cleanCfg = Object.assign({{}}, CFG);
      delete cleanCfg.workerBaseUrl;
      const bootstrapCode = "(" + installRealmStealth.toString() + ")(" + JSON.stringify(cleanCfg) + ");\\n";
      const blobMap = new Map();
      const sharedUrlPromiseMap = new Map();
      const localSwReg = new Map();
      const pendingSwWaiters = new Map();
      const activeSharedRefs = [];
      const docId = Math.random().toString(36).slice(2) + Date.now().toString(36);
      const OrigBroadcastChannel = (typeof BroadcastChannel === 'function') ? BroadcastChannel : null;
      const origBcPostMessage = (OrigBroadcastChannel && OrigBroadcastChannel.prototype && typeof OrigBroadcastChannel.prototype.postMessage === 'function')
        ? OrigBroadcastChannel.prototype.postMessage
        : null;
      const bcNameMap = new WeakMap();
      const patchedBcRealms = new WeakSet();
      const patchBroadcastChannel = (realm) => {{
        try {{
          if (!realm || patchedBcRealms.has(realm)) return;
          const RealmBC = realm.BroadcastChannel;
          if (typeof RealmBC !== 'function' || !RealmBC.prototype) return;
          patchedBcRealms.add(realm);
          const bcNameDesc = Object.getOwnPropertyDescriptor(RealmBC.prototype, 'name');
          if (bcNameDesc && typeof bcNameDesc.get === 'function') {{
            const origBcNameGet = bcNameDesc.get;
            Object.defineProperty(RealmBC.prototype, 'name', {{
              get: new Proxy(origBcNameGet, {{
                apply(target, thisArg, args) {{
                  const nativeName = Reflect.apply(target, thisArg, args);
                  return bcNameMap.has(thisArg) ? bcNameMap.get(thisArg) : nativeName;
                }}
              }}),
              enumerable: bcNameDesc.enumerable,
              configurable: bcNameDesc.configurable,
            }});
          }}
          const WrappedBroadcastChannel = new Proxy(RealmBC, {{
            construct(target, args, newTarget) {{
              if (args.length === 0) return Reflect.construct(target, args, newTarget);
              const userChannelName = `${{args[0]}}`;
              const actualNewTarget = (newTarget === RealmBC) ? WrappedBroadcastChannel : newTarget;
              if (/^\\x00+__fp_bc_/.test(userChannelName)) {{
                const inst = Reflect.construct(target, ['\\x00' + userChannelName, ...args.slice(1)], actualNewTarget);
                bcNameMap.set(inst, userChannelName);
                return inst;
              }}
              return Reflect.construct(target, [userChannelName, ...args.slice(1)], actualNewTarget);
            }}
          }});
          Object.defineProperty(RealmBC.prototype, 'constructor', {{
            value: WrappedBroadcastChannel,
            writable: true,
            enumerable: false,
            configurable: true,
          }});
          Object.defineProperty(realm, 'BroadcastChannel', {{
            value: WrappedBroadcastChannel,
            writable: true,
            enumerable: false,
            configurable: true,
          }});
        }} catch (_) {{}}
      }};
      patchBroadcastChannel(globalThis);
      let patchMessagePort = null;
      for (const FrameProto of [
        typeof HTMLIFrameElement !== 'undefined' ? HTMLIFrameElement.prototype : null,
        typeof HTMLFrameElement !== 'undefined' ? HTMLFrameElement.prototype : null,
      ]) {{
        if (!FrameProto) continue;
        const cwDesc = Object.getOwnPropertyDescriptor(FrameProto, 'contentWindow');
        if (cwDesc && typeof cwDesc.get === 'function') {{
          const origCwGet = cwDesc.get;
          Object.defineProperty(FrameProto, 'contentWindow', {{
            get: new Proxy(origCwGet, {{
              apply(target, thisArg, args) {{
                const cw = Reflect.apply(target, thisArg, args);
                if (cw) {{
                  patchBroadcastChannel(cw);
                  if (patchMessagePort) patchMessagePort(cw);
                }}
                return cw;
              }}
            }}),
            enumerable: cwDesc.enumerable,
            configurable: cwDesc.configurable,
          }});
        }}
      }}
      let swBc = null;
      const sendSwBc = (payload) => {{
        if (swBc && origBcPostMessage) {{
          try {{ Reflect.apply(origBcPostMessage, swBc, [payload]); }} catch (_) {{}}
        }}
      }};
      try {{
        if (typeof window !== 'undefined' && OrigBroadcastChannel && origBcPostMessage) {{
          swBc = Reflect.construct(OrigBroadcastChannel, ['\\x00__fp_bc_sw'], OrigBroadcastChannel);
          swBc.onmessage = (ev) => {{
            const msg = (ev && ev.data) || {{}};
            if (msg.type === 'sync_req') {{
              for (const [k, entry] of localSwReg.entries()) {{
                if (entry && entry.docs && entry.docs.size > 0) {{
                  sendSwBc({{
                    type: 'sync_res',
                    key: k,
                    url: entry.url,
                    docs: Array.from(entry.docs),
                  }});
                }}
              }}
            }} else if (msg.type === 'sync_res' || msg.type === 'announce') {{
              if (!msg.key || !msg.url) return;
              let entry = localSwReg.get(msg.key);
              const incomingDocs = Array.isArray(msg.docs) ? msg.docs : (msg.docId ? [msg.docId] : []);
              if (!entry || entry.docs.size === 0) {{
                entry = {{ url: msg.url, docs: new Set(incomingDocs) }};
                localSwReg.set(msg.key, entry);
              }} else if (entry.url === msg.url) {{
                for (const d of incomingDocs) {{
                  if (d) entry.docs.add(d);
                }}
              }}
              const waiters = pendingSwWaiters.get(msg.key);
              if (waiters) {{
                pendingSwWaiters.delete(msg.key);
                for (const fn of waiters) {{
                  try {{ fn(entry.url); }} catch (_) {{}}
                }}
              }}
            }} else if (msg.type === 'query') {{
              if (!msg.key) return;
              const entry = localSwReg.get(msg.key);
              if (entry && entry.docs && entry.docs.size > 0) {{
                if (msg.docId) entry.docs.add(msg.docId);
                sendSwBc({{
                  type: 'sync_res',
                  key: msg.key,
                  url: entry.url,
                  docs: Array.from(entry.docs),
                }});
              }}
            }} else if (msg.type === 'replace') {{
              if (!msg.key || !msg.url) return;
              localSwReg.set(msg.key, {{ url: msg.url, docs: new Set(msg.docId ? [msg.docId] : []) }});
            }} else if (msg.type === 'release') {{
              if (!msg.docId) return;
              for (const [k, entry] of localSwReg.entries()) {{
                entry.docs.delete(msg.docId);
                if (entry.docs.size === 0) localSwReg.delete(k);
              }}
            }}
          }};
          sendSwBc({{ type: 'sync_req', docId }});
        }}
      }} catch (_) {{}}

      const swPortMap = new WeakMap();
      let origSwPortGet = null;
      if (typeof SharedWorker === 'function' && SharedWorker.prototype) {{
        const swPortDesc = Object.getOwnPropertyDescriptor(SharedWorker.prototype, 'port');
        if (swPortDesc && typeof swPortDesc.get === 'function') {{
          origSwPortGet = swPortDesc.get;
          Object.defineProperty(SharedWorker.prototype, 'port', {{
            get: new Proxy(origSwPortGet, {{
              apply(target, thisArg, args) {{
                const nativePort = Reflect.apply(target, thisArg, args);
                return swPortMap.has(thisArg) ? swPortMap.get(thisArg) : nativePort;
              }}
            }}),
            enumerable: swPortDesc.enumerable,
            configurable: swPortDesc.configurable,
          }});
        }}
      }}

      const buildSharedKey = (resolvedUrl, opts, isModule) => {{
        let name = '';
        let creds = 'same-origin';
        if (typeof opts === 'string') {{
          name = opts;
        }} else if (opts && typeof opts === 'object') {{
          if (opts.name !== undefined) name = String(opts.name);
          if (opts.credentials !== undefined) creds = String(opts.credentials);
        }}
        return JSON.stringify([resolvedUrl, name, isModule ? 'module' : 'classic', creds]);
      }};

      if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {{
        const cleanupDocSwReg = () => {{
          while (activeConnList.length > 0) {{
            releaseConn(activeConnList.pop());
          }}
          activeSharedRefs.length = 0;
          sendSwBc({{ type: 'release', docId }});
        }};
        window.addEventListener('pagehide', cleanupDocSwReg);
        window.addEventListener('unload', cleanupDocSwReg);
      }}

      const origCreateObjectURL = (typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function')
        ? URL.createObjectURL
        : null;
      const origRevokeObjectURL = (typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function')
        ? URL.revokeObjectURL
        : null;
      if (origCreateObjectURL) {{
        URL.createObjectURL = new Proxy(origCreateObjectURL, {{
          apply(target, thisArg, args) {{
            const url = Reflect.apply(target, thisArg, args);
            if (typeof Blob !== 'undefined' && args[0] instanceof Blob) {{
              const b = args[0];
              if (!b.type || /javascript|ecmascript|text\\/plain/i.test(String(b.type))) {{
                blobMap.set(String(url), b);
              }}
            }}
            return url;
          }}
        }});
      }}
      if (origRevokeObjectURL) {{
        URL.revokeObjectURL = new Proxy(origRevokeObjectURL, {{
          apply(target, thisArg, args) {{
            if (args.length > 0) blobMap.delete(String(args[0]));
            return Reflect.apply(target, thisArg, args);
          }}
        }});
      }}

      const OrigMessageChannel = (typeof MessageChannel === 'function') ? MessageChannel : null;
      const OrigBlob = (typeof Blob === 'function') ? Blob : null;
      const OrigErrorEvent = (typeof ErrorEvent === 'function') ? ErrorEvent : null;
      const origAddEventListener = (typeof EventTarget !== 'undefined' && EventTarget.prototype && typeof EventTarget.prototype.addEventListener === 'function')
        ? EventTarget.prototype.addEventListener
        : null;
      const origDispatchEvent = (typeof EventTarget !== 'undefined' && EventTarget.prototype && typeof EventTarget.prototype.dispatchEvent === 'function')
        ? EventTarget.prototype.dispatchEvent
        : null;
      const origPortPostMessage = (typeof MessagePort !== 'undefined' && MessagePort.prototype && typeof MessagePort.prototype.postMessage === 'function')
        ? MessagePort.prototype.postMessage
        : null;
      const origPortStart = (typeof MessagePort !== 'undefined' && MessagePort.prototype && typeof MessagePort.prototype.start === 'function')
        ? MessagePort.prototype.start
        : null;
      const origPortClose = (typeof MessagePort !== 'undefined' && MessagePort.prototype && typeof MessagePort.prototype.close === 'function')
        ? MessagePort.prototype.close
        : null;
      const activeConnList = [];
      let swPortCloseMap = new WeakMap();
      // Share ownership across same-origin realms without a window/storage marker.
      try {{
        if (typeof window !== 'undefined' && window.parent !== window) {{
          const owners = window.parent.MessagePort.prototype.close[CFG.portCloseKey];
          if (owners) swPortCloseMap = owners;
        }}
      }} catch (_) {{}}
      const removeFromActiveRefs = (items) => {{
        for (const item of items) {{
          const idx = activeSharedRefs.indexOf(item);
          if (idx !== -1) activeSharedRefs.splice(idx, 1);
        }}
      }};
      const pushConnRefs = (conn, ...items) => {{
        if (conn.closed) return;
        for (const item of items) {{
          conn.refs.push(item);
          activeSharedRefs.push(item);
        }}
      }};
      const releaseConn = (conn) => {{
        if (!conn || conn.closed) return;
        conn.closed = true;
        const cIdx = activeConnList.indexOf(conn);
        if (cIdx !== -1) activeConnList.splice(cIdx, 1);
        removeFromActiveRefs(conn.refs);
        conn.refs.length = 0;
        if (origPortClose) {{
          if (conn.bridgePort) {{ try {{ Reflect.apply(origPortClose, conn.bridgePort, []); }} catch (_) {{}} }}
          if (conn.currentPort) {{ try {{ Reflect.apply(origPortClose, conn.currentPort, []); }} catch (_) {{}} }}
        }}
      }};
      if (origPortClose) {{
        const patchedMpProtos = new WeakSet();
        patchMessagePort = (realm) => {{
          try {{
            const proto = realm && realm.MessagePort && realm.MessagePort.prototype;
            if (!proto || patchedMpProtos.has(proto)) return;
            const desc = Object.getOwnPropertyDescriptor(proto, 'close');
            if (!desc || typeof desc.value !== 'function' || !desc.configurable) return;
            patchedMpProtos.add(proto);
            Object.defineProperty(proto, 'close', {{
              ...desc,
              value: new Proxy(desc.value, {{
                get(target, key, receiver) {{
                  if (key === CFG.portCloseKey) return swPortCloseMap;
                  return Reflect.get(target, key, receiver);
                }},
                apply(target, thisArg, args) {{
                  const res = Reflect.apply(target, thisArg, args);
                  const release = swPortCloseMap.get(thisArg);
                  if (release) {{
                    swPortCloseMap.delete(thisArg);
                    release();
                  }}
                  return res;
                }}
              }}),
            }});
          }} catch (_) {{}}
        }};
        patchMessagePort(globalThis);
      }}
      const onTarget = (et, type, fn, opts) => {{
        if (origAddEventListener) Reflect.apply(origAddEventListener, et, [type, fn, opts]);
        else et.addEventListener(type, fn, opts);
      }};
      const postPort = (port, data, transfer) => {{
        if (origPortPostMessage) {{
          if (transfer && transfer.length > 0) Reflect.apply(origPortPostMessage, port, [data, transfer]);
          else Reflect.apply(origPortPostMessage, port, [data]);
        }} else if (transfer && transfer.length > 0) {{
          port.postMessage(data, transfer);
        }} else {{
          port.postMessage(data);
        }}
      }};
      const startPort = (port) => {{
        if (origPortStart) Reflect.apply(origPortStart, port, []);
        else port.start();
      }};

      const constructSharedWorkerWithCoord = (target, args, newTarget, swKey, createFn) => {{
        if (!OrigMessageChannel || !origSwPortGet || !origCreateObjectURL || !OrigBlob) {{
          return Reflect.construct(target, [createFn(), ...args.slice(1)], newTarget);
        }}
        const shellBlob = new OrigBlob([''], {{ type: 'text/javascript' }});
        const shellUrl = Reflect.apply(origCreateObjectURL, URL, [shellBlob]);
        let w;
        try {{
          w = Reflect.construct(target, [shellUrl, ...args.slice(1)], newTarget);
        }} finally {{
          if (origRevokeObjectURL) {{
            try {{ Reflect.apply(origRevokeObjectURL, URL, [shellUrl]); }} catch (_) {{}}
          }}
        }}
        let allowSyntheticError = false;
        onTarget(w, 'error', (ev) => {{
          if (!allowSyntheticError) {{
            ev.stopImmediatePropagation();
            ev.preventDefault();
          }}
        }}, true);
        const mc = new OrigMessageChannel();
        const clientPort = mc.port1;
        const bridgePort = mc.port2;
        swPortMap.set(w, clientPort);
        const conn = {{ refs: [], closed: false, bridgePort, currentPort: null }};
        activeConnList.push(conn);
        swPortCloseMap.set(clientPort, () => releaseConn(conn));
        pushConnRefs(conn, w, clientPort, bridgePort);

        let urlPromise = sharedUrlPromiseMap.get(swKey);
        if (!urlPromise) {{
          const candidateUrl = createFn();
          const existing = localSwReg.get(swKey);
          if (existing && existing.docs && existing.docs.size > 0) {{
            existing.docs.add(docId);
            sendSwBc({{
              type: 'announce',
              key: swKey,
              url: existing.url,
              docs: Array.from(existing.docs),
              docId,
            }});
            urlPromise = Promise.resolve({{ canonicalUrl: existing.url, candidateUrl }});
          }} else if (swBc) {{
            urlPromise = new Promise((resolve) => {{
              let settled = false;
              const finish = (chosenUrl) => {{
                if (settled) return;
                settled = true;
                let entry = localSwReg.get(swKey);
                if (!entry || entry.docs.size === 0) {{
                  entry = {{ url: chosenUrl, docs: new Set([docId]) }};
                  localSwReg.set(swKey, entry);
                }} else {{
                  entry.docs.add(docId);
                }}
                sendSwBc({{
                  type: 'announce',
                  key: swKey,
                  url: entry.url,
                  docs: Array.from(entry.docs),
                  docId,
                }});
                resolve({{ canonicalUrl: entry.url, candidateUrl }});
              }};
              const timer = setTimeout(() => {{
                const set = pendingSwWaiters.get(swKey);
                if (set) {{
                  set.delete(onPeerUrl);
                  if (set.size === 0) pendingSwWaiters.delete(swKey);
                }}
                finish(candidateUrl);
              }}, 15);
              const onPeerUrl = (peerUrl) => {{
                clearTimeout(timer);
                finish(peerUrl || candidateUrl);
              }};
              let set = pendingSwWaiters.get(swKey);
              if (!set) {{
                set = new Set();
                pendingSwWaiters.set(swKey, set);
              }}
              set.add(onPeerUrl);
              sendSwBc({{ type: 'query', key: swKey, docId }});
            }});
          }} else {{
            localSwReg.set(swKey, {{ url: candidateUrl, docs: new Set([docId]) }});
            urlPromise = Promise.resolve({{ canonicalUrl: candidateUrl, candidateUrl }});
          }}
          sharedUrlPromiseMap.set(swKey, urlPromise);
        }}

        urlPromise.then(({{ canonicalUrl, candidateUrl }}) => {{
          if (conn.closed) return;
          try {{
            let currentPort = null;
            let currentRealWorker = null;
            let gotMessage = false;
            const pendingOut = [];
            const attachRealWorker = (targetUrl, allowFallback) => {{
              if (conn.closed) return;
              if (currentRealWorker || currentPort) {{
                const stale = [currentRealWorker, currentPort].filter(Boolean);
                removeFromActiveRefs(stale);
                for (const s of stale) {{
                  const rIdx = conn.refs.indexOf(s);
                  if (rIdx !== -1) conn.refs.splice(rIdx, 1);
                }}
              }}
              const realWorker = Reflect.construct(target, [targetUrl, ...args.slice(1)], target);
              const realPort = Reflect.apply(origSwPortGet, realWorker, []);
              currentRealWorker = realWorker;
              currentPort = realPort;
              conn.currentPort = realPort;
              pushConnRefs(conn, realWorker, realPort);
              onTarget(realWorker, 'error', (errEv) => {{
                if (allowFallback && !gotMessage && targetUrl !== candidateUrl) {{
                  localSwReg.set(swKey, {{ url: candidateUrl, docs: new Set([docId]) }});
                  sendSwBc({{ type: 'replace', key: swKey, url: candidateUrl, docId }});
                  sharedUrlPromiseMap.set(swKey, Promise.resolve({{ canonicalUrl: candidateUrl, candidateUrl }}));
                  attachRealWorker(candidateUrl, false);
                  for (const item of pendingOut) {{
                    try {{ postPort(currentPort, item); }} catch (_) {{}}
                  }}
                  return;
                }}
                const rawTarget = String(args[0]);
                if (!gotMessage && targetUrl === candidateUrl && !rawTarget.startsWith('blob:') && targetUrl !== rawTarget) {{
                  attachRealWorker(rawTarget, false);
                  for (const item of pendingOut) {{
                    try {{ postPort(currentPort, item); }} catch (_) {{}}
                  }}
                  return;
                }}
                try {{
                  allowSyntheticError = true;
                  const clonedErr = OrigErrorEvent
                    ? new OrigErrorEvent('error', {{
                        message: errEv.message,
                        filename: errEv.filename,
                        lineno: errEv.lineno,
                        colno: errEv.colno,
                        error: errEv.error,
                        cancelable: true,
                      }})
                    : errEv;
                  if (origDispatchEvent) Reflect.apply(origDispatchEvent, w, [clonedErr]);
                  else w.dispatchEvent(clonedErr);
                  allowSyntheticError = false;
                }} catch (_) {{}}
              }});
              onTarget(realPort, 'message', (ev) => {{
                gotMessage = true;
                pendingOut.length = 0;
                postPort(bridgePort, ev.data, ev.ports);
              }});
              startPort(realPort);
            }};

            attachRealWorker(canonicalUrl, canonicalUrl !== candidateUrl);
            onTarget(bridgePort, 'message', (ev) => {{
              if (ev.ports && ev.ports.length > 0) {{
                postPort(currentPort, ev.data, ev.ports);
              }} else {{
                if (!gotMessage) pendingOut.push(ev.data);
                postPort(currentPort, ev.data);
              }}
            }});
            startPort(bridgePort);
          }} catch (_) {{}}
        }});

        return w;
      }};

      const wrapWorkerCtor = (Ctor, isShared) => {{
        if (typeof Ctor !== 'function') return Ctor;
        const wrapped = new Proxy(Ctor, {{
          construct(target, args, newTarget) {{
            const actualNewTarget = (newTarget === Ctor) ? wrapped : newTarget;
            if (args.length === 0) return Reflect.construct(target, args, actualNewTarget);
            const urlStr = `${{args[0]}}`;
            let normOpts = undefined;
            let normRest = [];
            let isModule = false;
            if (args.length > 1 && args[1] !== undefined) {{
              const rawOpts = args[1];
              if ((typeof rawOpts === 'object' && rawOpts !== null) || typeof rawOpts === 'function') {{
                const rawCreds = rawOpts.credentials;
                const optCreds = rawCreds === undefined ? undefined : `${{rawCreds}}`;
                if (optCreds !== undefined && optCreds !== 'omit' && optCreds !== 'same-origin' && optCreds !== 'include') {{
                  return Reflect.construct(target, [urlStr, {{ credentials: optCreds }}], actualNewTarget);
                }}
                const rawName = rawOpts.name;
                const optName = rawName === undefined ? undefined : `${{rawName}}`;
                const rawType = rawOpts.type;
                const optType = rawType === undefined ? undefined : `${{rawType}}`;
                if (optType !== undefined && optType !== 'classic' && optType !== 'module') {{
                  return Reflect.construct(target, [urlStr, {{ type: optType }}], actualNewTarget);
                }}
                const normDict = {{}};
                if (optCreds !== undefined) normDict.credentials = optCreds;
                if (optName !== undefined) normDict.name = optName;
                if (optType !== undefined) normDict.type = optType;
                normOpts = normDict;
                normRest = [normDict];
                isModule = (optType === 'module');
              }} else if (rawOpts === null) {{
                normRest = [];
              }} else if (isShared) {{
                normOpts = `${{rawOpts}}`;
                normRest = [normOpts];
              }} else {{
                return Reflect.construct(target, [urlStr, rawOpts], actualNewTarget);
              }}
            }}
            const normArgs = [urlStr, ...normRest];
            try {{
              if (blobMap.has(urlStr) && typeof Blob === 'function' && origCreateObjectURL) {{
                const makeBlobPatchedUrl = () => {{
                  const origBlob = blobMap.get(urlStr);
                  let patchedBlob;
                  if (isModule) {{
                    const bootBlob = new Blob([bootstrapCode], {{ type: 'text/javascript' }});
                    const bootUrl = Reflect.apply(origCreateObjectURL, URL, [bootBlob]);
                    const appBlob = new Blob([origBlob], {{ type: origBlob.type || 'text/javascript' }});
                    const appUrl = Reflect.apply(origCreateObjectURL, URL, [appBlob]);
                    const loaderCode = "import " + JSON.stringify(bootUrl) + ";\\nimport " + JSON.stringify(appUrl) + ";\\n";
                    patchedBlob = new Blob([loaderCode], {{ type: 'text/javascript' }});
                  }} else {{
                    patchedBlob = new Blob([bootstrapCode, origBlob], {{
                      type: origBlob.type || 'text/javascript',
                    }});
                  }}
                  return Reflect.apply(origCreateObjectURL, URL, [patchedBlob]);
                }};
                if (isShared) {{
                  return constructSharedWorkerWithCoord(
                    target, normArgs, actualNewTarget, buildSharedKey(urlStr, normOpts, isModule), makeBlobPatchedUrl
                  );
                }}
                return Reflect.construct(target, [makeBlobPatchedUrl(), ...normRest], actualNewTarget);
              }}
              if (!urlStr.startsWith('blob:') && typeof Blob === 'function' && origCreateObjectURL) {{
                const effectiveBase = (CFG && CFG.workerBaseUrl) || (typeof location !== 'undefined' ? location.href : undefined);
                const absUrl = urlStr.startsWith('data:') ? urlStr : new URL(urlStr, effectiveBase).href;
                if (isShared && absUrl.startsWith('data:')) {{
                  return Reflect.construct(target, normArgs, actualNewTarget);
                }}
                if (!absUrl.startsWith('data:')) {{
                  const currentOrigin = (CFG && CFG.workerBaseUrl)
                    ? new URL(CFG.workerBaseUrl).origin
                    : (typeof location !== 'undefined' ? location.origin : '');
                  const targetOrigin = new URL(absUrl).origin;
                  if (currentOrigin && currentOrigin !== 'null' && targetOrigin !== currentOrigin) {{
                    return Reflect.construct(target, normArgs, actualNewTarget);
                  }}
                }}
                const makeUrlPatchedUrl = () => {{
                  const workerCfg = absUrl.startsWith('data:')
                    ? cleanCfg
                    : Object.assign({{}}, cleanCfg, {{ workerBaseUrl: absUrl }});
                  const workerBootstrap = "(" + installRealmStealth.toString() + ")(" + JSON.stringify(workerCfg) + ");\\n";
                  let patchedBlob;
                  if (isModule) {{
                    const bootBlob = new Blob([workerBootstrap], {{ type: 'text/javascript' }});
                    const bootUrl = Reflect.apply(origCreateObjectURL, URL, [bootBlob]);
                    const loaderCode = "import " + JSON.stringify(bootUrl) + ";\\nimport " + JSON.stringify(absUrl) + ";\\n";
                    patchedBlob = new Blob([loaderCode], {{ type: 'text/javascript' }});
                  }} else {{
                    const loaderCode = workerBootstrap + "importScripts(" + JSON.stringify(absUrl) + ");\\n";
                    patchedBlob = new Blob([loaderCode], {{ type: 'text/javascript' }});
                  }}
                  return Reflect.apply(origCreateObjectURL, URL, [patchedBlob]);
                }};
                if (isShared) {{
                  return constructSharedWorkerWithCoord(
                    target, normArgs, actualNewTarget, buildSharedKey(absUrl, normOpts, isModule), makeUrlPatchedUrl
                  );
                }}
                return Reflect.construct(target, [makeUrlPatchedUrl(), ...normRest], actualNewTarget);
              }}
            }} catch (_) {{}}
            return Reflect.construct(target, normArgs, actualNewTarget);
          }}
        }});
        if (Ctor.prototype) {{
          Object.defineProperty(Ctor.prototype, 'constructor', {{
            value: wrapped,
            writable: true,
            enumerable: false,
            configurable: true,
          }});
        }}
        return wrapped;
      }};

      if (typeof Worker === 'function') {{
        Object.defineProperty(globalThis, 'Worker', {{
          value: wrapWorkerCtor(Worker, false),
          writable: true,
          enumerable: false,
          configurable: true,
        }});
      }}
      if (typeof SharedWorker === 'function') {{
        Object.defineProperty(globalThis, 'SharedWorker', {{
          value: wrapWorkerCtor(SharedWorker, true),
          writable: true,
          enumerable: false,
          configurable: true,
        }});
      }}
    }} catch (_) {{}}
  }};

  installRealmStealth({cfg_json});
}})();"""


def bezier_curve(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 12,
    seed: int | None = None,
) -> list[tuple[float, float]]:
    """Generate a human-like cubic Bezier mouse path from `start` to `end`."""
    steps = max(2, int(steps))
    rng = Random(seed) if seed is not None else Random()
    x0, y0 = float(start[0]), float(start[1])
    x3, y3 = float(end[0]), float(end[1])
    dx, dy = x3 - x0, y3 - y0
    dist = math.hypot(dx, dy)
    spread = max(6.0, min(90.0, dist * 0.18))

    # Two intermediate control points with slight perpendicular arc
    c1x = x0 + dx * rng.uniform(0.22, 0.38) + rng.uniform(-spread, spread)
    c1y = y0 + dy * rng.uniform(0.22, 0.38) + rng.uniform(-spread, spread)
    c2x = x0 + dx * rng.uniform(0.62, 0.82) + rng.uniform(-spread * 0.6, spread * 0.6)
    c2y = y0 + dy * rng.uniform(0.62, 0.82) + rng.uniform(-spread * 0.6, spread * 0.6)

    points: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out timing parameter (smoothstep)
        u = t * t * (3.0 - 2.0 * t)
        inv = 1.0 - u
        px = (inv**3) * x0 + 3.0 * (inv**2) * u * c1x + 3.0 * inv * (u**2) * c2x + (u**3) * x3
        py = (inv**3) * y0 + 3.0 * (inv**2) * u * c1y + 3.0 * inv * (u**2) * c2y + (u**3) * y3
        points.append((round(px, 2), round(py, 2)))
    points[-1] = (round(x3, 2), round(y3, 2))
    return points


def keystroke_delays_ms(
    text: str,
    base_wpm: int = 65,
    seed: int | None = None,
) -> list[int]:
    """Return natural per-character typing delays in milliseconds."""
    if not text:
        return []
    rng = Random(seed) if seed is not None else Random()
    base_ms = max(18.0, 60_000.0 / (max(20, base_wpm) * 5.0))
    delays: list[int] = []
    prev = ""
    for ch in text:
        factor = rng.uniform(0.72, 1.32)
        if ch in (" ", ".", ",", ";", ":", "!", "?", "\n"):
            factor *= rng.uniform(1.25, 1.75)
        elif prev and prev.isalpha() and ch.isalpha() and prev.islower() == ch.islower():
            factor *= 0.85
        delays.append(max(12, int(round(base_ms * factor))))
        prev = ch
    return delays


async def human_click_at(
    page: Any,
    x: float,
    y: float,
    steps: int = 10,
    start: tuple[float, float] | None = None,
) -> None:
    """Move the mouse along a cubic Bezier curve to `(x, y)` and click."""
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        return
    move_fn = getattr(mouse, "move", None)
    if callable(move_fn):
        origin = start or getattr(page, "_last_mouse_pos", (max(0.0, x * 0.35), max(0.0, y * 0.35)))
        for px, py in bezier_curve(origin, (x, y), steps=steps):
            await move_fn(px, py)
            await asyncio.sleep(0.004)
        with contextlib.suppress(Exception):
            page._last_mouse_pos = (float(x), float(y))
    await mouse.click(x, y)


async def human_type(
    page: Any,
    text: str,
    delays_ms: list[int] | None = None,
    max_total_sleep: float = 2.5,
) -> None:
    """Type `text` via `page.keyboard` with natural per-character cadence.

    Preserves the relative rhythm of `delays_ms` by scaling the schedule
    proportionally only when the total duration exceeds `max_total_sleep`.
    """
    kb = getattr(page, "keyboard", None)
    if kb is None or not text:
        return
    delays = delays_ms if delays_ms is not None else keystroke_delays_ms(text)
    raw_seconds = [max(0.0, float(d) / 1000.0) for d in delays]
    total_raw = sum(raw_seconds)
    budget = max(0.0, float(max_total_sleep))
    scale = min(1.0, budget / total_raw) if total_raw > 0 else 0.0
    for ch, sec in zip(text, raw_seconds, strict=False):
        await kb.type(ch)
        if scale > 0 and sec > 0:
            await asyncio.sleep(round(sec * scale, 6))


async def human_scroll(
    page: Any,
    delta_y: float = 380.0,
    delta_x: float = 0.0,
    steps: int = 6,
) -> None:
    """Scroll the active page in smooth wheel increments (falling back to window.scrollBy)."""
    steps = max(1, int(steps))
    mouse = getattr(page, "mouse", None)
    wheel_fn = getattr(mouse, "wheel", None) if mouse is not None else None
    if callable(wheel_fn):
        step_x = float(delta_x) / steps
        step_y = float(delta_y) / steps
        for _ in range(steps):
            await wheel_fn(step_x, step_y)
            await asyncio.sleep(0.008)
        return
    eval_fn = getattr(page, "evaluate", None)
    if callable(eval_fn):
        await eval_fn(
            f"window.scrollBy({{left: {float(delta_x)}, top: {float(delta_y)}, behavior: 'smooth'}}); true;"
        )
