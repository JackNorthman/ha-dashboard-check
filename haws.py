#!/usr/bin/env python3
"""One-shot Home Assistant websocket command helper.

Sends each JSON argument as a websocket command (ids are filled in for you)
and prints the result(s) as JSON.

    export HA_URL=http://homeassistant.local:8123 HA_TOKEN_FILE=~/.ha_token
    python3 haws.py '{"type": "lovelace/dashboards/list"}'
    python3 haws.py '{"type": "lovelace/config", "url_path": null}'
    python3 haws.py '{"type": "get_states"}' '{"type": "get_config"}'

Exit status is 1 if any command came back with "success": false.
"""
import asyncio, json, os, sys

try:
    import websockets
except ImportError:
    sys.exit("needs the 'websockets' package:  pip install websockets")


def config():
    url = os.environ.get("HA_URL", "").rstrip("/")
    if not url:
        sys.exit("set HA_URL, e.g. http://homeassistant.local:8123")
    tok = os.environ.get("HA_TOKEN", "").strip()
    if not tok and os.environ.get("HA_TOKEN_FILE"):
        with open(os.path.expanduser(os.environ["HA_TOKEN_FILE"])) as f:
            tok = f.read().strip()
    if not tok:
        sys.exit("set HA_TOKEN, or HA_TOKEN_FILE to a file holding a long-lived access token")
    if url.startswith("https://"):
        ws = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        ws = "ws://" + url[len("http://"):]
    else:
        ws = url
    return ws + "/api/websocket", tok


async def main(cmds):
    url, token = config()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()                                    # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        r = json.loads(await ws.recv())
        if r.get("type") != "auth_ok":
            sys.exit(f"auth failed: {r}")
        out = []
        for i, c in enumerate(cmds, start=1):
            c = dict(c); c["id"] = i
            await ws.send(json.dumps(c))
            while True:                                    # skip unrelated events
                r = json.loads(await ws.recv())
                if r.get("id") == i:
                    break
            out.append(r)
    print(json.dumps(out if len(out) > 1 else out[0], indent=1))
    return 1 if any(r.get("success") is False for r in out) else 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip()); sys.exit(0 if len(sys.argv) > 1 else 2)
    try:
        cmds = [json.loads(a) for a in sys.argv[1:]]
    except json.JSONDecodeError as e:
        sys.exit(f"bad JSON argument: {e}")
    sys.exit(asyncio.run(main(cmds)))
