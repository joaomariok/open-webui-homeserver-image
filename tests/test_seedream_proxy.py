"""Tests for seedream/seedream_proxy.py. No network: BytePlus is faked with httpx.MockTransport.

Run:  python -m pytest -q tests
"""

import base64
import io
import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "seedream"))
import seedream_proxy as sp  # noqa: E402

POD_KEY = "pod-secret-key"
LLAMA_KEY = "llama-key"
BASE = sp.Settings(pod_key=POD_KEY, llama_key=LLAMA_KEY, retry_delay=0.0)
OK = {"data": [{"b64_json": "AAAA", "size": "2048x2048"}], "usage": {"generated_images": 1}}


class Upstream:
    """Fake Seedream: records requests, answers with queued responses (last one repeats)."""

    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses) or [httpx.Response(200, json=OK)]

    def __call__(self, request: httpx.Request):
        self.calls.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def body(self):
        return json.loads(self.calls[-1].content)

    @property
    def auth(self):
        return self.calls[-1].headers.get("authorization")


def client(upstream, settings=BASE):
    return TestClient(sp.create_app(settings, httpx.MockTransport(upstream)))


def png(w, h, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), (10, 20, 30, 128) if mode == "RGBA" else (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_with_orientation(w, h, orientation):
    img = Image.new("RGB", (w, h), (200, 10, 10))
    exif = img.getexif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def gen(c, json_body, path="/v1/images/generations", key="default"):
    return c.post(path, json=json_body, headers={"Authorization": f"Bearer {key}"})


def edit(c, files, data=None, path="/v1/images/edits", key="default"):
    return c.post(path, files=files, data={"prompt": "make it blue", "model": "default", **(data or {})},
                  headers={"Authorization": f"Bearer {key}"})


# ---------------------------------------------------------------- settings
def test_settings_from_env():
    s = sp.Settings.from_env({
        "ARK_API_KEY": "  {{ RUNPOD_SECRET_ark }} ",  # unresolved template -> ignored
        "RUNPOD_SECRET_ARK_API_KEY": " real-key\n",
        "EDIT_SIZE_MODE": "EXACT",  # old name for "original"
        "SEEDREAM_MIN_PIXELS": "abc",  # bad number -> default
        "ARK_BASE_URL": "https://ark.eu-west.bytepluses.com/api/v3/",
        "ARK_KEY_HOSTS": " .bytepluses.com, my.host ",
    })
    assert s.pod_key == "real-key"
    assert s.edit_size_mode == "original"
    assert s.min_pixels == 921600
    assert s.base_url == "https://ark.eu-west.bytepluses.com/api/v3"
    assert s.key_hosts == (".bytepluses.com", "my.host")
    assert s.model == "dola-seedream-5-0-pro-260628"
    assert s.gen_default_size == "1K" and s.watermark is False


def test_health_never_shows_key():
    with client(Upstream()) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["pod_key_set"] is True
    assert POD_KEY not in r.text


# ---------------------------------------------------------------- generate
def test_generate_defaults():
    up = Upstream()
    with client(up) as c:
        r = gen(c, {"model": "default", "prompt": "a cat", "n": 1, "response_format": "b64_json",
                    "output_format": "png"})
    assert r.status_code == 200 and r.json() == OK
    b = up.body
    assert str(up.calls[0].url) == "https://ark.ap-southeast.bytepluses.com/api/v3/images/generations"
    assert up.auth == f"Bearer {POD_KEY}"
    assert b["model"] == "dola-seedream-5-0-pro-260628"
    assert b["size"] == "1K"  # no size sent -> GEN_DEFAULT_SIZE
    assert "n" not in b
    assert b["watermark"] is False and b["response_format"] == "b64_json"
    assert b["output_format"] == "png"  # Open WebUI "Additional parameters" are kept


@pytest.mark.parametrize("sent,expected", [
    ("512x512", "960x960"),      # Open WebUI's factory default, under the minimum
    ("2048x2048", "2048x2048"),  # in range: unchanged
    ("1536x1024", "1536x1024"),
    ("4096x4096", "2048x2048"),  # over the maximum
    ("1.5k", "1.5K"),            # preset from a tool call
    ("auto", "1K"),
    ("huge", "1K"),
])
def test_generate_sizes(sent, expected):
    up = Upstream()
    with client(up) as c:
        gen(c, {"prompt": "x", "size": sent})
    assert up.body["size"] == expected


def test_generate_explicit_watermark_param_wins():
    up = Upstream()
    with client(up) as c:
        gen(c, {"prompt": "x", "watermark": True})
    assert up.body["watermark"] is True


@pytest.mark.parametrize("sent,expected", [
    ("dall-e-2", "dola-seedream-5-0-pro-260628"),  # Open WebUI's value when the field is empty
    ("", "dola-seedream-5-0-pro-260628"),
    ("Default", "dola-seedream-5-0-pro-260628"),
    ("seedream-4-5-251128", "seedream-4-5-251128"),
])
def test_model_resolution(sent, expected):
    up = Upstream()
    with client(up) as c:
        gen(c, {"prompt": "x", "model": sent})
    assert up.body["model"] == expected


def test_default_model_from_settings():
    up = Upstream()
    with client(up, replace(BASE, model="my-model")) as c:
        gen(c, {"prompt": "x"})
    assert up.body["model"] == "my-model"


def test_generate_missing_prompt():
    up = Upstream()
    with client(up) as c:
        r = gen(c, {"model": "x"})
    assert r.status_code == 400 and not up.calls


# ---------------------------------------------------------------- keys and upstream URL
@pytest.mark.parametrize("typed,expected", [
    ("typed-key", "typed-key"),   # key typed in Open WebUI wins
    ("default", POD_KEY),
    ("", POD_KEY),
    (LLAMA_KEY, POD_KEY),         # Open WebUI's own fallback: never forward the llama key
])
def test_key_resolution(typed, expected):
    up = Upstream()
    with client(up) as c:
        gen(c, {"prompt": "x"}, key=typed)
    assert up.auth == f"Bearer {expected}"


def test_no_key_anywhere_is_401():
    up = Upstream()
    with client(up, replace(BASE, pod_key="")) as c:
        r = gen(c, {"prompt": "x"})
    assert r.status_code == 401 and not up.calls


def test_upstream_from_path_bytepluses_gets_pod_key():
    up = Upstream()
    with client(up) as c:
        r = gen(c, {"prompt": "x"}, path="/https://ark.eu-west.bytepluses.com/api/v3/images/generations")
    assert r.status_code == 200
    assert str(up.calls[0].url) == "https://ark.eu-west.bytepluses.com/api/v3/images/generations"
    assert up.auth == f"Bearer {POD_KEY}"


def test_upstream_from_path_other_host_never_gets_pod_key():
    up = Upstream()
    with client(up) as c:
        r = gen(c, {"prompt": "x"}, path="/https://api.other.com/v1/images/generations")
        assert r.status_code == 401 and not up.calls
        r = gen(c, {"prompt": "x"}, path="/https://api.other.com/v1/images/generations", key="other-key")
    assert r.status_code == 200
    assert str(up.calls[0].url) == "https://api.other.com/v1/images/generations"
    assert up.auth == "Bearer other-key"


def test_upstream_path_variants():
    up = Upstream()
    with client(up) as c:
        assert gen(c, {"prompt": "x"}, path="/https:/ark.eu-west.bytepluses.com/api/v3/images/generations").status_code == 200
        assert gen(c, {"prompt": "x"}, path="/images/generations").status_code == 200
        assert gen(c, {"prompt": "x"}, path="/http://ark.eu-west.bytepluses.com/api/v3/images/generations").status_code == 400
        assert gen(c, {"prompt": "x"}, path="/v2/images/generations").status_code == 404
    assert [c.url.host for c in up.calls] == ["ark.eu-west.bytepluses.com", "ark.ap-southeast.bytepluses.com"]


# ---------------------------------------------------------------- edit
def test_edit_single_png_keeps_original_size():
    up = Upstream()
    with client(up) as c:
        r = edit(c, [("image", ("a.png", png(1920, 1080), "image/png"))], {"size": "512x512"})
    assert r.status_code == 200
    b = up.body
    assert str(up.calls[0].url).endswith("/api/v3/images/generations")
    assert isinstance(b["image"], str) and b["image"].startswith("data:image/png;base64,")
    assert b["size"] == "1920x1080"  # original size, untouched; Open WebUI's size ignored
    assert b["model"] == "dola-seedream-5-0-pro-260628"
    assert b["watermark"] is False and b["response_format"] == "b64_json"
    assert b["prompt"] == "make it blue"
    assert up.auth == f"Bearer {POD_KEY}"


@pytest.mark.parametrize("w,h,expected", [
    (640, 480, "1109x832"),     # under the minimum: scaled up, ratio kept
    (4000, 3000, "2364x1773"),  # over the maximum: scaled down, ratio kept
    (1024, 1024, "1024x1024"),
    (1001, 999, "1001x999"),    # in range: sent exactly, no rounding
])
def test_edit_original_size_clamped(w, h, expected):
    up = Upstream()
    with client(up) as c:
        edit(c, [("image", ("a.png", png(w, h), "image/png"))])
    assert up.body["size"] == expected
    ew, eh = (int(v) for v in expected.split("x"))
    assert 921600 <= ew * eh <= 4194304


def test_edit_several_images_size_from_first():
    up = Upstream()
    with client(up) as c:
        edit(c, [("image[]", ("a.png", png(1024, 1024), "image/png")),
                 ("image[]", ("b.png", png(1920, 1080), "image/png"))])
    b = up.body
    assert isinstance(b["image"], list) and len(b["image"]) == 2
    assert b["size"] == "1024x1024"


@pytest.mark.parametrize("w,h,expected", [(3000, 2000, "2K"), (1000, 1000, "1K")])
def test_edit_tier_mode(w, h, expected):
    up = Upstream()
    with client(up, replace(BASE, edit_size_mode="tier")) as c:
        edit(c, [("image", ("a.png", png(w, h), "image/png"))], {"size": "1024x1024"})
    assert up.body["size"] == expected


def test_edit_requested_mode():
    up = Upstream()
    with client(up, replace(BASE, edit_size_mode="requested")) as c:
        edit(c, [("image", ("a.png", png(1920, 1080), "image/png"))], {"size": "1024x1024"})
        assert up.body["size"] == "1024x1024"
        edit(c, [("image", ("a.png", png(1920, 1080), "image/png"))])
        assert up.body["size"] == "1920x1080"


def test_edit_exif_rotation_applied():
    up = Upstream()
    with client(up) as c:
        edit(c, [("image", ("p.jpg", jpeg_with_orientation(400, 200, 6), "image/jpeg"))])
    b = up.body
    assert b["image"].startswith("data:image/jpeg;base64,")
    out = Image.open(io.BytesIO(base64.b64decode(b["image"].split(",", 1)[1])))
    assert out.size == (200, 400)  # rotated to portrait
    w, h = (int(v) for v in b["size"].split("x"))
    assert h > w


def test_edit_unsupported_format_converted():
    buf = io.BytesIO()
    Image.new("RGB", (1024, 1024)).save(buf, format="PPM")
    up = Upstream()
    with client(up) as c:
        r = edit(c, [("image", ("a.ppm", buf.getvalue(), "image/x-portable-pixmap"))])
    assert r.status_code == 200
    assert up.body["image"].startswith("data:image/jpeg;base64,")


def test_edit_transparent_png_stays_png_when_reencoded():
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(buf, format="TGA")  # not a Seedream format
    up = Upstream()
    with client(up) as c:
        edit(c, [("image", ("a.tga", buf.getvalue(), "image/x-tga"))])
    assert up.body["image"].startswith("data:image/png;base64,")


def test_edit_heic_passed_through_without_size():
    up = Upstream()
    with client(up) as c:
        r = edit(c, [("image", ("a.heic", b"\x00\x00\x00\x18ftypheic....", "image/heic"))])
    assert r.status_code == 200
    assert up.body["image"].startswith("data:image/heic;base64,")
    assert "size" not in up.body  # unknown input size: Seedream decides


def test_edit_rejects_garbage_too_many_and_missing():
    up = Upstream()
    with client(up, replace(BASE, max_images=2)) as c:
        assert edit(c, [("image", ("a.png", b"not an image", "image/png"))]).status_code == 400
        three = [("image[]", (f"{i}.png", png(32, 32), "image/png")) for i in range(3)]
        assert edit(c, three).status_code == 400
        assert c.post("/v1/images/edits", data={"prompt": "x"}).status_code == 400
    assert not up.calls


# ---------------------------------------------------------------- errors and retries
def err(status, code, message="boom", headers=None):
    return httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers)


