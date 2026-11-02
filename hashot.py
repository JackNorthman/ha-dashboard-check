#!/usr/bin/env python3
"""Check a Home Assistant dashboard without a desktop browser.

Starts headless Chrome in Docker (zenika/alpine-chrome), drives it over the
Chrome DevTools Protocol, logs in with a long-lived access token, loads the
dashboard and reports:

  * every hui-error-card anywhere in the (shadow) DOM, with its text
  * counts of the card elements that actually rendered
  * console.error / console.warn output, with objects serialised to JSON
  * uncaught exceptions and failed resource loads
  * a full-page PNG screenshot

    HA_URL=http://homeassistant.local:8123 HA_TOKEN_FILE=~/.ha_token \\
        python3 hashot.py /lovelace/0 --out shot.png

Exit status: 0 = clean; 1 = error cards, an HA error screen, or no cards
rendered at all; 2 = setup failure.
"""
import argparse, asyncio, base64, json, os, re, socket, subprocess, sys, time, urllib.request

try:
    import websockets
except ImportError:
    sys.exit("needs the 'websockets' package:  pip install websockets")

# Walks the whole DOM, descending into every shadow root (HA's frontend is
# Lit web components all the way down, so document.querySelector sees nothing).
DEEP = r"""
(function(){
  const out={err:[],warn:[],screen:[],counts:{}};
  const extra=new Set(%s);
  const txt=el=>(el.shadowRoot?el.shadowRoot.textContent:el.textContent).replace(/\s+/g,' ').trim();
  function walk(root){
    for(const el of root.querySelectorAll('*')){
      const t=el.tagName.toLowerCase();
      if(t.endsWith('-card')||extra.has(t)) out.counts[t]=(out.counts[t]||0)+1;
      if(t==='hui-card'){   // the element hui-card wraps is the card itself, whatever its tag
        for(const c of [...el.children, ...(el.shadowRoot?el.shadowRoot.children:[])]){
          const ct=c.tagName.toLowerCase();
          if(ct!=='style'&&!ct.endsWith('-card')) out.counts[ct]=(out.counts[ct]||0)+1;
        }
      }
      if(t==='hui-error-card'||t==='hui-card-element-editor-error') out.err.push(txt(el).slice(0,300));
      if(t==='hui-warning'||t==='ha-alert') out.warn.push(txt(el).slice(0,200));
      if(t==='hass-error-screen') out.screen.push(txt(el).slice(0,300));
      if(el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  out.url=location.href; out.title=document.title; out.h=document.documentElement.scrollHeight;
  return JSON.stringify(out);
})()
"""

# Installed before any page script runs (Page.addScriptToEvaluateOnNewDocument),
# so card validation errors logged during the first render are captured, with
# their object arguments JSON-serialised instead of CDP's truncated previews.
HOOK = r"""(function(){
  window.__hashot=[];
  const ser=x=>{ if(typeof x==='string') return x;
    try{ return JSON.stringify(x,(k,v)=>(v instanceof Error)?('Error: '+v.message+(v.stack?' @ '+String(v.stack).split('\n')[1]:'')):v).slice(0,3000); }
    catch(e){ return String(x); } };
  for(const lvl of ['error','warn']){
    const orig=console[lvl].bind(console);
    console[lvl]=(...a)=>{ try{ if(window.__hashot.length<500){
      let b=a;   // drop the CSS arguments that pair with %c styling markers
      if(typeof b[0]==='string'&&b[0].includes('%c')){ const k=b[0].split('%c').length-1; b=[b[0].replace(/%c/g,''),...b.slice(1+k)]; }
      window.__hashot.push(lvl.toUpperCase()+': '+b.map(ser).join(' ').replace(/\s+/g,' ').trim().slice(0,3000)); } }catch(e){}
      orig(...a); };
  }
})();"""


