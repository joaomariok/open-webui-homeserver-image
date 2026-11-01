"""
seedream_proxy.py - lets Open WebUI generate AND edit images with BytePlus
ModelArk Seedream.

Open WebUI's "Default (Open AI)" image engine speaks OpenAI's image API.
Seedream does both jobs through one endpoint, POST {base}/images/generations,
with input images in an `image` field. There is no /images/edits endpoint.

  Open WebUI --POST /v1/images/generations (JSON)------> proxy --JSON--------------> Seedream
  Open WebUI --POST /v1/images/edits (multipart files)--> proxy --JSON + data URIs--> Seedream
  GET /health  shows the defaults in effect (never the key)

Listens on 127.0.0.1 by default: it attaches the server's API key to
requests, so it must never be reachable from outside. In Docker Compose, set
SEEDREAM_HOST=0.0.0.0 so the Open WebUI container can reach it over the
private Compose network (http://seedream:8787/v1), and do NOT publish its
port to the host.

Which upstream, key and model (first match wins)
------------------------------------------------
  URL    Open WebUI "API Base URL" = http://127.0.0.1:8787/https://<host>/<path>
                                       -> https://<host>/<path>
         Open WebUI "API Base URL" = http://127.0.0.1:8787/v1 -> ARK_BASE_URL
  Key    Open WebUI "API Key" = a real key                    -> that key
         Open WebUI "API Key" = default (or empty, or the llama key Open WebUI
         fills in by itself)                                  -> ARK_API_KEY
         ARK_API_KEY is only sent to ARK_BASE_URL or to hosts in ARK_KEY_HOSTS.
  Model  Open WebUI "Model" = a model ID                      -> that model
         Open WebUI "Model" = default (or empty)              -> SEEDREAM_MODEL

Output size
-----------
  Generate  the size Open WebUI sends (its "Image Size" field, or the chat
            model's tool call); if none, GEN_DEFAULT_SIZE (1K). A WxH outside
            [SEEDREAM_MIN_PIXELS, SEEDREAM_MAX_PIXELS] is scaled into it,
            keeping the aspect ratio.
  Edit      EDIT_SIZE_MODE:
            original  (default) the first input image's own size, aspect ratio
                      kept. Sent exactly as it is, unless its pixel count is
                      outside [SEEDREAM_MIN_PIXELS, SEEDREAM_MAX_PIXELS]: then
                      it is scaled into that range. Any size Open WebUI sends
                      is ignored.
            tier      Seedream's preset "1K", or "2K" when the first input has
                      more than EDIT_2K_ABOVE_PIXELS pixels. Open WebUI's size
                      is ignored.
            requested the size Open WebUI sends; "original" when it sends none.
  A WxH under 1024x1024 pixels that Seedream rejects as a bad size is retried
  once at 1024x1024 pixels (same aspect ratio).

Environment variables (all optional)
------------------------------------
  ARK_BASE_URL         https://ark.ap-southeast.bytepluses.com/api/v3
  ARK_API_KEY          Seedream key (falls back to RUNPOD_SECRET_ARK_API_KEY)
  ARK_KEY_HOSTS        .bytepluses.com   comma list; ".x.com" = x.com and subdomains
  SEEDREAM_MODEL       dola-seedream-5-0-pro-260628
  SEEDREAM_WATERMARK   false
  GEN_DEFAULT_SIZE     1K
  EDIT_SIZE_MODE       original          original | tier | requested
  EDIT_2K_ABOVE_PIXELS 4194304           tier mode threshold (2048x2048)
  SEEDREAM_MIN_PIXELS  921600            Seedream 5.0 pro/flash minimum (1280x720)
  SEEDREAM_MAX_PIXELS  4194304           2048x2048 (BytePlus documents 4624220)
  SEEDREAM_MAX_IMAGES  10                reference images per edit (5.0 pro/flash)
  UPSTREAM_TIMEOUT     300               seconds to wait for one image
  UPSTREAM_RETRIES     1                 extra tries after overload/5xx/connect errors
  SEEDREAM_PORT        8787
  SEEDREAM_HOST        127.0.0.1         0.0.0.0 inside a container (see above)
"""

import asyncio
import base64
import io
import json
import logging
import math
import os
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("seedream")

