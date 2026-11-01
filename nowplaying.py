#!/usr/bin/env python3
"""Now-playing kiosk for a PiTFT 3.5".

Shows the artwork of whatever a BluOS player is playing (including Spotify
Connect, which BluOS reports like any other source). Touch: tap left = back,
center = play/pause, right = next; swipe left = next, swipe right = back.
With a Spotify login (--spotify-login), a heart in the bottom-right corner
likes / unlikes the current Spotify track.

Draws straight to the framebuffer, so no X / SDL is needed.
Deps (Bullseye): sudo apt install python3-pil python3-numpy python3-evdev python3-requests
"""
import argparse
import base64
import functools
import glob
import hashlib
import io
import json
import math
import os
import secrets
import signal
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

FONT_CANDIDATES = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
    "regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf"],
}
PLAYING_STATES = ("play", "stream", "connecting")
SWIPE_DISTANCE = 0.25  # fraction of the screen width
SPOTIFY_GREEN = (30, 215, 96)
SPOTIFY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify.json")
SPOTIFY_REDIRECT = "http://127.0.0.1:8888/callback"
SPOTIFY_SCOPES = "user-read-currently-playing user-library-read user-library-modify"


@functools.lru_cache(maxsize=None)
def load_font(style, size):
    for path in FONT_CANDIDATES[style]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


# --- BluOS ------------------------------------------------------------------

