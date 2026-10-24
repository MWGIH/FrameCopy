# FrameCopy

Play, download and delete your Steam Frame recordings from any browser on the same Wi-Fi: a PC, a Mac or a phone. Nothing needs installing on the other device, and nothing needs root on the Frame. It uses only Python's standard library.

It's made for recordings from [framecorder](https://github.com/coah80/framecorder), and it works with any folder of videos.

## Use it

1. **Turn on Developer Mode:** on the Frame, open **Settings → General** and turn on **Developer Mode** so you can open Konsole.
2. **Open Konsole,** paste this line and press Enter:

```bash
cd ~ && (git clone https://github.com/MWGIH/FrameCopy.git || git -C FrameCopy pull) && python3 FrameCopy/framecopy.py
```

3. It prints two addresses and a 4-digit PIN. Open either address on your PC and enter the PIN.

From the page you can:

- click a recording to play it in the browser
- download one recording
- tick several and download them as one zip
- delete recordings (it asks first)

Press Ctrl+C in Konsole to stop sharing. It also stops by itself after 30 minutes without use.

## Options

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
