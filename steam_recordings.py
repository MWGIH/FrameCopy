"""
Steam Game Recording support for FrameCopy: finds Steam's raw recordings and
clips and serves each one as a single MP4, built on the fly. Python standard
library only, no ffmpeg, and nothing extra is written to disk.

Steam saves a recording as an MPEG-DASH session, a folder holding
session.mpd, init-stream<N>.m4s and chunk-stream<N>-<number>.m4s for each
stream (stream0 video, stream1 audio). Those folders live under
userdata/<user>/gamerecordings/{video,clips}/ (or the folder chosen in Steam's
settings) inside folders named bg_<app id>_<YYYYMMDD>_<HHMMSS> for background
recordings and clip_<app id>_<YYYYMMDD>_<HHMMSS> for clips.

A session becomes one fragmented MP4: one moov holding every stream's track,
an index (sidx) so players can seek, then the chunks' moof/mdat pairs with
their track numbers and timestamps rewritten. Only the moof boxes (a few KB
each) are read into memory; the media itself is streamed from the chunk files.
"""

import glob
import os
import re
import struct
import threading

STEAM_FOLDER_NAME = re.compile(r"^(bg|clip)_(\d+)_(\d{8})_(\d{6})")
CHUNK_NAME = re.compile(r"^chunk-stream(\d+)-(\d+)\.m4s$")
INIT_NAME = re.compile(r"^init-stream(\d+)\.m4s$")


class UnsupportedRecording(Exception):
    pass


# --- MP4 boxes ---