def read_token(args):
    tok = os.environ.get("HA_TOKEN", "").strip()
    path = args.token_file or os.environ.get("HA_TOKEN_FILE")
    if not tok and path:
        with open(os.path.expanduser(path)) as f:
            tok = f.read().strip()
    if not tok:
        sys.exit("no token: set HA_TOKEN, or HA_TOKEN_FILE / --token-file to a file holding one")
    return tok


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def start_chrome(args, port, name):
    cmd = ["docker", "run", "-d", "--rm", "--name", name,
           # /dev/shm in a container defaults to 64 MB. Without the next two
           # options Chrome runs out, the HA frontend fails with
           # ERR_INSUFFICIENT_RESOURCES and you get "Error while loading page",
           # which looks like a broken card but isn't.
           "--shm-size=1g", f"--memory={args.memory}"]
    if args.network == "host":
        cmd += ["--network", "host"]
        bind = "127.0.0.1"                 # never expose CDP beyond loopback
    else:
        if args.network:
            cmd += ["--network", args.network]
        cmd += ["-p", f"127.0.0.1:{port}:{port}"]
        bind = "0.0.0.0"                   # inside the container; published to host loopback only
    cmd += [args.image, "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars",
            f"--window-size={args.width},{args.height}", f"--remote-debugging-address={bind}",
            f"--remote-debugging-port={port}", "about:blank"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print("docker run failed:", r.stderr.strip(), file=sys.stderr)
        sys.exit(2)
    for _ in range(80):
        try:
            return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2))
        except Exception:
            time.sleep(0.25)
    print("Chrome's DevTools endpoint never came up", file=sys.stderr)
    sys.exit(2)


