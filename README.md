# ha-dashboard-check

Check a Home Assistant dashboard from the command line, with no desktop browser
and no clicking around.

`hashot.py` starts headless Chrome in Docker, logs in to Home Assistant with a
long-lived access token, loads a dashboard and tells you:

- **error cards**: every `hui-error-card` anywhere on the page, including deep
  inside shadow DOM, with its message
- **what rendered**: counts of the card elements actually on the page (built-in
  and custom), so "the card is silently missing" shows up too
- **console output**: `console.error` / `console.warn` captured from the first
  moment of page load, with object arguments serialised to JSON. Many custom cards
  report config validation problems only there.
- **exceptions and failed loads**: uncaught JS exceptions, 404s on card resources
- **a full-page screenshot**, beyond the viewport

`haws.py` is a one-shot helper for Home Assistant's websocket API: list
dashboards, fetch or save a dashboard config, read states.

It's handy in CI and for AI coding agents that edit dashboards: make a change,
run the check, read the errors, repeat, without a human refreshing a browser.

## Requirements

- Docker (pulls [`zenika/alpine-chrome`](https://hub.docker.com/r/zenika/alpine-chrome))
- Python 3.8+ and `pip install -r requirements.txt` (just `websockets`)
- A Home Assistant **long-lived access token** (your profile → Security →
  Long-lived access tokens)

## Configuration

Both scripts read the same environment variables:

| variable | |
|---|---|
| `HA_URL` | base URL, e.g. `http://homeassistant.local:8123`. For `hashot.py`, this is the address **Chrome inside Docker** uses, so `localhost` only works with `--network host`. |
| `HA_TOKEN` | the token itself |
| `HA_TOKEN_FILE` | or a file holding it (`hashot.py` also takes `--token-file`) |

`HA_TOKEN` wins if both are set. There's deliberately no `--token` argument:
command lines show up in `ps` and shell history.

## hashot.py

```bash
export HA_URL=http://homeassistant.local:8123
export HA_TOKEN_FILE=~/.ha_token

python3 hashot.py /lovelace/0 --out overview.png
```

```
PAGE: http://homeassistant.local:8123/lovelace/0 | Overview – Home Assistant | scrollHeight 1725
COUNTS: {"hui-card": 52, "hui-entities-card": 6, "hui-tile-card": 31, "mushroom-light-card": 4}
ERROR CARDS (1):
  - Configuration error type: custom:mushroom-ligth-card ...
CONSOLE WARN: [SomeCard] Config validation warnings: [{"field":"entity","message":"Entity not found", ...}]
wrote overview.png 192844 bytes
```

Exit status: **0** clean; **1** error cards, an HA error screen, or no cards
rendered at all (wrong path, rejected token); **2** setup failure (Docker,
Chrome).

| option | default | |
|---|---|---|
| `path` | | dashboard path, e.g. `/lovelace/0`, `/my-dashboard/kitchen` |
| `--out FILE` | `hashot.png` | screenshot; `--out ''` skips it |
| `--width` / `--height` | 1600 × 1000 | viewport. Sections views reflow their column count with width, so test the widths you care about. |
| `--wait SEC` | 9 | settle time after load; raise it for heavy dashboards |
| `--json` | | print one JSON object (for scripts and agents) |
| `--console-filter RE` | | only report console lines matching this |
| `--ignore RE` | favicon 404s and Chrome's `Custom state pseudo classes` deprecation notice | drop console lines matching this |
| `--count TAG` | | extra element tags to count (repeatable) |
| `--network NET` | Docker default | `host` when HA runs on the same machine's `localhost` |
| `--image` | `zenika/alpine-chrome:latest` | pin a version for reproducible CI |
| `--memory` | `2g` | container memory limit |

### How it works

1. Runs `zenika/alpine-chrome` with `--remote-debugging-port`, published on
   **127.0.0.1 only**. The DevTools protocol gives full control of the browser, so
   never expose it further.
2. Connects over the Chrome DevTools Protocol (CDP) with `websockets`.
3. Registers a console hook with `Page.addScriptToEvaluateOnNewDocument`, so it
   runs before any frontend code and catches errors logged during the first render.
4. Opens the HA origin and writes `localStorage.hassTokens`, which is where the
   frontend keeps its auth. The long-lived token goes in as the access token with
   a far-future expiry.
5. Loads the dashboard, waits, then walks the whole DOM, descending into every
   shadow root (HA's frontend is web components all the way down, so a plain
   `document.querySelectorAll` finds nothing).
6. Takes a `captureBeyondViewport` screenshot and removes the container.

### Gotcha: "Error while loading page" that isn't your dashboard

Docker gives containers a 64 MB `/dev/shm`. That's not enough for Chrome on a real
dashboard: it fails with `ERR_INSUFFICIENT_RESOURCES`, and HA shows **"Error while
loading page"**. That looks exactly like a broken card, but it isn't one.
`hashot.py` always runs Chrome with:

```
--shm-size=1g          (docker)
--disable-dev-shm-usage (chrome)
```

Keep both if you adapt the docker command.

### Other notes

- Each wrong token is a failed login. If you've enabled `ip_ban_enabled` /
  `login_attempts_threshold` in HA's `http:` config, repeated runs with a bad
  token can ban the machine running the check.
- The token sits in the browser's localStorage only for the life of a throwaway
  container, but treat it as a real credential all the same. Use a dedicated
  user's token for CI, and a non-admin one if you only need to view.
- Custom cards often log validation *warnings* (missing entity and so on) that
  never turn into an error card. Those show up under `CONSOLE`.

## haws.py

```bash
python3 haws.py '{"type": "lovelace/dashboards/list"}'
python3 haws.py '{"type": "lovelace/config", "url_path": "my-dashboard"}' > my-dashboard.json
python3 haws.py '{"type": "get_states"}' '{"type": "get_config"}'
```

Each argument is one websocket command; the `id` is filled in for you. Several
commands share one connection. The exit status is 1 if any of them returns
`"success": false`.

`lovelace/config/save` **replaces** a dashboard's whole config. Fetch and keep a
copy first.

## Agent or CI loop

```bash
# 1. back up, 2. save your edited config, 3. check it
python3 haws.py '{"type": "lovelace/config", "url_path": "test-dash"}' > backup.json
python3 haws.py "$(jq -c '{type: "lovelace/config/save", url_path: "test-dash", config: .}' new-config.json)"
python3 hashot.py /test-dash/0 --json --out test-dash.png
```

A non-zero exit from `hashot.py` fails the job. The JSON includes the error-card
text and console lines for whoever, or whatever, fixes it next. Point it at a
scratch dashboard rather than one people use.

## Licence

MIT. See [LICENSE](LICENSE). Not affiliated with Home Assistant or the Open Home
Foundation.
