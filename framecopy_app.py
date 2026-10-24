#!/usr/bin/env python3
"""
FrameCopy app - a "FrameCopy" tab in the SteamVR dashboard that starts and stops
sharing and shows how to connect: the address and the PIN.

Runs as a user service that waits for SteamVR. Sharing lives in this process,
not in SteamVR, so it keeps going if SteamVR quits and stops on its own after
30 minutes without use.
"""

import configparser
import ctypes
import glob
import os
import signal
import subprocess
import sys
import time

import openvr
from PIL import Image, ImageDraw, ImageFont

from framecopy import DEFAULT_DIR, DEFAULT_PORT, IDLE_MINUTES, ShareServer

APP_KEY = "gamesinhand.framecopy"
INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "framecopy")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")
SOURCE_DIR_FILE = os.path.join(CONFIG_DIR, "source_dir")

CONFIRM_SECONDS = 5.0   # window for the second "Uninstall" click
TICK_SECONDS = 0.1
COUNT_SECONDS = 5.0     # how often the file count is refreshed

FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/LiberationSans-Bold.ttf",
]

# Gaming On Steam colours.
WHITE = (255, 255, 255, 255)
GREY = (159, 179, 200, 255)
BRAND = (24, 157, 255, 255)
ACCENT = (3, 191, 255, 255)
YELLOW = (255, 210, 60, 255)


def log(message):
    print(message, flush=True)


