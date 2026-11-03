# Open WebUI on the home server

Always-on Open WebUI for the Debian 13 laptop (`homeserver`), plus the Seedream
image proxy from `RunpodImage` and SearXNG for web search. Reached only through Tailscale; the language
models run elsewhere.

```
 Phone / PC ──(Tailscale, HTTPS)──► homeserver: tailscale serve ──► 127.0.0.1:3000
                                                                     │
                                        ┌────────────── Docker Compose project "homeserver-ai" ──────────────┐
                                        │  open-webui (official image, chats in ./data)                       │
                                        │      │ private network                                              │
                                        │      ├──► seedream:8787 (image proxy) ──────────► BytePlus Seedream │
                                        │      └──► searxng:8080 (web search) ────────────► search engines    │
                                        └─────────────────────────────────────────────────────────────────────┘
                     open-webui ──► Venice API (daily chat)
                     open-webui ──► Runpod GPU Pod :8000 (only while you run it)
```

## Files

| File | What it is |
|---|---|
| `docker-compose.yml` | The three services and all their settings |
| `.env.example` | Template for `.env` (secrets and per-machine values) |
| `seedream/` | The image proxy (`seedream_proxy.py`, same code as `RunpodImage` plus `SEEDREAM_HOST`), its `Dockerfile` and pinned `requirements.txt` |
| `searxng/settings.yml` | SearXNG settings (turns on the JSON output Open WebUI needs) |
| `tests/` | Proxy tests (no network needed) |
| `scripts/backup.sh` | Consistent backup of the database and uploads |

## Setup

Already done on `homeserver`: Debian 13, Docker with the Compose plugin, Tailscale.

### 1. Copy this folder to the server

From PowerShell on the PC:

```powershell
scp -r D:\Development\AI\HomeserverImage joaom@homeserver:~/homeserver-ai
```

(Or push it to a private Git repository and `git clone` it on the server.)

### 2. Create `.env`

On the server (`ssh joaom@homeserver`):

```bash
cd ~/homeserver-ai
cp .env.example .env && chmod 600 .env
openssl rand -hex 32        # paste the output into WEBUI_SECRET_KEY
openssl rand -hex 32        # paste this one into SEARXNG_SECRET
nano .env
```

Fill in `WEBUI_SECRET_KEY`, `SEARXNG_SECRET`, `SEARXNG_BASE_URL`, `WEBUI_URL`, `WEBUI_ADMIN_EMAIL`, `WEBUI_ADMIN_PASSWORD`,
`OPENAI_API_BASE_URLS`, `OPENAI_API_KEYS` and `ARK_API_KEY`. `WEBUI_URL` is this machine's Tailscale name with `https://`:
`tailscale dns status` prints it ("Other devices in your tailnet can reach
this device at …").

**Do this before the first start.** Open WebUI copies most settings into its
database the first time it starts with an empty `data` folder. After that, the
database wins: change them in **Admin Panel › Settings**, not in `.env`.

### 3. Start

```bash
docker compose up -d --build
docker compose ps                      # all three "running"; open-webui becomes "healthy" after a while
docker compose logs -f open-webui      # Ctrl+C to stop following
```

The first start takes several minutes on this hardware: Open WebUI runs its
database setup and downloads its small local models (document embeddings,
speech-to-text) into `./data`. Then check:

```bash
curl -s http://127.0.0.1:3000/health                    # {"status":true}
docker compose exec seedream python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8787/health').read().decode())"
```

The second command shows the proxy's defaults (`pod_key_set: true` means
`ARK_API_KEY` arrived; the key itself is never shown).

### 4. HTTPS on your tailnet

1. In the Tailscale admin console, **DNS** page: enable **HTTPS Certificates**
   (MagicDNS is already on).
2. On the server:

```bash
sudo tailscale serve --bg 3000
tailscale serve status
```

`--bg` keeps it running across reboots. Open `WEBUI_URL` on the phone or PC.
Phones need HTTPS for the microphone and for installing the site as an app.
To stop serving: `sudo tailscale serve reset`.

### 5. First login and lock-down