DEFAULT_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"
DEFAULT_MODEL = "dola-seedream-5-0-pro-260628"

# Values in Open WebUI's fields that mean "use the proxy's own setting".
# "dall-e-2" is what Open WebUI sends when its Model field is empty.
KEY_PLACEHOLDERS = {"", "default", "none", "null"}
MODEL_PLACEHOLDERS = {"", "default", "dall-e-2"}

# Seedream input limits (BytePlus image generation API docs).
SEEDREAM_FORMATS = {"jpeg", "png", "webp", "bmp", "tiff", "gif", "heic", "heif"}
INPUT_MAX_PIXELS = 36_000_000
# Inputs above these are shrunk before upload: the output is at most ~4 MP
# anyway, and a smaller upload is faster and stays far below the 30 MB limit.
REENCODE_ABOVE_BYTES = 10 * 1024 * 1024
REENCODE_TARGET_PIXELS = 4096 * 4096

# Pillow refuses images over ~179 MP by default ("decompression bomb" guard);
# 200 MP phone photos are real, and the only client is Open WebUI on localhost.
Image.MAX_IMAGE_PIXELS = 250_000_000

SAFE_RETRY_PIXELS = 1024 * 1024  # the 1K preset's pixel count
RETRY_STATUSES = {500, 502, 503}
RETRY_429_CODES = {"ServerOverloaded", "RequestBurstTooFast"}

SIZE_RE = re.compile(r"^\s*(\d{1,5})\s*[xX]\s*(\d{1,5})\s*$")
PRESET_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[kK]\s*$")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _clean_secret(value: str | None) -> str:
    """Strip whitespace; treat an unresolved Runpod template ("{{ ... }}") as empty."""
    value = (value or "").strip()
    return "" if value.startswith("{{") or value.lower() in KEY_PLACEHOLDERS else value


def _int(env, name: str, default: int) -> int:
    try:
        return int(env.get(name, "") or default)
    except ValueError:
        log.warning("%s=%r is not a number, using %d", name, env.get(name), default)
        return default


def _float(env, name: str, default: float) -> float:
    try:
        return float(env.get(name, "") or default)
    except ValueError:
        log.warning("%s=%r is not a number, using %s", name, env.get(name), default)
        return default