def load_font(size):
    for path in FONT_CANDIDATES + sorted(glob.glob("/usr/share/fonts/**/*Sans*Bold*.ttf", recursive=True)):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_config():
    """Recordings folder, port and whether to share SteamOS media folders, from ~/.config/framecopy/config.ini."""
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    section = config["framecopy"] if config.has_section("framecopy") else {}
    try:
        port = int(section.get("port", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    steamos_folders = str(section.get("steamos_folders", "yes")).lower() not in ("no", "false", "0", "off")
    return section.get("folder", DEFAULT_DIR), port, steamos_folders


def to_texture(img):
    data = img.tobytes()
    return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)


def run_detached(command):
    """Runs a shell command in its own systemd scope so it survives this service being restarted or removed."""
    subprocess.Popen(["systemd-run", "--user", "--collect", "/bin/bash", "-c", command],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Menu:
    """The 'FrameCopy' tab in the SteamVR dashboard."""
    WIDTH, HEIGHT = 800, 840
    BAR = (0, 0, 0, 255)
    BG = (8, 17, 31, 255)
    BUTTON = (26, 45, 77, 255)
    ON = BRAND
    OFF = (34, 56, 92, 255)
    DANGER = (200, 40, 60, 255)

    def __init__(self):
        self.title_font = load_font(52)
        self.big_font = load_font(56)
        self.font = load_font(34)
        self.small_font = load_font(26)
        self.buttons = []  # (x0, y0, x1, y1, action)

    def _button(self, draw, rect, text, action, fill=None, font=None):
        draw.rounded_rectangle(rect, radius=18, fill=fill or self.BUTTON)
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        draw.text((cx, cy), text, font=font or self.font, fill=WHITE, anchor="mm")
        self.buttons.append((*rect, action))

    def _text(self, draw, xy, text, font_sizes, width, **kwargs):
        """Draws text in the largest of font_sizes that fits width, cutting its start if none does."""
        for size in font_sizes:
            font = load_font(size)
            if draw.textlength(text, font=font) <= width:
                break
        while draw.textlength(text, font=font) > width and len(text) > 1:
            text = "…" + text[2:]
        draw.text(xy, text, font=font, **kwargs)

    def render(self, server, recordings, devices, status, confirming):
        """`confirming` is the action waiting for a second click ("forget" or "uninstall"), or None."""
        self.buttons = []
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), self.BG)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, self.WIDTH, 82), fill=self.BAR)
        draw.text((self.WIDTH // 2, 42), "FrameCopy", font=self.title_font, fill=BRAND, anchor="mm")

        sharing = server.active
        self._button(draw, (60, 100, 740, 200), "Stop sharing" if sharing else "Start sharing", "toggle",
                     fill=self.ON if sharing else self.OFF, font=self.big_font)

        if sharing:
            ip_url, local_url = server.urls[1], server.urls[0]
            cx = self.WIDTH // 2
            draw.text((cx, 245), "In a browser on your PC or phone, open", font=self.small_font, fill=GREY, anchor="mm")
            # The .local name is easier to type; the IP address is the backup for devices that can't resolve it.
            self._text(draw, (cx, 305), local_url, (52, 44, 36, 30), 680, fill=WHITE, anchor="mm")
            self._text(draw, (cx, 360), "or " + ip_url, (28, 24, 20), 680, fill=GREY, anchor="mm")
            pin_font = load_font(80)
            label_width = draw.textlength("PIN  ", font=self.font)
            x = cx - (label_width + draw.textlength(server.pin, font=pin_font)) / 2
            draw.text((x, 445), "PIN", font=self.font, fill=GREY, anchor="lm")
            draw.text((x + label_width, 445), server.pin, font=pin_font, fill=ACCENT, anchor="lm")
            footer = f"Stops by itself after {IDLE_MINUTES} minutes without use"
        else:
            draw.text((self.WIDTH // 2, 330), "Turn on sharing to play, download", font=self.font,
                      fill=GREY, anchor="mm")
            draw.text((self.WIDTH // 2, 380), "or delete your files from a browser", font=self.font,
                      fill=GREY, anchor="mm")
            draw.text((self.WIDTH // 2, 430), "on the same Wi-Fi.", font=self.font, fill=GREY, anchor="mm")
            footer = ""

        folder = server.recordings_dir.replace(os.path.expanduser("~"), "~", 1)
        count = f"{recordings} file{'' if recordings == 1 else 's'}"
        line = f"{count}: recordings, screenshots, videos, pictures" if server.steamos_folders else f"{count} in {folder}"
        self._text(draw, (self.WIDTH // 2, 540), line, (28, 24, 20), 680,
                   fill=GREY, anchor="mm")
        if footer:
            draw.text((self.WIDTH // 2, 585), footer, font=self.small_font, fill=GREY, anchor="mm")

        self._button(draw, (60, 690, 280, 760), "Confirm?" if confirming == "forget" else f"Forget PCs ({devices})",
                     "forget", font=self.small_font)
        self._button(draw, (290, 690, 510, 760), "Update", "update", font=self.small_font)
        self._button(draw, (520, 690, 740, 760), "Confirm?" if confirming == "uninstall" else "Uninstall",
                     "uninstall", fill=self.DANGER, font=self.small_font)

        if status:
            draw.text((self.WIDTH // 2, 800), status, font=self.small_font, fill=YELLOW, anchor="mm")
        return img

    def hit(self, x, y):
        for x0, y0, x1, y1, action in self.buttons:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return action
        return None

    @staticmethod
    def thumbnail():
        img = Image.new("RGBA", (256, 256), Menu.BAR)
        draw = ImageDraw.Draw(img)
        draw.text((128, 128), "COPY", font=load_font(60), fill=BRAND, anchor="mm")
        return img


def steamvr_running():
    """True if a SteamVR server is up. Background init never launches SteamVR itself."""
    try:
        openvr.init(openvr.VRApplication_Background)
    except openvr.error_code.InitError:
        return False
    openvr.shutdown()
    return True


def run(server):
    """Shows the dashboard tab until SteamVR quits. Sharing is left as it is."""
    vr_system = openvr.init(openvr.VRApplication_Overlay)
    overlay = openvr.VROverlay()
    menu = Menu()

    menu_handle, thumb_handle = overlay.createDashboardOverlay(APP_KEY + ".dashboard", "FrameCopy")
    overlay.setOverlayWidthInMeters(menu_handle, 1.5)
    overlay.setOverlayInputMethod(menu_handle, openvr.VROverlayInputMethod_Mouse)
    mouse_scale = openvr.HmdVector2_t()
    mouse_scale[0], mouse_scale[1] = Menu.WIDTH, Menu.HEIGHT
    overlay.setOverlayMouseScale(menu_handle, mouse_scale)
    thumb = Menu.thumbnail()
    overlay.setOverlayRaw(thumb_handle, to_texture(thumb), thumb.width, thumb.height, 4)

    status = ""
    confirm_action = None  # "forget" or "uninstall" while waiting for its second click
    confirm_until = 0.0
    shown = None           # what the tab last drew, to redraw only on change
    next_count = 0.0
    recordings = 0
    devices = 0
    event = openvr.VREvent_t()

    def handle(action):
        nonlocal status, confirm_action, confirm_until
        confirmed = action == confirm_action and time.monotonic() < confirm_until
        confirm_action = None
        status = ""
        if action == "toggle":
            if server.active:
                server.stop()
            else:
                try:
                    server.start()
                except OSError as e:
                    status = f"Can't share on port {server.port}: {e.strerror or e}"
        elif action == "update":
            source = read_text(SOURCE_DIR_FILE)
            if not (source and os.path.isdir(os.path.join(source, ".git"))):
                status = "Update needs the original git checkout"
            else:
                status = "Updating FrameCopy... it will restart"
                run_detached(f'cd "{source}" && git pull --ff-only && ./install.sh')
        elif action == "forget":
            if confirmed:
                server.forget_all_devices()
                status = "Every computer will need the PIN again"
            elif server.device_count():
                confirm_action, confirm_until = "forget", time.monotonic() + CONFIRM_SECONDS
                status = "Click again to forget every remembered computer"
            else:
                status = "No computers are remembered"
        elif action == "uninstall":
            if confirmed:
                status = "Uninstalling..."
                run_detached(f'"{INSTALL_DIR}/uninstall.sh"')
            else:
                confirm_action, confirm_until = "uninstall", time.monotonic() + CONFIRM_SECONDS
                status = "Click again to uninstall"

    while True:
        now = time.monotonic()

        while vr_system.pollNextEvent(event):
            if event.eventType == openvr.VREvent_Quit:
                vr_system.acknowledgeQuit_Exiting()
                return

        while overlay.pollNextOverlayEvent(menu_handle, event)[0]:
            if event.eventType == openvr.VREvent_MouseButtonUp:
                # OpenVR mouse coordinates have their origin at the bottom-left of the texture.
                action = menu.hit(event.data.mouse.x, Menu.HEIGHT - event.data.mouse.y)
                if action:
                    handle(action)
                    next_count = 0.0

        # Let an unanswered confirmation lapse on its own.
        if confirm_action and now >= confirm_until:
            confirm_action = None
            status = ""

        if now >= next_count:
            next_count = now + COUNT_SECONDS
            recordings = len(server.recordings())
            devices = server.device_count()

        # Sharing can stop by itself (idle), so the tab follows the server, not the last click.
        state = (server.active, server.pin, tuple(server.urls) if server.active else (), recordings, devices,
                 status, confirm_action)
        if state != shown:
            shown = state
            img = menu.render(server, recordings, devices, status, confirm_action)
            overlay.setOverlayRaw(menu_handle, to_texture(img), img.width, img.height, 4)

        time.sleep(TICK_SECONDS)


def main():
    folder, port, steamos_folders = read_config()
    server = ShareServer(folder, port, steamos_folders=steamos_folders)

    def quit_app(*_):
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, quit_app)
    while True:
        # Wait for SteamVR rather than launching it.
        while not steamvr_running():
            time.sleep(5)
        try:
            run(server)
        finally:
            openvr.shutdown()
        time.sleep(5)


if __name__ == "__main__":
    main()