def test_error_passed_through():
    up = Upstream(err(400, "OutputImageSensitiveContentDetected", "blocked"))
    with client(up) as c:
        r = gen(c, {"prompt": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "OutputImageSensitiveContentDetected"
    assert len(up.calls) == 1


def test_retry_on_503_then_ok():
    up = Upstream(err(503, "ServiceUnavailable"), httpx.Response(200, json=OK))
    with client(up) as c:
        r = gen(c, {"prompt": "x"})
    assert r.status_code == 200 and len(up.calls) == 2


@pytest.mark.parametrize("code,calls", [("ServerOverloaded", 2), ("QuotaExceeded", 1),
                                        ("ModelAccountIpmRateLimitExceeded", 1)])
def test_429_retried_only_when_it_can_help(code, calls):
    up = Upstream(err(429, code), httpx.Response(200, json=OK))
    with client(up) as c:
        gen(c, {"prompt": "x"})
    assert len(up.calls) == calls


def test_retries_are_limited():
    up = Upstream(err(500, "InternalServiceError"))
    with client(up, replace(BASE, retries=2)) as c:
        r = gen(c, {"prompt": "x"})
    assert r.status_code == 500 and len(up.calls) == 3


def test_read_timeout_not_retried():
    up = Upstream(httpx.ReadTimeout("slow"))
    with client(up) as c:
        r = gen(c, {"prompt": "x"})
    assert r.status_code == 504 and len(up.calls) == 1


def test_connect_error_retried_then_502():
    up = Upstream(httpx.ConnectError("refused"))
    with client(up) as c:
        r = gen(c, {"prompt": "x"})
    assert r.status_code == 502 and len(up.calls) == 2


def test_small_size_rejected_is_retried_at_1k():
    up = Upstream(err(400, "InvalidParameter", "The parameter `size` specified in the request is not valid"),
                  httpx.Response(200, json=OK))
    with client(up) as c:
        r = gen(c, {"prompt": "x", "size": "960x960"})
    assert r.status_code == 200 and len(up.calls) == 2
    assert json.loads(up.calls[0].content)["size"] == "960x960"
    assert up.body["size"] == "1024x1024"


def test_non_json_upstream_reply_becomes_error():
    up = Upstream(httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"}))
    with client(up) as c:
        r = gen(c, {"prompt": "x"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "SeedreamProxyError"