@dataclass(frozen=True)
class Settings:
    base_url: str = DEFAULT_BASE_URL
    pod_key: str = ""
    key_hosts: tuple[str, ...] = (".bytepluses.com",)
    model: str = DEFAULT_MODEL
    watermark: bool = False
    gen_default_size: str = "1K"
    edit_size_mode: str = "original"
    tier_2k_above: int = 2048 * 2048
    min_pixels: int = 1280 * 720
    max_pixels: int = 2048 * 2048
    max_images: int = 10
    timeout: float = 300.0
    retries: int = 1
    retry_delay: float = 2.0
    llama_key: str = ""

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        mode = (env.get("EDIT_SIZE_MODE") or "original").strip().lower()
        if mode == "exact":  # name used by the earlier standalone proxy
            mode = "original"
        if mode not in ("original", "tier", "requested"):
            log.warning("EDIT_SIZE_MODE=%r unknown, using 'original'", mode)
            mode = "original"
        hosts = tuple(h.strip().lower() for h in (env.get("ARK_KEY_HOSTS") or ".bytepluses.com").split(",") if h.strip())
        return cls(
            base_url=(env.get("ARK_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
            pod_key=_clean_secret(env.get("ARK_API_KEY")) or _clean_secret(env.get("RUNPOD_SECRET_ARK_API_KEY")),
            key_hosts=hosts,
            model=(env.get("SEEDREAM_MODEL") or DEFAULT_MODEL).strip(),
            watermark=(env.get("SEEDREAM_WATERMARK") or "false").strip().lower() == "true",
            gen_default_size=(env.get("GEN_DEFAULT_SIZE") or "1K").strip(),
            edit_size_mode=mode,
            tier_2k_above=_int(env, "EDIT_2K_ABOVE_PIXELS", 2048 * 2048),
            min_pixels=_int(env, "SEEDREAM_MIN_PIXELS", 1280 * 720),
            max_pixels=_int(env, "SEEDREAM_MAX_PIXELS", 2048 * 2048),
            max_images=_int(env, "SEEDREAM_MAX_IMAGES", 10),
            timeout=_float(env, "UPSTREAM_TIMEOUT", 300.0),
            retries=max(0, _int(env, "UPSTREAM_RETRIES", 1)),
            llama_key=(env.get("LLAMA_API_KEY") or "").strip(),
        )


class ProxyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": "SeedreamProxyError", "message": message, "type": "proxy_error"}},
                        status_code=status)


# --------------------------------------------------------------------------
# Upstream, key and model
# --------------------------------------------------------------------------
def resolve_upstream(prefix: str, s: Settings) -> str:
    """'v1' or '' -> ARK_BASE_URL; 'https://host/path' -> that URL."""
    p = prefix.strip("/")
    if p in ("", "v1"):
        return s.base_url
    m = re.match(r"^(https?):/+(.+)$", p)  # tolerate "https:/host" if a client merged the slashes
    if not m:
        raise ProxyError(404, f"unknown path /{p}: use http://127.0.0.1:<port>/v1 or "
                              f"http://127.0.0.1:<port>/https://<host>/<path>")
    if m.group(1) != "https":
        raise ProxyError(400, "the upstream URL must start with https://")
    url = "https://" + m.group(2).rstrip("/")
    if not urlsplit(url).hostname:
        raise ProxyError(400, f"no host in upstream URL {url!r}")
    return url


def host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    host = host.lower()
    for pat in patterns:
        if pat.startswith("."):
            if host == pat[1:] or host.endswith(pat):
                return True
        elif host == pat:
            return True
    return False


def resolve_key(authorization: str, upstream: str, s: Settings) -> tuple[str, str]:
    """Return (key, source) where source is 'openwebui' or 'pod'."""
    typed = (authorization or "").strip()
    # An empty key arrives as "Bearer " and HTTP strips the trailing space: "Bearer".
    m = re.match(r"(?i)^bearer(?:\s+(.*))?$", typed)
    if m:
        typed = (m.group(1) or "").strip()
    if typed.lower() not in KEY_PLACEHOLDERS and not typed.startswith("{{") and typed != s.llama_key:
        return typed, "openwebui"
    host = urlsplit(upstream).hostname or ""
    if s.pod_key and (upstream == s.base_url or host_allowed(host, s.key_hosts)):
        return s.pod_key, "pod"
    if s.pod_key:
        raise ProxyError(401, f"ARK_API_KEY is not sent to {host} (not ARK_BASE_URL and not in ARK_KEY_HOSTS); "
                              f"type that service's key in Open WebUI's API Key field")
    raise ProxyError(401, "no API key: type one in Open WebUI's API Key field or set ARK_API_KEY on the Pod")


def resolve_model(sent, s: Settings) -> str:
    m = sent.strip() if isinstance(sent, str) else ""
    return s.model if m.lower() in MODEL_PLACEHOLDERS else m


# --------------------------------------------------------------------------
# Sizes
# --------------------------------------------------------------------------
def fit_size(width: int, height: int, min_px: int, max_px: int) -> tuple[int, int]:
    """Return (width, height) unchanged when the pixel count is inside [min_px, max_px];
    otherwise scale it into the range keeping the aspect ratio. No other rounding
    (whole pixels only): any alignment Seedream wants is left to Seedream."""
    if min_px <= width * height <= max_px:
        return width, height
    target = min(max(width * height, min_px), max_px)
    scale = math.sqrt(target / (width * height))
    fw, fh = width * scale, height * scale
    w, h = round(fw), round(fh)
    if w * h < min_px:  # whole-pixel rounding pushed it under the minimum
        w, h = math.ceil(fw), math.ceil(fh)
    elif w * h > max_px:  # ... or over the maximum
        w, h = math.floor(fw), math.floor(fh)
    return max(1, w), max(1, h)


def parse_wxh(size: str | None) -> tuple[int, int] | None:
    m = SIZE_RE.match(size or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if w > 0 and h > 0 else None


def requested_size(value, s: Settings) -> str | None:
    """Normalize a size from Open WebUI. None = nothing usable was sent."""
    if not isinstance(value, str) or not value.strip() or value.strip().lower() == "auto":
        return None
    p = PRESET_RE.match(value)
    if p:
        return f"{p.group(1)}K"
    wh = parse_wxh(value)
    if not wh:
        log.warning("ignoring size %r (want WIDTHxHEIGHT or a preset like 2K)", value)
        return None
    w, h = wh
    if s.min_pixels <= w * h <= s.max_pixels:
        return f"{w}x{h}"
    fw, fh = fit_size(w, h, s.min_pixels, s.max_pixels)
    return f"{fw}x{fh}"


def edit_size(sent: str | None, first_dims: tuple[int, int] | None, s: Settings) -> str | None:
    """None = let Seedream choose (only when the first image's size is unknown)."""
    if s.edit_size_mode == "requested":
        size = requested_size(sent, s)
        if size:
            return size
    if first_dims is None:
        return None
    w, h = first_dims
    if s.edit_size_mode == "tier":
        return "2K" if w * h > s.tier_2k_above else "1K"
    fw, fh = fit_size(w, h, s.min_pixels, s.max_pixels)
    return f"{fw}x{fh}"


# --------------------------------------------------------------------------
# Input images
# --------------------------------------------------------------------------
def _encode(img: Image.Image) -> tuple[bytes, str]:
    out = io.BytesIO()
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img.convert("RGBA").save(out, format="PNG")
        return out.getvalue(), "png"
    img.convert("RGB").save(out, format="JPEG", quality=95)
    return out.getvalue(), "jpeg"


def prepare_image(raw: bytes, content_type: str | None) -> tuple[str, tuple[int, int] | None, str]:
    """Return (data URI, (width, height) or None, note for the log)."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        # Pillow cannot read HEIC/HEIF without a plugin, but Seedream can.
        sub = ctype.split("/")[-1]
        if sub in ("heic", "heif"):
            return f"data:image/{sub};base64,{base64.b64encode(raw).decode()}", None, sub + " passed through"
        raise ProxyError(400, f"cannot read an input image ({ctype or 'unknown type'}): {e}")

    with img:
        fmt = (img.format or "").lower()
        orientation = img.getexif().get(0x0112, 1)
        pixels = img.width * img.height
        reasons = []
        if orientation not in (1, None):
            reasons.append("rotated by EXIF")
        if fmt not in SEEDREAM_FORMATS:
            reasons.append(f"{fmt or 'unknown'} converted")
        if pixels > INPUT_MAX_PIXELS or len(raw) > REENCODE_ABOVE_BYTES:
            reasons.append("shrunk")
        if not reasons:
            return f"data:image/{fmt};base64,{base64.b64encode(raw).decode()}", img.size, ""

        work = ImageOps.exif_transpose(img) if orientation not in (1, None) else img
        if "shrunk" in reasons:
            scale = math.sqrt(REENCODE_TARGET_PIXELS / (work.width * work.height))
            if scale < 1:
                work = work.resize((max(1, int(work.width * scale)), max(1, int(work.height * scale))),
                                   Image.Resampling.LANCZOS)
        data, out_fmt = _encode(work)
        return f"data:image/{out_fmt};base64,{base64.b64encode(data).decode()}", work.size, ", ".join(reasons)


# --------------------------------------------------------------------------
# Upstream call
# --------------------------------------------------------------------------
def upstream_error(r: httpx.Response) -> tuple[str, str]:
    """(code, message) from a Seedream error body, best effort."""
    try:
        err = r.json().get("error") or {}
        return str(err.get("code") or ""), str(err.get("message") or "")[:500]
    except (ValueError, AttributeError):
        return "", r.text[:500]


def _retry_delay(r: httpx.Response | None, attempt: int, s: Settings) -> float:
    if r is not None:
        try:
            return min(30.0, max(0.0, float(r.headers.get("retry-after", ""))))
        except ValueError:
            pass
    return s.retry_delay * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)


def _retryable(r: httpx.Response) -> bool:
    if r.status_code in RETRY_STATUSES:
        return True
    if r.status_code == 429:
        return upstream_error(r)[0] in RETRY_429_CODES or "retry-after" in r.headers
    return False


async def post_upstream(client: httpx.AsyncClient, url: str, body: dict, key: str,
                        s: Settings, rid: str) -> httpx.Response:
    """POST with retries on overload, 500/502/503 and connection failures only.
    Never retries after a timeout: the image may still be generated and billed."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    attempt = 0
    while True:
        try:
            r = await client.post(url, json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            if attempt < s.retries:
                attempt += 1
                log.warning("[%s] cannot connect (%s), retry %d/%d", rid, e, attempt, s.retries)
                await asyncio.sleep(_retry_delay(None, attempt, s))
                continue
            raise ProxyError(502, f"cannot reach {urlsplit(url).hostname}: {e}")
        except httpx.TimeoutException:
            raise ProxyError(504, f"no answer within {s.timeout:.0f} s (UPSTREAM_TIMEOUT); "
                                  f"not retried, so the image is not paid for twice")
        except httpx.HTTPError as e:
            raise ProxyError(502, f"connection to {urlsplit(url).hostname} failed: {e}")
        if attempt < s.retries and _retryable(r):
            attempt += 1
            code, msg = upstream_error(r)
            log.warning("[%s] upstream %d %s, retry %d/%d", rid, r.status_code, code or msg[:80], attempt, s.retries)
            await asyncio.sleep(_retry_delay(r, attempt, s))
            continue
        return r


def _is_size_error(r: httpx.Response) -> bool:
    code, msg = upstream_error(r)
    return r.status_code == 400 and code.startswith("InvalidParameter") and "size" in msg.lower()


async def run(request: Request, rid: str, op: str, upstream: str, key_source: str, key: str,
              body: dict, ref_dims: tuple[int, int] | None, note: str) -> Response:
    s: Settings = request.app.state.settings
    client: httpx.AsyncClient = request.app.state.http
    url = f"{upstream}/images/generations"
    log.info("[%s] %s model=%s size=%s %s upstream=%s key=%s",
             rid, op, body.get("model"), body.get("size", "(Seedream default)"), note,
             urlsplit(upstream).hostname, key_source)
    t0 = time.monotonic()
    r = await post_upstream(client, url, body, key, s, rid)

    # A WxH under the 1K pixel count rejected as a bad size: retry once at 1K pixels.
    wh = parse_wxh(body.get("size"))
    if wh and wh[0] * wh[1] < SAFE_RETRY_PIXELS and _is_size_error(r):
        w, h = fit_size(wh[0], wh[1], SAFE_RETRY_PIXELS, max(s.max_pixels, SAFE_RETRY_PIXELS))
        log.warning("[%s] size %s rejected (%s), retrying at %dx%d", rid, body["size"], upstream_error(r)[1], w, h)
        body["size"] = f"{w}x{h}"
        r = await post_upstream(client, url, body, key, s, rid)

    elapsed = time.monotonic() - t0
    if r.status_code >= 400:
        code, msg = upstream_error(r)
        log.error("[%s] -> %d in %.1fs %s: %s", rid, r.status_code, elapsed, code or "error", msg)
    else:
        sizes = []
        try:
            for item in r.json().get("data") or []:
                out = str(item.get("size") or "")
                owh = parse_wxh(out)
                if owh and ref_dims:
                    drift = abs((owh[0] / owh[1]) / (ref_dims[0] / ref_dims[1]) - 1) * 100
                    sizes.append(f"{out} (ratio off {drift:.1f}%)")
                elif out:
                    sizes.append(out)
        except (ValueError, AttributeError):
            pass
        log.info("[%s] -> %d in %.1fs result=%s", rid, r.status_code, elapsed, ", ".join(sizes) or "?")

    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")
    return error_response(r.status_code if r.status_code >= 400 else 502,
                          f"upstream returned {ctype or 'no content type'}: {r.text[:300]}")


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    s = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(s.timeout, connect=20.0), transport=transport)
        log.info("ready: upstream=%s pod_key=%s model=%s generate=%s edit=%s watermark=%s",
                 s.base_url, "set" if s.pod_key else "not set", s.model, s.gen_default_size,
                 s.edit_size_mode, s.watermark)
        try:
            yield
        finally:
            await app.state.http.aclose()

    app = FastAPI(title="seedream-proxy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = s

    @app.exception_handler(ProxyError)
    async def _proxy_error(request: Request, exc: ProxyError):
        log.error("[%s] %d %s", getattr(request.state, "rid", "-"), exc.status, exc.message)
        return error_response(exc.status, exc.message)

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "default_upstream": s.base_url,
            "pod_key_set": bool(s.pod_key),
            "pod_key_hosts": list(s.key_hosts),
            "default_model": s.model,
            "generate_default_size": s.gen_default_size,
            "edit_size_mode": s.edit_size_mode,
            "watermark": s.watermark,
        }

    async def handle_generations(request: Request, prefix: str):
        rid = request.state.rid = uuid.uuid4().hex[:8]
        upstream = resolve_upstream(prefix, s)
        key, key_source = resolve_key(request.headers.get("authorization", ""), upstream, s)
        try:
            incoming = json.loads(await request.body())
        except ValueError:
            raise ProxyError(400, "request body is not JSON")
        if not isinstance(incoming, dict) or not str(incoming.get("prompt") or "").strip():
            raise ProxyError(400, "missing prompt")

        body = dict(incoming)  # keeps Open WebUI's "Additional parameters"
        for k in ("n", "stream"):  # Seedream 5.0 pro returns one image; streaming breaks the JSON reply
            body.pop(k, None)
        sent = incoming.get("size")
        body["model"] = resolve_model(incoming.get("model"), s)
        body["size"] = requested_size(sent, s) or s.gen_default_size
        body["response_format"] = "b64_json"
        body.setdefault("watermark", s.watermark)
        return await run(request, rid, "generate", upstream, key_source, key, body,
                         parse_wxh(body["size"]), f"(requested {sent or 'none'})")

    async def handle_edits(request: Request, prefix: str):
        rid = request.state.rid = uuid.uuid4().hex[:8]
        upstream = resolve_upstream(prefix, s)
        key, key_source = resolve_key(request.headers.get("authorization", ""), upstream, s)
        form = await request.form()
        # Open WebUI sends one image as "image" and several as "image[]".
        uploads = [u for u in form.getlist("image") + form.getlist("image[]") if hasattr(u, "read")]
        prompt = str(form.get("prompt") or "").strip()
        if not uploads:
            raise ProxyError(400, "no image in request")
        if not prompt:
            raise ProxyError(400, "missing prompt")
        if len(uploads) > s.max_images:
            raise ProxyError(400, f"{len(uploads)} images sent, the limit is {s.max_images} (SEEDREAM_MAX_IMAGES)")

        uris, notes, first_dims = [], [], None
        for i, up in enumerate(uploads):
            uri, dims, note = prepare_image(await up.read(), up.content_type)
            uris.append(uri)
            if i == 0:
                first_dims = dims
            if note:
                notes.append(f"#{i + 1} {note}")

        sent = form.get("size")
        size = edit_size(sent if isinstance(sent, str) else None, first_dims, s)
        body = {
            "model": resolve_model(form.get("model") if isinstance(form.get("model"), str) else None, s),
            "prompt": prompt,
            "image": uris[0] if len(uris) == 1 else uris,
            "response_format": "b64_json",
            "watermark": s.watermark,
        }
        if size:
            body["size"] = size
        dims_txt = f"{first_dims[0]}x{first_dims[1]}" if first_dims else "unknown"
        note = (f"(images={len(uris)} input={dims_txt} mode={s.edit_size_mode} requested={sent or 'none'}"
                + (f"; {'; '.join(notes)}" if notes else "") + ")")
        return await run(request, rid, "edit", upstream, key_source, key, body, first_dims, note)

    # "/{prefix:path}/images/..." matches /v1/images/... and /https://host/path/images/...
    @app.post("/{prefix:path}/images/generations")
    async def generations(request: Request, prefix: str):
        return await handle_generations(request, prefix)

    @app.post("/images/generations")
    async def generations_no_prefix(request: Request):
        return await handle_generations(request, "")

    @app.post("/{prefix:path}/images/edits")
    async def edits(request: Request, prefix: str):
        return await handle_edits(request, prefix)

    @app.post("/images/edits")
    async def edits_no_prefix(request: Request):
        return await handle_edits(request, "")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # our own lines already log every call
    uvicorn.run(app, host=os.getenv("SEEDREAM_HOST") or "127.0.0.1",
                port=int(os.getenv("SEEDREAM_PORT") or 8787), access_log=False, log_level="info")