class Player:
    def __init__(self, host):
        self.base = "http://%s" % (host if ":" in host else host + ":11000")
        self.actions = {}

    def status(self, etag=None, wait=100):
        """Long-polls /Status: returns as soon as something changes (or after `wait` s)."""
        params = {"timeout": wait, "etag": etag} if etag else {}
        r = requests.get(self.base + "/Status", params=params, timeout=wait + 10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        # Spotify etc. expose their own back/skip URLs; plain /Back and /Skip are the fallback.
        self.actions = {a.get("name"): a.get("url") for a in root.iter("action") if a.get("url")}
        return root.get("etag"), {c.tag: (c.text or "") for c in root}

    def _get(self, path):
        try:
            requests.get(urljoin(self.base, path), timeout=5)
        except requests.RequestException as e:
            print("command failed:", path, e)

    def back(self):
        self._get(self.actions.get("back", "/Back"))

    def toggle(self):
        self._get("/Pause?toggle=1")

    def next(self):
        self._get(self.actions.get("skip", "/Skip"))

    def fetch_art(self, url):
        try:
            r = requests.get(urljoin(self.base, url), timeout=10)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except (requests.RequestException, OSError) as e:
            print("artwork failed:", url, e)
            return None


# --- Spotify (likes only; playback stays with BluOS) -------------------------

class Spotify:
    API = "https://api.spotify.com/v1"
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    def __init__(self, path=SPOTIFY_FILE):
        self.path = path
        self.cfg = json.load(open(path))
        self.token, self.expires = None, 0.0
        self.lock = threading.Lock()

    def _headers(self):
        with self.lock:
            if time.time() > self.expires - 60:
                r = requests.post(self.TOKEN_URL, timeout=10, data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.cfg["refresh_token"],
                    "client_id": self.cfg["client_id"],
                })
                r.raise_for_status()
                t = r.json()
                self.token, self.expires = t["access_token"], time.time() + t["expires_in"]
                if t.get("refresh_token", self.cfg["refresh_token"]) != self.cfg["refresh_token"]:
                    self.cfg["refresh_token"] = t["refresh_token"]  # Spotify rotates these
                    save_private_json(self.path, self.cfg)
            return {"Authorization": "Bearer " + self.token}

    def _call(self, method, path, **params):
        r = requests.request(method, self.API + path, params=params, headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r

    def current_track(self):
        """(uri, name) of what Spotify thinks is playing, or None."""
        r = self._call("GET", "/me/player/currently-playing")
        item = r.json().get("item") if r.status_code == 200 else None
        return (item["uri"], item["name"]) if item else None

    def is_saved(self, uri):
        return self._call("GET", "/me/library/contains", uris=uri).json()[0]

    def set_saved(self, uri, saved):
        self._call("PUT" if saved else "DELETE", "/me/library", uris=uri)


def save_private_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


def spotify_login(client_id):
    """One-time PKCE login; stores a refresh token next to the script."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    print("1. Open this link in any browser and approve:\n")
    print("https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": SPOTIFY_REDIRECT,
        "scope": SPOTIFY_SCOPES, "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    }))
    print("\n2. The browser then fails to load a 127.0.0.1 page. That's expected:")
    url = input("   paste that page's full address here: ").strip()
    query = parse_qs(urlparse(url).query)
    if query.get("state", [""])[0] != state or "code" not in query:
        raise SystemExit("That address doesn't contain a login code: %s" % query.get("error", url))
    r = requests.post(Spotify.TOKEN_URL, timeout=10, data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": SPOTIFY_REDIRECT,
        "client_id": client_id, "code_verifier": verifier,
    })
    r.raise_for_status()
    save_private_json(SPOTIFY_FILE, {"client_id": client_id, "refresh_token": r.json()["refresh_token"]})
    print("Logged in. Saved to %s. Restart the service to show the heart." % SPOTIFY_FILE)


# --- Display ----------------------------------------------------------------

class Framebuffer:
    def __init__(self, dev=None):
        self.dev = dev or self._find()
        sysfs = "/sys/class/graphics/" + os.path.basename(self.dev)
        w, h = open(sysfs + "/virtual_size").read().strip().split(",")
        self.size = (int(w), int(h))
        self.bpp = int(open(sysfs + "/bits_per_pixel").read())
        self.lock = threading.Lock()
        self.frame = Image.new("RGB", self.size, "black")  # last full frame, for overlays
        self.icon, self.icon_until = None, 0.0
        print("framebuffer %s %dx%d @ %dbpp" % (self.dev, self.size[0], self.size[1], self.bpp))

    @staticmethod
    def _find():
        # fbtft drivers (the PiTFT) register as "fb_<chip>", e.g. fb_hx8357d.
        for path in sorted(glob.glob("/sys/class/graphics/fb*")):
            if open(path + "/name").read().startswith("fb_"):
                return "/dev/" + os.path.basename(path)
        return "/dev/fb1" if os.path.exists("/dev/fb1") else "/dev/fb0"

    def show(self, img=None):
        """Shows a new frame (or redraws the last one), with the flashed icon on top while it lasts."""
        with self.lock:
            if img is not None:
                self.frame = img
            out = self.frame
            if self.icon and time.monotonic() < self.icon_until:
                out = feedback(out, self.icon)
            self._write(out)

    def flash(self, icon, img=None, secs=1.5):
        """Shows the icon for a while; with img, the new frame and icon go out in one write."""
        self.icon, self.icon_until = icon, time.monotonic() + secs
        self.show(img)
        threading.Timer(secs + 0.05, self.show).start()

    def _write(self, img):
        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        if self.bpp == 16:
            px = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
            data = px.astype("<u2").tobytes()
        else:  # 32 bpp BGRA
            alpha = np.full(a.shape[:2], 255, dtype=np.uint16)
            data = np.dstack([a[..., 2], a[..., 1], a[..., 0], alpha]).astype(np.uint8).tobytes()
        with open(self.dev, "wb") as f:
            f.write(data)


class Backlight:
    def __init__(self):
        paths = glob.glob("/sys/class/backlight/*")
        self.path = paths[0] if paths else None
        self.on = True

    def set(self, on):
        if on == self.on:
            return
        self.on = on
        if not self.path:
            return
        try:
            value = open(self.path + "/max_brightness").read().strip() if on else "0"
            with open(self.path + "/brightness", "w") as f:
                f.write(value)
        except OSError as e:
            print("backlight:", e)


def prepare_art(art, size):
    """Scales the cover once per track: (sharp square, blurred backdrop)."""
    W, H = size
    side = min(W, H)
    square = art.resize((side, side), Image.LANCZOS)
    # Blurring a thumbnail and scaling it up looks the same and is far cheaper on a Pi.
    small = art.resize((W // 8, H // 8)).filter(ImageFilter.GaussianBlur(2))
    backdrop = ImageEnhance.Brightness(small.resize(size, Image.BILINEAR)).enhance(0.35)
    return square, backdrop


def wrap(draw, text, font, width, max_lines):
    lines, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=font) <= width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def draw_heart(draw, cx, cy, size, filled, color):
    pts = []
    for i in range(48):
        t = 2 * math.pi * i / 48
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * size / 34, cy - y * size / 34))
    if filled:
        draw.polygon(pts, fill=color)
    else:
        draw.line(pts + pts[:1], fill=color, width=3, joint="curve")


def render(size, status, art, message=None, liked=None):
    """liked: True / False draws the like button, None hides it."""
    W, H = size
    img = Image.new("RGB", size, "black")
    draw = ImageDraw.Draw(img)

    if status is None or not status.get("title1"):
        msg = message or ("Can't reach player" if status is None else "Nothing playing")
        font = load_font("regular", 18)
        draw.text((W / 2, H / 2), msg, font=font, fill=(110, 110, 110), anchor="mm")
        return img

    if art is not None:
        square, backdrop = art
        img.paste(backdrop, (0, 0))
        img.paste(square, (0, 0))

    panel_x = min(W, H) + 12
    panel_w = W - panel_x - 12
    if panel_w >= 100:
        y = 16
        for key, style, size_, color, max_lines in (
            ("title1", "bold", 20, (255, 255, 255), 5),
            ("title2", "regular", 16, (210, 210, 210), 3),
            ("title3", "regular", 14, (150, 150, 150), 3),
        ):
            font = load_font(style, size_)
            for line in wrap(draw, status.get(key, ""), font, panel_w, max_lines):
                draw.text((panel_x, y), line, font=font, fill=color)
                y += size_ + 5
            y += 8

    if status.get("state") not in PLAYING_STATES:
        # Pause badge in the bottom-left corner of the text panel.
        x, y = panel_x, H - 44
        draw.rectangle((x, y, x + 8, y + 28), fill=(255, 255, 255))
        draw.rectangle((x + 16, y, x + 24, y + 28), fill=(255, 255, 255))
    if liked is not None:
        draw_heart(draw, W - 34, H - 32, 34, liked, SPOTIFY_GREEN if liked else (255, 255, 255))
    return img


def feedback(frame, icon):
    """Draws a back / next / play / pause / like / unlike icon over the frame."""
    # Only the square around the icon is blended; the whole frame is too slow on a Pi.
    W, H = frame.size
    r = 48
    box = (W // 2 - r, H // 2 - r, W // 2 + r, H // 2 + r)
    layer = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx = cy = r
    draw.ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=(0, 0, 0, 170))
    white = (255, 255, 255, 255)
    if icon == "pause":
        draw.rectangle((cx - 16, cy - 20, cx - 5, cy + 20), fill=white)
        draw.rectangle((cx + 5, cy - 20, cx + 16, cy + 20), fill=white)
    elif icon == "play":
        draw.polygon([(cx - 13, cy - 22), (cx - 13, cy + 22), (cx + 22, cy)], fill=white)
    elif icon in ("like", "unlike"):
        draw_heart(draw, cx, cy + 2, 46, icon == "like", SPOTIFY_GREEN + (255,) if icon == "like" else white)
    else:
        d = 1 if icon == "next" else -1
        for ox in (-18, 2):
            x0 = cx + ox * d
            draw.polygon([(x0, cy - 18), (x0, cy + 18), (x0 + 18 * d, cy)], fill=white)
    out = frame.copy()
    out.paste(Image.alpha_composite(frame.crop(box).convert("RGBA"), layer).convert("RGB"), box[:2])
    return out


# --- Touch ------------------------------------------------------------------

def find_touch_device():
    import evdev
    from evdev import ecodes
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        abs_codes = [code for code, _ in dev.capabilities().get(ecodes.EV_ABS, [])]
        if ecodes.ABS_X in abs_codes and ("stmpe" in dev.name.lower() or "touch" in dev.name.lower()):
            return dev
    raise RuntimeError("no touchscreen found (looked for stmpe/touch input devices)")


def touch_loop(on_gesture):
    """Calls on_gesture(start, end) per touch, with raw (x, y) positions normalized to 0..1."""
    from evdev import ecodes
    dev = find_touch_device()
    print("touchscreen:", dev.path, dev.name)
    ax, ay = dev.absinfo(ecodes.ABS_X), dev.absinfo(ecodes.ABS_Y)

    def pos():
        return (x - ax.min) / (ax.max - ax.min), (y - ay.min) / (ay.max - ay.min)

    x = y = None
    touching, start = False, None
    for ev in dev.read_loop():
        if ev.type == ecodes.EV_ABS and ev.code == ecodes.ABS_X:
            x = ev.value
        elif ev.type == ecodes.EV_ABS and ev.code == ecodes.ABS_Y:
            y = ev.value
        elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
            touching = bool(ev.value)
            if not touching and start and x is not None:
                on_gesture(start, pos())
            start = None
        elif ev.type == ecodes.EV_SYN and touching and start is None and x is not None:
            start = pos()  # first complete report of this touch


def to_screen(p, args):
    """Raw normalized touch position -> (left..right, top..bottom), both 0..1."""
    h, v = (p[0], p[1]) if args.touch_axis == "x" else (p[1], p[0])
    return (1 - h if args.touch_invert else h), (1 - v if args.touch_invert_v else v)


def gesture(start, end, like_button=False):
    """start / end in screen coordinates (see to_screen)."""
    (h0, v0), (h1, _) = start, end
    if h1 - h0 < -SWIPE_DISTANCE:
        return "next"  # swipe left
    if h1 - h0 > SWIPE_DISTANCE:
        return "back"  # swipe right
    if like_button and h0 > 0.8 and v0 > 0.7:
        return "like"
    return "back" if h0 < 1 / 3 else "next" if h0 > 2 / 3 else "toggle"


# --- Main -------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="NODE2iWohnzimmer.local", help="BluOS player host[:port]")
    p.add_argument("--fb", help="framebuffer device (default: autodetect the PiTFT)")
    p.add_argument("--touch-axis", choices=("x", "y"), default="x",
                   help="raw touch axis that runs left-to-right on screen")
    p.add_argument("--touch-invert", action="store_true", help="flip left/right")
    p.add_argument("--touch-invert-v", action="store_true", help="flip top/bottom")
    p.add_argument("--idle-off", type=int, default=600,
                   help="seconds paused/stopped before the backlight turns off (0 = never)")
    p.add_argument("--calibrate", action="store_true", help="print touch positions and gestures, then exit with Ctrl-C")
    p.add_argument("--spotify-login", metavar="CLIENT_ID", help="log in to Spotify once, for the like button")
    p.add_argument("--png", help="render the current status to this PNG once and exit (for testing)")
    args = p.parse_args()

    if args.spotify_login:
        spotify_login(args.spotify_login)
        return

    player = Player(args.host)
    spotify = Spotify() if os.path.exists(SPOTIFY_FILE) else None

    if args.calibrate:
        def show_gesture(start, end):
            s, e = to_screen(start, args), to_screen(end, args)
            print("left-right %.2f top-bottom %.2f -> %.2f %.2f: %s"
                  % (s + e + (gesture(s, e, like_button=True),)))
        touch_loop(show_gesture)
        return

    if args.png:
        _, status = player.status()
        art = player.fetch_art(status["image"]) if status.get("image") else None
        art = prepare_art(art, (480, 320)) if art else None
        render((480, 320), status, art, liked=False if spotify else None).save(args.png)
        return

    fb = Framebuffer(args.fb)
    fb.show(render(fb.size, {"title1": ""}, None, "Connecting…"))
    backlight = Backlight()

    def on_stop(signum, frame):
        # The framebuffer keeps its last image, so clear it rather than leave a stale cover.
        fb.show(Image.new("RGB", fb.size, "black"))
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    view = {"status": None, "art": None}
    like = {"track": None, "uri": None, "liked": None}  # liked None = no button

    def current_frame():
        return render(fb.size, view["status"], view["art"], liked=like["liked"])

    def lookup_like(track):
        # Spotify can lag behind BluOS by a moment, so retry until the titles match.
        # BluOS cuts long titles short, hence startswith.
        for _ in range(5):
            try:
                current = spotify.current_track()
                if current and track[0] and current[1].startswith(track[0].strip()):
                    liked = spotify.is_saved(current[0])
                    if like["track"] == track:
                        like.update(uri=current[0], liked=liked)
                        fb.show(current_frame())
                    return
            except (requests.RequestException, ValueError, KeyError) as e:
                print("spotify:", e)
                return
            time.sleep(1.5)
        print("spotify: couldn't match", track[0])

    def toggle_like():
        uri, liked = like["uri"], like["liked"]
        like["liked"] = not liked
        fb.flash("like" if not liked else "unlike", current_frame())
        try:
            spotify.set_saved(uri, not liked)
        except requests.RequestException as e:
            print("spotify:", e)
            like["liked"] = liked
            fb.show(current_frame())

    last_touch = [0.0]
    touch_icon = [None]
    playing = [False]

    def on_gesture(start, end):
        now = time.monotonic()
        if now - last_touch[0] < 0.4:  # the resistive panel bounces
            return
        last_touch[0] = now
        if not backlight.on:  # first touch on a dark screen only wakes it
            backlight.set(True)
            return
        s, e = to_screen(start, args), to_screen(end, args)
        action = gesture(s, e, like_button=like["liked"] is not None)
        print("touch %.2f,%.2f -> %.2f,%.2f: %s" % (s + e + (action,)))
        if action == "like":
            toggle_like()
            return
        touch_icon[0] = action if action != "toggle" else "pause" if playing[0] else "play"
        fb.flash(touch_icon[0])
        getattr(player, action)()

    try:
        threading.Thread(target=touch_loop, args=(on_gesture,), daemon=True).start()
    except Exception as e:
        print("touch disabled:", e)

    etag, art_url = None, None
    track, prev_track = None, None
    last_playing = time.monotonic()
    while True:
        try:
            etag, status = player.status(etag)
        except (requests.RequestException, ET.ParseError) as e:
            print("status failed:", e)
            fb.show(render(fb.size, None, None))
            etag = None
            time.sleep(5)
            continue

        print("%s: %s - %s" % (status.get("state"), status.get("title2"), status.get("title1")))
        # Changes made elsewhere (BluOS remote, app) get the same icons as touches.
        # BluOS doesn't say which way a skip went, so returning to the previous track counts as back.
        now_track = (status.get("title1"), status.get("title2"), status.get("title3"))
        now_playing = status.get("state") in PLAYING_STATES
        icon = None
        if track is not None and now_track != track:
            icon = "back" if now_track == prev_track else "next"
            prev_track = track
        elif track is not None and now_playing != playing[0]:
            icon = "play" if now_playing else "pause"
        if icon and time.monotonic() - last_touch[0] < 3:
            icon = touch_icon[0]  # a touch caused it, so we know the real direction
        track = now_track
        playing[0] = now_playing

        if now_track != like["track"]:
            like.update(track=now_track, uri=None, liked=None)
            if spotify and status.get("service") == "Spotify":
                threading.Thread(target=lookup_like, args=(now_track,), daemon=True).start()

        view["status"] = status
        if status.get("image") != art_url:
            art_url = status.get("image")
            view["art"] = None  # new titles now, cover once it's downloaded
            fb.flash(icon, current_frame()) if icon else fb.show(current_frame())
            icon = None
            art = player.fetch_art(art_url) if art_url else None
            view["art"] = prepare_art(art, fb.size) if art else None
        fb.flash(icon, current_frame()) if icon else fb.show(current_frame())

        if playing[0]:
            last_playing = time.monotonic()
            backlight.set(True)
        elif args.idle_off and time.monotonic() - last_playing > args.idle_off:
            backlight.set(False)


if __name__ == "__main__":
    main()