async def check(args, ws_url, token):
    ha = args.url.rstrip("/")
    keep = re.compile(args.console_filter, re.I) if args.console_filter else None
    drop = re.compile(args.ignore, re.I) if args.ignore else None
    logs = []

    def wanted(line):
        return (not drop or not drop.search(line)) and (not keep or keep.search(line))

    async with websockets.connect(ws_url, max_size=None) as ws:
        pending, n = {}, [0]

        def note(r):
            m, p = r.get("method"), r.get("params", {})
            if m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {}); ex = d.get("exception", {})
                logs.append("EXCEPTION: " + (ex.get("description") or d.get("text") or "")[:700])
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") in ("error", "warning"):
                e = p["entry"]
                logs.append(f"LOG {e['level'].upper()}: {e.get('text', '')[:300]} {e.get('url', '')[:160]}".rstrip())

        async def reader():
            async for raw in ws:
                r = json.loads(raw)
                if "id" in r and r["id"] in pending:
                    pending.pop(r["id"]).set_result(r.get("result", r))
                else:
                    note(r)

        async def cmd(method, sid=None, **params):
            n[0] += 1; i = n[0]
            msg = {"id": i, "method": method, "params": params}
            if sid:
                msg["sessionId"] = sid
            fut = asyncio.get_running_loop().create_future(); pending[i] = fut
            await ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, 60)

        rt = asyncio.create_task(reader())
        t = await cmd("Target.createTarget", url="about:blank")
        sid = (await cmd("Target.attachToTarget", targetId=t["targetId"], flatten=True))["sessionId"]
        for dom in ("Page", "Runtime", "Log"):
            await cmd(f"{dom}.enable", sid)
        await cmd("Page.addScriptToEvaluateOnNewDocument", sid, source=HOOK)
        await cmd("Emulation.setDeviceMetricsOverride", sid, width=args.width, height=args.height,
                  deviceScaleFactor=1, mobile=False)

        # 1. land on the HA origin so localStorage belongs to it
        await cmd("Page.navigate", sid, url=f"{ha}/auth/authorize?client_id={ha}/&redirect_uri={ha}/")
        await asyncio.sleep(2.5)
        # 2. the frontend keeps its auth in localStorage.hassTokens; a long-lived
        #    access token works as the access_token with a far-future expiry
        tok = {"access_token": token, "token_type": "Bearer", "expires_in": 315360000,
               "hassUrl": ha, "clientId": f"{ha}/", "refresh_token": "",
               "expires": int(time.time() * 1000) + 315360000 * 1000}
        await cmd("Runtime.evaluate", sid,
                  expression="localStorage.setItem('hassTokens', %s)" % json.dumps(json.dumps(tok)))
        # 3. load the dashboard for real
        logs.clear()
        await cmd("Page.navigate", sid, url=ha + args.path)
        await asyncio.sleep(args.wait)

        info = await cmd("Runtime.evaluate", sid, expression=DEEP % json.dumps(args.count),
                         returnByValue=True)
        val = json.loads(info.get("result", {}).get("value", "{}"))
        hooked = await cmd("Runtime.evaluate", sid, expression="JSON.stringify(window.__hashot||[])",
                           returnByValue=True)
        console = json.loads(hooked.get("result", {}).get("value", "[]"))

        shot_bytes = 0
        if args.out:
            shot = await cmd("Page.captureScreenshot", sid, format="png", captureBeyondViewport=True)
            with open(args.out, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            shot_bytes = os.path.getsize(args.out)
        rt.cancel()

    seen, events = set(), []
    for line in console + logs:
        if line[:200] in seen or not wanted(line):
            continue
        seen.add(line[:200]); events.append(line)

    result = {
        "url": val.get("url"), "title": val.get("title"), "scroll_height": val.get("h"),
        "counts": val.get("counts", {}), "error_cards": val.get("err", []),
        "warnings": val.get("warn", []), "error_screen": val.get("screen", []),
        "console": events, "screenshot": args.out, "screenshot_bytes": shot_bytes,
    }
    result["no_cards"] = not result["counts"].get("hui-card") and not result["error_cards"]
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print("PAGE:", result["url"], "|", result["title"], "| scrollHeight", result["scroll_height"])
        print("COUNTS:", json.dumps(result["counts"], sort_keys=True))
        print(f"ERROR CARDS ({len(result['error_cards'])}):")
        for e in result["error_cards"]:
            print("  -", e)
        if result["warnings"]:
            print("WARNINGS:", result["warnings"][:8])
        for e in result["error_screen"]:
            print("ERROR SCREEN:", e)
        if "/auth/authorize" in (result["url"] or ""):
            print("NOT LOGGED IN: Home Assistant rejected the token (wrong token, or wrong HA_URL origin)")
        elif result["no_cards"]:
            print("NO CARDS RENDERED: wrong path, or still loading (try a longer --wait)")
        for line in events[:args.max_console]:
            print("CONSOLE", line[:1500])
        if args.out:
            print("wrote", args.out, shot_bytes, "bytes")
    if result["error_screen"] and any("ERR_INSUFFICIENT_RESOURCES" in l for l in console + logs):
        print("hint: Chrome ran out of shared memory -- this is the browser, not your dashboard",
              file=sys.stderr)
    return 1 if (result["error_cards"] or result["error_screen"] or result["no_cards"]) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="dashboard path, e.g. /lovelace/0 or /dashboard-test/overview")
    ap.add_argument("--url", default=os.environ.get("HA_URL"),
                    help="Home Assistant base URL, as Chrome inside Docker will see it (default $HA_URL)")
    ap.add_argument("--token-file", help="file holding a long-lived access token (default $HA_TOKEN_FILE; "
                                         "$HA_TOKEN takes precedence)")
    ap.add_argument("--out", default="hashot.png", help="screenshot path; '' to skip (default hashot.png)")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--wait", type=float, default=9.0, help="seconds to let the dashboard settle (default 9)")
    ap.add_argument("--image", default="zenika/alpine-chrome:latest", help="Chrome Docker image")
    ap.add_argument("--memory", default="2g", help="container memory limit (default 2g)")
    ap.add_argument("--network", help="docker network for Chrome; 'host' if HA is on this machine's localhost")
    ap.add_argument("--count", action="append", default=[], metavar="TAG",
                    help="extra element tag to count (anything ending in -card is counted already)")
    ap.add_argument("--console-filter", metavar="REGEX", help="only report console lines matching this")
    ap.add_argument("--ignore", default="favicon|Custom state pseudo classes", metavar="REGEX",
                    help="drop console lines matching this (default: favicon 404s and a Chrome "
                         "deprecation notice the HA frontend triggers on every page)")
    ap.add_argument("--max-console", type=int, default=50, help="max console lines printed (default 50)")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    args = ap.parse_args()
    if not args.url:
        sys.exit("no Home Assistant URL: set HA_URL or pass --url")
    if not args.path.startswith("/"):
        args.path = "/" + args.path
    token = read_token(args)

    port, name = free_port(), f"hashot-{os.getpid()}"
    try:
        v = start_chrome(args, port, name)
        rc = asyncio.run(check(args, v["webSocketDebuggerUrl"], token))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
