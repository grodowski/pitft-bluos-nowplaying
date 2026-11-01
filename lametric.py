#!/usr/bin/env python3
"""BluOS now-playing on a LaMetric TIME, drawn as raw pixels over the Streaming API (LMSP).

Left 8x8: the cover, area-averaged down to 8x8. Right 29x8: a dim greyscale wash of the
cover stretched to fit, a play/pause/stop icon, and a progress bar on the bottom row.
Redraws the moment BluOS reports a change; the bar advances locally between reports.
After --idle seconds paused/stopped it ends the stream, so the clock goes back to its
normal apps; it takes over again when playback resumes.

Also listens for button webhooks (GET or POST): /btn/next, /btn/prev, /btn/toggle.

Deps: sudo apt install python3-pil python3-requests (nowplaying.py's imports also need python3-numpy)
"""
import argparse
import signal
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import urllib3
from PIL import Image, ImageOps

from nowplaying import PLAYING_STATES, Player

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # the clock's cert is self-signed

HEALTH_CHECK = 30  # seconds between checks that our stream is still the active one


class Clock:
    def __init__(self, host, key):
        self.host = host
        self.base = "https://%s:4343/api/v2/device/stream" % host
        self.auth = ("dev", key)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.session, self.port = None, 9999
        self.size = (37, 8)

    def _req(self, method, path="", **kw):
        r = requests.request(method, self.base + path, auth=self.auth, verify=False, timeout=10, **kw)
        r.raise_for_status()
        return r.json()

    def streaming(self):
        """True if the clock is still receiving a stream (ours, or one we should leave alone)."""
        st = self._req("GET")
        size = st.get("canvas", {}).get("pixel", {}).get("size", {})
        self.size = (size.get("width", 37), size.get("height", 8))
        return st.get("status") == "receiving"

    def start(self):
        self.streaming()  # refreshes the canvas size
        body = {"canvas": {"fill_type": "scale", "render_mode": "pixel", "post_process": {"type": "none"}}}
        data = self._req("PUT", "/start", json=body)["success"]["data"]
        self.session, self.port = data["session_id"], data.get("port", 9999)
        print("stream started, session", self.session)

    def stop(self):
        if not self.session:
            return
        self.session = None
        try:
            self._req("PUT", "/stop")
            print("stream stopped")
        except requests.RequestException as e:
            print("stream stop failed:", e)

    def send(self, img):
        data = img.convert("RGB").tobytes()
        header = (b"lmsp" + struct.pack("<H", 1) + bytes.fromhex(self.session)
                  + bytes([0x00, 0x00, 0x01, 0x00])  # RAW, reserved, 1 canvas area, reserved
                  + struct.pack("<HHHHH", 0, 0, img.width, img.height, len(data)))
        self.sock.sendto(header + data, (self.host, self.port))


def white(level):
    v = round(255 * max(0.0, min(1.0, level)))
    return (v, v, v)


# Small state icons, drawn top-right of the right block.
ICONS = {
    "play": ("100", "110", "111", "110", "100"),
    "pause": ("11011", "11011", "11011", "11011", "11011"),
    "stop": ("000", "111", "111", "111", "000"),
    "connecting": ("00000", "00000", "00000", "00000", "10101"),
}
PLAY_FLASH = 3  # seconds the play icon stays after resuming


def blit(frame, glyph, x, y, color):
    for dy, row in enumerate(glyph):
        for dx, bit in enumerate(row):
            if bit == "1":
                frame.putpixel((x + dx, y + dy), color)


def clear(frame, x0, y0, x1, y1):
    """Black out a box (inclusive, clipped), so the icon stays readable over the wash."""
    w, h = frame.size
    for x in range(max(0, x0), min(w, x1 + 1)):
        for y in range(max(0, y0), min(h, y1 + 1)):
            frame.putpixel((x, y), (0, 0, 0))