def iter_boxes(data, start=0, end=None):
    """(type, box start, payload start, box end) for each box in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise UnsupportedRecording(f"bad box {kind!r}")
        yield kind.decode("latin-1"), pos, pos + header, pos + size
        pos += size


def find_box(data, path, start=0, end=None):
    """(payload start, box end) of the first box along path, e.g. ["mdia", "mdhd"], or None."""
    for kind, _, payload, box_end in iter_boxes(data, start, end):
        if kind == path[0]:
            return (payload, box_end) if len(path) == 1 else find_box(data, path[1:], payload, box_end)
    return None


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind.encode()) + payload


def file_boxes(path):
    """(type, offset, header size, size) of the top-level boxes in a file, reading headers only."""
    found = []
    with open(path, "rb") as f:
        total = os.fstat(f.fileno()).st_size
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            head = f.read(16)
            size, kind = struct.unpack_from(">I4s", head)
            header = 8
            if size == 1:
                size = struct.unpack_from(">Q", head, 8)[0]
                header = 16
            elif size == 0:
                size = total - pos
            if size < header or pos + size > total:
                raise UnsupportedRecording(f"bad box in {os.path.basename(path)}")
            found.append((kind.decode("latin-1"), pos, header, size))
            pos += size
    return found


def read_at(path, offset, length):
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)


# --- one stream's init segment ---

class Track:
    def __init__(self, init_path, track_id):
        self.track_id = track_id
        data = open(init_path, "rb").read()
        moov = find_box(data, ["moov"])
        if not moov:
            raise UnsupportedRecording("init segment has no moov")
        self.data = data
        self.ftyp = next((data[s:e] for k, s, _, e in iter_boxes(data) if k == "ftyp"), b"")
        self.mvhd = next((data[s:e] for k, s, _, e in iter_boxes(data, *moov) if k == "mvhd"), None)
        traks = [(s, e) for k, s, _, e in iter_boxes(data, *moov) if k == "trak"]
        if len(traks) != 1 or not self.mvhd:
            raise UnsupportedRecording("expected one track per stream")
        trak = bytearray(data[traks[0][0]:traks[0][1]])
        tkhd = find_box(trak, ["tkhd"], 8)
        version = trak[tkhd[0]]
        struct.pack_into(">I", trak, tkhd[0] + (20 if version == 1 else 12), track_id)
        self.trak = bytes(trak)
        mdhd = find_box(trak, ["mdia", "mdhd"], 8)
        version = trak[mdhd[0]]
        self.timescale = struct.unpack_from(">I", trak, mdhd[0] + (20 if version == 1 else 12))[0]
        trex = find_box(data, ["mvex", "trex"], *moov)
        if not trex:
            raise UnsupportedRecording("init segment has no trex")
        trex_payload = bytearray(data[trex[0]:trex[1]])
        struct.pack_into(">I", trex_payload, 4, track_id)
        self.trex = box("trex", bytes(trex_payload))
        self.default_duration = struct.unpack_from(">I", trex_payload, 12)[0]


# --- fragments ---

class Fragment:
    """One moof (patched in memory) and the byte ranges that follow it in its chunk file."""

    def __init__(self, track, moof, tfdt_at, tfdt_version, decode_time, duration, mfhd_at, tail):
        self.track = track
        self.moof = moof                # bytearray
        self.tfdt_at = tfdt_at
        self.tfdt_version = tfdt_version
        self.decode_time = decode_time
        self.duration = duration
        self.mfhd_at = mfhd_at
        self.tail = tail                # [(path, offset, length)] for the mdat(s)

    @property
    def size(self):
        return len(self.moof) + sum(length for _, _, length in self.tail)


def read_fragments(track, chunk_path):
    fragments = []
    boxes = file_boxes(chunk_path)
    for i, (kind, offset, header, size) in enumerate(boxes):
        if kind != "moof":
            continue
        moof = bytearray(read_at(chunk_path, offset, size))
        mfhd = find_box(moof, ["mfhd"], 8)
        traf = find_box(moof, ["traf"], 8)
        if not mfhd or not traf:
            raise UnsupportedRecording("moof without mfhd/traf")
        tfhd = find_box(moof, ["tfhd"], *traf)
        flags = struct.unpack_from(">I", moof, tfhd[0])[0] & 0xFFFFFF
        if flags & 0x1:
            raise UnsupportedRecording("absolute data offsets aren't supported")
        struct.pack_into(">I", moof, tfhd[0] + 4, track.track_id)
        default_duration = track.default_duration
        if flags & 0x8:
            pos = tfhd[0] + 8 + (4 if flags & 0x2 else 0)
            default_duration = struct.unpack_from(">I", moof, pos)[0]
        tfdt = find_box(moof, ["tfdt"], *traf)
        if not tfdt:
            raise UnsupportedRecording("fragment without tfdt")
        version = moof[tfdt[0]]
        decode_time = struct.unpack_from(">Q" if version == 1 else ">I", moof, tfdt[0] + 4)[0]
        duration = 0
        for kind2, _, payload, end in iter_boxes(moof, *traf):
            if kind2 != "trun":
                continue
            trun_flags = struct.unpack_from(">I", moof, payload)[0] & 0xFFFFFF
            count = struct.unpack_from(">I", moof, payload + 4)[0]
            pos = payload + 8 + (4 if trun_flags & 0x1 else 0) + (4 if trun_flags & 0x4 else 0)
            per_sample = 4 * bin(trun_flags & 0xF00).count("1")
            if trun_flags & 0x100:
                for n in range(count):
                    duration += struct.unpack_from(">I", moof, pos + n * per_sample)[0]
            else:
                duration += count * default_duration
        # The mdat (and anything else) up to the next moof belongs to this fragment.
        tail = []
        for kind2, offset2, _, size2 in boxes[i + 1:]:
            if kind2 == "moof":
                break
            if kind2 == "mdat":
                tail.append((chunk_path, offset2, size2))
        fragments.append(Fragment(track, moof, tfdt[0] + 4, version, decode_time, duration, mfhd[0] + 4, tail))
    return fragments


# --- a whole session as one MP4 ---

class DashMovie:
    """A Steam recording session served as one MP4. Rebuilt when the chunk files change."""

    def __init__(self, session_dir):
        self.session_dir = session_dir
        self._lock = threading.Lock()
        self._key = None
        self._parts = []    # [(start offset, bytes or (path, offset, length))]
        self.size = 0

    def _streams(self):
        inits, chunks = {}, {}
        for entry in os.scandir(self.session_dir):
            m = INIT_NAME.match(entry.name)
            if m:
                inits[int(m.group(1))] = entry.path
                continue
            m = CHUNK_NAME.match(entry.name)
            if m:
                chunks.setdefault(int(m.group(1)), []).append((int(m.group(2)), entry.path))
        streams = [n for n in sorted(inits) if chunks.get(n)]
        if 0 not in streams:
            raise UnsupportedRecording("no video stream")
        return [(inits[n], [p for _, p in sorted(chunks[n])]) for n in streams]

    def _change_key(self):
        newest, count = 0.0, 0
        for entry in os.scandir(self.session_dir):
            if entry.name.endswith(".m4s"):
                count += 1
                newest = max(newest, entry.stat().st_mtime)
        return count, newest

    def prepare(self):
        """Builds (or reuses) the layout of the MP4. Returns its size."""
        with self._lock:
            key = self._change_key()
            if key != self._key:
                self._build()
                self._key = key
            return self.size

    def _build(self):
        streams = self._streams()
        tracks = [Track(init, n + 1) for n, (init, _) in enumerate(streams)]
        per_track = [[f for chunk in chunk_paths for f in read_fragments(track, chunk)]
                     for track, (_, chunk_paths) in zip(tracks, streams)]
        if not per_track[0]:
            raise UnsupportedRecording("no video fragments")

        # Start every track at the same moment, as close to zero as possible.
        start_seconds = min(frags[0].decode_time / t.timescale for t, frags in zip(tracks, per_track) if frags)
        for track, frags in zip(tracks, per_track):
            shift = int(start_seconds * track.timescale)
            for frag in frags:
                value = frag.start = max(0, frag.decode_time - shift)
                if frag.tfdt_version == 1:
                    struct.pack_into(">Q", frag.moof, frag.tfdt_at, value)
                else:
                    struct.pack_into(">I", frag.moof, frag.tfdt_at, min(value, 0xFFFFFFFF))

        # Interleave: each video fragment, then the other streams' fragments up to its end time.
        video = tracks[0]
        groups = []
        cursors = [0] * len(tracks)
        for i, vfrag in enumerate(per_track[0]):
            group = [vfrag]
            end_seconds = (vfrag.decode_time + vfrag.duration) / video.timescale
            last = i == len(per_track[0]) - 1
            for t in range(1, len(tracks)):
                frags = per_track[t]
                while cursors[t] < len(frags) and (last or frags[cursors[t]].decode_time / tracks[t].timescale < end_seconds):
                    group.append(frags[cursors[t]])
                    cursors[t] += 1
            groups.append(group)

        sequence = 1
        for group in groups:
            for frag in group:
                struct.pack_into(">I", frag.moof, frag.mfhd_at, sequence)
                sequence += 1

        mvhd = bytearray(tracks[0].mvhd)
        version = mvhd[8]
        mvhd_timescale = struct.unpack_from(">I", mvhd, 8 + (20 if version == 1 else 12))[0]
        struct.pack_into(">I", mvhd, len(mvhd) - 4, len(tracks) + 1)  # next_track_ID
        total_seconds = max(sum(f.duration for f in frags) / t.timescale for t, frags in zip(tracks, per_track))
        mehd = box("mehd", struct.pack(">IQ", 1 << 24, int(total_seconds * mvhd_timescale)))
        moov = box("moov", bytes(mvhd) + b"".join(t.trak for t in tracks)
                   + box("mvex", mehd + b"".join(t.trex for t in tracks)))

        # A segment index on the video track lets players seek without reading the whole file.
        refs = b""
        for group in groups:
            size = sum(f.size for f in group)
            if size >= 1 << 31:
                raise UnsupportedRecording("fragment too large")
            refs += struct.pack(">III", size, group[0].duration, 0x90000000)
        sidx = box("sidx", struct.pack(">IIIQQHH", 1 << 24, video.track_id, video.timescale,
                                               per_track[0][0].start, 0, 0, len(groups))
                   + refs)

        parts = [tracks[0].ftyp + moov + sidx]
        for group in groups:
            for frag in group:
                parts.append(bytes(frag.moof))
                parts.extend(frag.tail)
        offset, laid_out = 0, []
        for part in parts:
            laid_out.append((offset, part))
            offset += len(part) if isinstance(part, bytes) else part[2]
        self._parts, self.size = laid_out, offset

    def read(self, start, end):
        """Yields the bytes from start to end (inclusive). Call prepare() first."""
        for offset, part in self._parts:
            length = len(part) if isinstance(part, bytes) else part[2]
            if offset + length <= start:
                continue
            if offset > end:
                break
            lo, hi = max(start, offset) - offset, min(end + 1, offset + length) - offset
            if isinstance(part, bytes):
                yield part[lo:hi]
            else:
                path, file_offset, _ = part
                with open(path, "rb") as f:
                    f.seek(file_offset + lo)
                    remaining = hi - lo
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise UnsupportedRecording("chunk file changed while reading")
                        yield chunk
                        remaining -= len(chunk)

    def first_video_frames(self):
        """Init plus first chunk of the video stream: enough for ffmpeg to grab a thumbnail."""
        init, chunks = self._streams()[0]
        return open(init, "rb").read() + open(chunks[0], "rb").read()


# --- finding recordings ---

def recording_folders(steam):
    """(user id, folder) pairs that may hold Steam recordings: the default and any custom folder."""
    folders = []
    for user_dir in sorted(glob.glob(os.path.join(steam, "userdata", "*"))):
        user = os.path.basename(user_dir)
        folders.append((user, os.path.join(user_dir, "gamerecordings")))
        try:
            with open(os.path.join(user_dir, "config", "localconfig.vdf"), errors="replace") as f:
                for line in f:
                    if '"BackgroundRecordPath"' in line:
                        m = re.search(r'"BackgroundRecordPath"\s+"([^"]+)"', line)
                        if m:
                            folders.append((user, m.group(1).replace("\\\\", "\\")))
                        break
        except OSError:
            pass
    seen, unique = set(), []
    for user, folder in folders:
        real = os.path.realpath(os.path.expanduser(folder))
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            unique.append((user, real))
    return unique


def find_sessions(steam):
    """Each recording session: dict with user, kind ("bg" or "clip"), app_id, date, time, session_dir,
    part (1-based, when a recording has several sessions), parts, size and mtime (of the newest chunk)."""
    sessions = []
    for user, folder in recording_folders(steam):
        for sub in ("video", "clips"):
            base = os.path.join(folder, sub)
            if not os.path.isdir(base):
                continue
            for entry in sorted(os.scandir(base), key=lambda e: e.name):
                m = STEAM_FOLDER_NAME.match(entry.name)
                if not m or not entry.is_dir(follow_symlinks=False):
                    continue
                session_dirs = sorted(os.path.dirname(p) for p in
                                      glob.glob(os.path.join(entry.path, "**", "init-stream0.m4s"), recursive=True))
                for part, session_dir in enumerate(session_dirs, 1):
                    size, newest = 0, 0.0
                    for chunk in os.scandir(session_dir):
                        if chunk.name.endswith(".m4s"):
                            st = chunk.stat()
                            size += st.st_size
                            newest = max(newest, st.st_mtime)
                    sessions.append({"user": user, "kind": m.group(1), "app_id": m.group(2), "date": m.group(3),
                                     "time": m.group(4), "folder": entry.name, "session_dir": session_dir,
                                     "part": part, "parts": len(session_dirs), "size": size, "mtime": newest})
    return sessions
