# PiTFT Now Playing

Shows BluOS / Spotify Connect cover art on a PiTFT 3.5". Tap left = previous, center = play/pause, right = next. Swipe left = next, swipe right = previous. Backlight turns off after 10 min paused; first tap wakes it.

Runs on Raspberry Pi OS Bullseye, no desktop needed.

## Setup

PiTFT driver (the current Adafruit script is Bookworm-only, so use an older version):

```bash
git clone https://github.com/adafruit/Raspberry-Pi-Installer-Scripts.git
cd Raspberry-Pi-Installer-Scripts && git checkout 9086294
sudo -E env PATH=$PATH python3 adafruit-pitft.py --display=35r --rotation=270 --install-type=console
```

Dependencies, and stop the console drawing over the art (run over SSH):

```bash
sudo apt install python3-pil python3-numpy python3-evdev python3-requests
sudo systemctl mask --now getty@tty1
sudo sed -i 's/$/ vt.global_cursor_default=0/' /boot/cmdline.txt
```

Check touch zones (Ctrl-C to quit). If left/right are swapped, add `--touch-invert`. If top/bottom changes the zone, add `--touch-axis y`.

```bash
sudo python3 ~/pitft-nowplaying/nowplaying.py --calibrate
```

Set `--host` and any touch flags in `nowplaying.service`, then:

```bash
sudo cp ~/pitft-nowplaying/nowplaying.service /etc/systemd/system/
sudo systemctl enable --now nowplaying
```

Logs: `journalctl -u nowplaying -f`

## Restore the console

```bash
sudo systemctl disable --now nowplaying
sudo systemctl unmask getty@tty1
sudo systemctl enable getty@tty1
sudo sed -i 's/ vt.global_cursor_default=0//' /boot/cmdline.txt
sudo reboot
```

Autologin comes back too, if it was on.

## Options

| Option | Default | |
|---|---|---|
| `--host` | `NODE2iWohnzimmer.local` | BluOS player. Find yours with `avahi-browse -rt _musc._tcp` |
| `--fb` | autodetect | Framebuffer, e.g. `/dev/fb1` |
| `--touch-axis` | `x` | Touch axis that runs left to right |
| `--touch-invert` | off | Swap left/right zones |
| `--idle-off` | `600` | Seconds before backlight off (`0` = never) |
| `--calibrate` | | Print tap zones |
| `--png FILE` | | Render a screenshot and exit |