def make_wash(cover, size):
    """The cover in greyscale, stretched over the right block above the bar (rows 0-5)."""
    w, h = size
    return ImageOps.autocontrast(cover.convert("L")).resize((w - h, h - 2), Image.BOX)


class Look:
    """Brightness (0-1) of each element; argparse overrides these."""
    played, head, track, icon, wash = 0.35, 1.0, 0.08, 0.5, 0.3


def render(size, art, progress, look=Look, wash=None, icon=None):
    w, h = size
    frame = Image.new("RGB", size)  # black
    if art is not None:
        frame.paste(art, (0, 0))
    if wash is not None and look.wash > 0:
        for x in range(wash.width):
            for y in range(wash.height):
                frame.putpixel((h + x, y), white(wash.getpixel((x, y)) / 255 * look.wash))
    if progress is not None:
        n = w - h
        pos = h + min(n - 1, int(n * max(0.0, min(1.0, progress))))  # the pixel being played now
        for x in range(h, w):
            frame.putpixel((x, h - 1), white(look.head if x == pos else look.played if x < pos else look.track))
        frame.putpixel((pos, h - 2), white(look.head))
    # The icon sits on rows 0-4, so row 5 stays clear above the 2 px playhead.
    if icon and look.icon > 0:
        glyph = ICONS[icon]
        x = w - 1 - len(glyph[0])  # one column in from the right edge
        clear(frame, x - 1, 0, w - 1, 5)
        blit(frame, glyph, x, 0, white(look.icon))
    return frame