- **With `WEBUI_ADMIN_EMAIL` and `WEBUI_ADMIN_PASSWORD` set** (as on the Pod):
  Open WebUI created that admin account on the first start and turned off new
  sign-ups. Log in with it, then delete the `WEBUI_ADMIN_PASSWORD` line from
  `.env`: it's only read while the database has no users, so keeping the
  plain-text password on disk has no benefit.
- **Without them:** the first account you create becomes the admin. Create it,
  then in **Admin Panel › Settings › General** turn off **Enable New Sign Ups**.
- **If you moved your chats from the Pod:** your Pod account (email and
  password) comes with the database, and the admin variables are ignored
  because a user already exists.

### 6. Use the Runpod GPU Pod from here

Open WebUI and the image proxy no longer need to run on the Pod. In the Pod's
settings (Edit Pod):

- Environment: `OWUI_ENABLE=false`, `SEEDREAM_ENABLE=false` (keep `LLAMA_API_KEY`).
- HTTP ports: `8000` only.

Then add the Pod in **Admin Panel › Settings › Connections** (or in `.env`
before the first start): URL `https://<pod-id>-8000.proxy.runpod.net/v1`, key =
the Pod's `LLAMA_API_KEY`. The Pod ID, and so the URL, changes if the Pod is
migrated. While the Pod is stopped, its models just disappear from the list
after a 3-second wait.

### 7. Web search

Open WebUI reaches SearXNG directly over the private Compose network. Check it
from inside Open WebUI's container:

```bash
docker compose exec open-webui curl -s -o /dev/null -w '%{http_code}\n' 'http://searxng:8080/search?q=test&format=json'   # 200
```

If you added SearXNG to a server that was already running, the database may
override the values in `docker-compose.yml`. In **Admin Panel › Settings › Web
Search**, check that search is on, the engine is `searxng` and the query URL is
`http://searxng:8080/search`. Then turn on web search in a chat.

Search engines see this machine's home IP address, and heavy use can get it
blocked or sent CAPTCHAs (see Possible errors).

To use SearXNG's own search page from your devices, set `SEARXNG_BASE_URL` in
`.env` to your `WEBUI_URL` plus `/searxng/`, then:

```bash
docker compose up -d searxng
sudo tailscale serve --bg --set-path=/searxng 8888
```

Open it at `WEBUI_URL` plus `/searxng/`. Open WebUI stays at `/`. To remove only
this page: `sudo tailscale serve --set-path=/searxng off`.

**SearXNG has no login.** Every device allowed to reach this machine's port 443
can use it, including other accounts on your tailnet. Don't use `/search` as the
path: it collides with SearXNG's own results page, and searches land on the home
page.

### 8. Backups

```bash
sudo apt install -y sqlite3
sudo sh scripts/backup.sh          # writes ./backups/webui-<date>.db and uploads-<date>.tar.gz
```

Nightly at 03:30: `sudo crontab -e` and add

```
30 3 * * * sh /home/joaom/homeserver-ai/scripts/backup.sh >> /var/log/openwebui-backup.log 2>&1
```

Keeps the newest 14 of each (`BACKUP_KEEP`). The backups still sit on the same
disk: copy `backups/` to another device now and then, for example from
PowerShell with `scp -r joaom@homeserver:~/homeserver-ai/backups D:\Backups\homeserver-ai`.
Also keep a copy of `.env` somewhere safe: without `WEBUI_SECRET_KEY`, a
restored database logs everyone out.

**Restore:** `docker compose stop open-webui`, copy the backup over
`data/open-webui/webui.db` (and delete any `webui.db-wal` / `webui.db-shm`
next to it), unpack the uploads, `docker compose start open-webui`.

## Optional: bring your chats from the Pod

Do this **before step 3** (or delete `./data` first). Both sides must run the
same Open WebUI version (the Pod was built with 0.12.5).

1. In the Pod's terminal (Runpod › Connect), make a consistent copy of the
   database (a plain copy of a running SQLite file can miss recent writes),
   pack it with the uploads and, if you used Documents/Knowledge, the
   `vector_db` folder, and send it:

```bash
cd /workspace/open-webui
/opt/open-webui/bin/python -c "import sqlite3; s=sqlite3.connect('webui.db'); d=sqlite3.connect('/workspace/webui.db'); s.backup(d); d.close()"
tar -czf /workspace/owui-export.tgz -C /workspace webui.db -C /workspace/open-webui $(ls -d uploads vector_db 2>/dev/null)
runpodctl send /workspace/owui-export.tgz        # prints a one-time code
```

Runpod says `runpodctl` is pre-installed on all Pods; check with `which runpodctl`.

2. On the server, receive it with `runpodctl receive <code>` ([install
   runpodctl](https://docs.runpod.io/runpodctl/overview)), then:

```bash
mkdir -p ~/homeserver-ai/data/open-webui
tar -xzf owui-export.tgz -C ~/homeserver-ai/data/open-webui
```

3. Start (step 3), log in with your **Pod account** (a new session is normal),
   then fix the settings that came from the Pod, because the database wins
   over `.env`:
   - **Connections:** Venice, and the Pod's proxy URL instead of `http://127.0.0.1:8000/v1`.
   - **Images:** both API Base URLs → `http://seedream:8787/v1`.
   - **General:** WebUI URL → your `WEBUI_URL`.

## Updating Open WebUI

Updates change the database and can't be undone, so back up first.

```bash
sudo sh scripts/backup.sh
nano .env                          # OPEN_WEBUI_TAG=v0.x.y (see the releases page)
docker compose pull open-webui
docker compose up -d
```

Proxy changes: edit `seedream/`, then `docker compose up -d --build seedream`.

SearXNG has no data to back up: set a newer `SEARXNG_TAG` in `.env`, then
`docker compose pull searxng && docker compose up -d searxng`.

## Tests (proxy)

```bash
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m pytest -q tests
```

## Possible errors

| Symptom | Cause / fix |
|---|---|
| `required variable WEBUI_SECRET_KEY is missing` (or `OPENAI_API_…`) | `.env` is missing that value, or you're not in `~/homeserver-ai`. |
| A value with `$` gets mangled | Compose expands `$` in `.env`. Write `$$` for a literal `$`. |
| `open-webui` restarts in a loop | `docker compose logs open-webui`. "Killed" or exit code 137 means out of memory: raise `OWUI_MEM_LIMIT`. |
| Site loads but chats never answer / "connection lost" | WebSockets blocked, or `WEBUI_URL` doesn't match the address you use. Open it exactly as `WEBUI_URL`. Tailscale's docs don't mention WebSockets; if it's the cause, set `ENABLE_WEBSOCKET_SUPPORT=false` in the compose file as a test. |
| `tailscale serve` asks you to enable HTTPS | Do step 4.1, then run it again. |
| Images fail with "Bad Request" | Open WebUI hides the reason: `docker compose logs seedream` shows BytePlus's error code. |
| Proxy log: `401 no API key` | `ARK_API_KEY` empty in `.env`, or Open WebUI's image key field isn't `default`. Then `docker compose up -d seedream`. |
| Changed `.env`, nothing happened | That setting lives in the database now (see step 2). Change it in Admin Panel › Settings. |
| Pod models missing | Pod stopped, port 8000 not exposed, wrong key, or the Pod ID changed. Test from the server: `curl -s -H "Authorization: Bearer <llama-key>" https://<pod-id>-8000.proxy.runpod.net/v1/models` |
| Very long Pod requests fail | Possibly Runpod's HTTP proxy timeout (not verified for this setup). Shorter contexts help; streaming keeps the connection active. |
| Web search fails with a 403 | `search.formats` in `searxng/settings.yml` is missing `json`. After fixing it: `docker compose restart searxng`. |
| Web search returns few or no results | A search engine is blocking this IP: `docker compose logs searxng` names it. Disable that engine in `searxng/settings.yml` or try again later. |
| Web search is very slow | Open WebUI fetches and embeds every result page. Set **Bypass Embedding and Retrieval** in Admin Panel › Settings › Web Search; this can send more text to the model, which costs more tokens on Venice. |
| `backup.sh`: `unable to open database` | Run it with `sudo`. |
