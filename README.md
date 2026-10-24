# FrameCopy

Play, download and delete your Steam Frame recordings from any browser on the same Wi-Fi: a PC, a Mac or a phone. Nothing needs installing on the other device, and nothing needs root on the Frame. Start and stop it from a tab in the SteamVR dashboard.

It's made for recordings from [framecorder](https://github.com/coah80/framecorder), and it works with any folder of videos.

## Install

You can install it straight from the headset. You don't need Desktop Mode.

1. **Turn on Developer Mode:** on the Frame, open **Settings → General** and turn on **Developer Mode** so you can open Konsole.
2. **Open Konsole,** paste this line and press Enter:

```bash
cd ~ && (git clone https://github.com/MWGIH/FrameCopy.git || git -C FrameCopy pull) && FrameCopy/install.sh
```

Running the same line again later updates it.

## Use it

Open the **SteamVR dashboard** and select the **FrameCopy** tab.

1. Press **Start sharing**. The tab shows an address and a 4-digit PIN.
2. On your PC or phone, open the address in a browser and enter the PIN.
3. Click a recording to play it, download one, tick several to download them as one zip, or delete them (it asks first).
4. Press **Stop sharing** when you're done. It also stops by itself after 30 minutes without use.

Sharing keeps going if you leave VR, until you stop it or it times out.

The tab also has **Update**, which downloads the latest FrameCopy, and **Uninstall** (click twice). Uninstalling never touches your recordings.

## Settings

To share another folder or use another port, create `~/.config/framecopy/config.ini`:

```ini
[framecopy]
folder = ~/Videos/framecorder
port = 8642
```

Then restart FrameCopy with `systemctl --user restart framecopy`.

## Without the headset

You can also share from Konsole or over SSH, without the app or SteamVR:

```bash
python3 ~/FrameCopy/framecopy.py
```

It prints the address and PIN; press Ctrl+C to stop.

## Options for framecopy.py

| Option | What it does |
|---|---|
| `--dir FOLDER` | Folder to share (default `~/Videos/framecorder`). Subfolders one level down, such as `clips`, are included |
| `--port N` | Port to listen on (default 8642) |
| `--json` | For apps that run FrameCopy as a child process: prints one line `{"urls": [...], "pin": "1234", "port": 8642}` once listening, logs to stderr, exits 0 when sharing stops (idle, SIGTERM or Ctrl+C), and exits 1 if the port is taken |

## Good to know

- Only video files (`.mp4`, `.mkv`, `.webm`, `.mov`) are shown. Recordings still being written (`.part` files) appear once they finish.
- Thumbnails need `ffmpeg`. Without it the page still works, it just shows no previews.
- If the `.local` address doesn't open, use the one with the number (IP address).
- It uses plain HTTP and a PIN, which is fine on your home network. Don't turn it on over public Wi-Fi.

## Using it from another app

A recorder can start and stop sharing itself, for example from a "Share recordings" switch:

```python
from framecopy import ShareServer

server = ShareServer("~/Videos/framecorder", port=8642, on_stop=switch_off)
server.start()
show(server.urls, server.pin)   # server.urls: [".local" address, IP address]
...
server.stop()
```

`on_stop` is called when sharing stops by itself after 30 minutes idle.