def ramp(size):
    """Test card: 29 grey levels across the right block, a colour fade on the left."""
    w, h = size
    frame = Image.new("RGB", size)
    for x in range(h, w):
        for y in range(h):
            frame.putpixel((x, y), white((x - h + 1) / (w - h)))
    for x in range(h):
        for y in range(h):
            frame.putpixel((x, y), (255 * (x + 1) // h, 0, 255 * (y + 1) // h))
    return frame


def num(status, key):
    try:
        return float(status[key])
    except (KeyError, ValueError):
        return None


class Playback:
    """Latest BluOS status, with the track position counted locally between updates.

    BluOS only reports whole seconds and we only hear from it on changes, so we anchor on a reported
    position and add wall-clock time, re-anchoring only when BluOS disagrees by more than
    a second (seek, track change) or playback starts / stops.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.status, self.art, self.wash = {}, None, None
        self.anchor, self.anchor_t, self.playing, self.track = None, 0.0, False, None

    def update(self, status, art_changed=False, art=None, wash=None):
        now = time.monotonic()
        with self.lock:
            playing = status.get("state") in PLAYING_STATES
            track = (status.get("title1"), status.get("title2"), status.get("title3"))
            reported = num(status, "secs")
            predicted = self.position(now)
            if (reported is None or predicted is None or playing != self.playing or track != self.track
                    or abs(predicted - reported) > 1.2):
                self.anchor, self.anchor_t = reported, now
            self.status, self.playing, self.track = status, playing, track
            if art_changed:
                self.art, self.wash = art, wash
        self.changed.set()

    def position(self, now):
        if self.anchor is None:
            return None
        pos = self.anchor + (now - self.anchor_t if self.playing else 0)
        total = num(self.status, "totlen")
        return min(pos, total) if total else pos


def poll_bluos(player, playback, size):
    """Background thread: long-polls BluOS, which answers at once on any change."""
    etag, art_url = None, None
    while True:
        try:
            etag, status = player.status(etag, wait=10)
        except (requests.RequestException, ET.ParseError) as e:
            print("BluOS status failed:", e)
            etag = None
            time.sleep(5)
            continue
        if status.get("image") != art_url:
            art_url = status.get("image")
            print("%s: %s - %s" % (status.get("state"), status.get("title2"), status.get("title1")))
            cover = player.fetch_art(art_url) if art_url else None
            h = size[1]
            # BOX averages each block; NEAREST would keep one arbitrary pixel per ~75x75 block.
            art = cover.resize((h, h), Image.BOX) if cover else None
            wash = make_wash(cover, size) if cover else None
            playback.update(status, True, art, wash)
        else:
            playback.update(status)


def serve_buttons(port, player):
    routes = {"/btn/next": player.next, "/btn/prev": player.back, "/btn/toggle": player.toggle}

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            action = routes.get(self.path.split("?")[0].rstrip("/"))
            if not action:
                self.send_error(404)
                return
            self.send_response(204)
            self.end_headers()
            print("button:", self.path)
            action()

        do_GET = do_POST = _handle

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("button webhooks on port", port)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--clock", required=True, help="LaMetric IP")
    p.add_argument("--key", required=True, help="device API key (LaMetric phone app, device settings)")
    p.add_argument("--host", default="NODE2iWohnzimmer.local", help="BluOS player host[:port]")
    p.add_argument("--idle", type=int, default=30,
                   help="seconds paused/stopped before handing the display back to the clock")
    p.add_argument("--port", type=int, default=5000, help="button webhook port (0 = off)")
    p.add_argument("--played", type=float, default=Look.played, help="brightness of the played part of the bar (0-1)")
    p.add_argument("--head", type=float, default=Look.head, help="brightness of the playhead (0-1)")
    p.add_argument("--track", type=float, default=Look.track, help="brightness of the unplayed part (0-1, 0 = off)")
    p.add_argument("--icon", type=float, default=Look.icon,
                   help="brightness of the play/pause/stop icon (0-1, 0 = off)")
    p.add_argument("--wash", type=float, default=Look.wash,
                   help="max brightness of the greyscale cover behind the right block (0-1, 0 = off)")
    p.add_argument("--ramp", action="store_true", help="show a brightness test card for 20 s and exit")
    args = p.parse_args()

    player = Player(args.host)
    clock = Clock(args.clock, args.key)

    if args.ramp:
        clock.start()
        try:
            for _ in range(40):
                clock.send(ramp(clock.size))
                time.sleep(0.5)
        finally:
            clock.stop()
        return

    def on_stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    if args.port:
        serve_buttons(args.port, player)

    playback = Playback()
    threading.Thread(target=poll_bluos, args=(player, playback, clock.size), daemon=True).start()

    last_playing = time.monotonic()
    was_playing, play_since = True, 0.0
    last_check, last_frame, last_sent = 0.0, None, 0.0
    try:
        while True:
            # Wake on a BluOS change, otherwise 4x a second so the timer turns over on time.
            playback.changed.wait(0.25)
            playback.changed.clear()
            now = time.monotonic()
            with playback.lock:
                status, art, wash = playback.status, playback.art, playback.wash
                playing, pos = playback.playing, playback.position(now)
            if not status:
                continue

            state = status.get("state")
            if playing:
                last_playing = now
                if not was_playing:
                    play_since = now
            was_playing = playing
            if not playing and now - last_playing > args.idle:
                clock.stop()
                last_frame = None
                continue
            if state == "connecting":
                icon = "connecting"
            elif playing:
                icon = "play" if now - play_since < PLAY_FLASH else None
            else:
                icon = "stop" if state == "stop" else "pause"

            total = num(status, "totlen")
            frame = render(clock.size, art, pos / total if pos is not None and total else None, args, wash, icon)
            try:
                if clock.session and now - last_check > HEALTH_CHECK:
                    last_check = now
                    if not clock.streaming():
                        print("stream ended on the clock, restarting")
                        clock.session = None
                if not clock.session:
                    clock.start()
                    last_check, last_frame = now, None
                # Send on change, and at least every 2 s so the clock never sees a silent stream.
                if frame.tobytes() != last_frame or now - last_sent > 2:
                    clock.send(frame)
                    last_frame, last_sent = frame.tobytes(), now
            except (requests.RequestException, OSError, KeyError, ValueError) as e:
                print("LaMetric failed:", e)
                clock.session = None
                time.sleep(5)
    finally:
        clock.stop()


if __name__ == "__main__":
    main()
