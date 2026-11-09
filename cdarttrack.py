#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# CDArtTrack - Copyright (C) 2026 CDArtTrack contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; see the LICENSE file.
"""CDArtTrack: playable CD-DA music on the inner part of a CD-R, visible artwork outside.

The artwork is drawn by Disc Image Studio's data-mode CD image (a full-disc Mode 1
ISO whose sector bytes form a picture). This tool cuts that image so that the music
occupies the first sectors and the rest of the picture becomes a Mode 1 data track,
which CD players do not play.

Subcommands
  layout   radii of the music / artwork areas and an optional design template PNG
  art      run Disc Image Studio to render the full-disc artwork image
  build    splice songs + artwork into WAV/BIN + CUE + cdrdao TOC (with CD-TEXT)
  verify   Linux only: check a burned disc (CD-TEXT, silence at the boundary, data)

Only the Python 3 standard library is used. See README.md for the full workflow.
"""
import argparse
import ctypes
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib

SECTOR = 2352            # CD-DA bytes per sector (1/75 s)
DATA_SECTOR = 2048       # Mode 1 user bytes per sector
SECTORS_PER_SECOND = 75
STANDARD_VELOCITY = 1200.0  # mm/s, as used by Disc Image Studio

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class Geometry:
    """Disc geometry in Disc Image Studio terms."""

    def __init__(self, r0, r1, sectors, usable=None, velocity=STANDARD_VELOCITY,
                 pitch=(0.0, 0.0, 0.0)):
        self.r0, self.r1, self.sectors = float(r0), float(r1), int(sectors)
        self.usable = int(usable) if usable else None
        self.velocity = float(velocity)
        self.pitch = tuple(float(p) for p in pitch)

    @property
    def output_sectors(self):
        return self.usable or self.sectors

    def radius(self, lba):
        """Constant-pitch radius estimate of a sector (good to ~0.1 mm)."""
        frac = min(max(lba / self.sectors, 0.0), 1.0)
        return math.sqrt(self.r0 ** 2 + (self.r1 ** 2 - self.r0 ** 2) * frac)

    @property
    def image_outer_radius(self):
        """Radius that Disc Image Studio maps to the edge of the source image."""
        return self.radius(self.output_sectors) if self.usable else self.r1

    def cli_args(self):
        args = ["--r0", repr(self.r0), "--r1", repr(self.r1), "--sectors", str(self.sectors),
                "--velocity", repr(self.velocity)]
        if self.usable:
            args += ["--usable-sectors", str(self.usable)]
        for name, value in zip(("linear", "quadratic", "cubic"), self.pitch):
            if value:
                args += [f"--pitch-{name}", repr(value)]
        return args


def add_geometry_args(ap):
    g = ap.add_argument_group("disc geometry (use --calibration or the explicit values)")
    g.add_argument("--calibration", help="Disc Image Studio .calibration.json (CD)")
    g.add_argument("--r0", type=float, help="inner radius, mm")
    g.add_argument("--r1", type=float, help="outer radius, mm")
    g.add_argument("--sectors", type=int, help="total sectors (ATIP lead-out start)")
    g.add_argument("--usable-sectors", type=int, help="sectors actually written (default: --sectors)")
    g.add_argument("--velocity", type=float, default=STANDARD_VELOCITY, help="mm/s (default 1200)")
    g.add_argument("--pitch-linear", type=float, default=0.0)
    g.add_argument("--pitch-quadratic", type=float, default=0.0)
    g.add_argument("--pitch-cubic", type=float, default=0.0)


def geometry_from_args(a):
    if a.calibration:
        with open(a.calibration, encoding="utf-8-sig") as f:
            doc = json.load(f)
        p = (doc.get("target") or doc).get("parameters") or sys.exit(
            f"{a.calibration}: no target.parameters found")
        if str(p.get("kind", "Cd")).lower() != "cd":
            sys.exit(f"{a.calibration}: not a CD calibration (kind={p.get('kind')})")
        return Geometry(p["innerRadiusMm"], p["outerRadiusMm"], p["sectors"],
                        p.get("usableSectors"), p.get("linearDensity", STANDARD_VELOCITY),
                        (p.get("pitchLinear", 0), p.get("pitchQuadratic", 0), p.get("pitchCubic", 0)))
    if a.r0 is None or a.r1 is None or a.sectors is None:
        sys.exit("give --calibration, or --r0, --r1 and --sectors")
    return Geometry(a.r0, a.r1, a.sectors, a.usable_sectors, a.velocity,
                    (a.pitch_linear, a.pitch_quadratic, a.pitch_cubic))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def msf(sectors):
    return f"{sectors // 4500:02d}:{sectors // 75 % 60:02d}:{sectors % 75:02d}"


def read_wav(path):
    """Return (fmt tuple, data_offset, data_length) of a RIFF/WAVE file."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            sys.exit(f"{path}: not a RIFF/WAVE file")
        fmt = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                sys.exit(f"{path}: no data chunk")
            cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                body = f.read(size)
                fmt = struct.unpack("<HHIIHH", body[:16])
                if size & 1:
                    f.seek(1, 1)
            elif cid == b"data":
                if fmt is None:
                    sys.exit(f"{path}: data chunk before fmt chunk")
                return fmt, f.tell(), size
            else:
                f.seek(size + (size & 1), 1)


def check_cdda(path, fmt):
    tag, ch, rate, _, _, bits = fmt
    if (tag, ch, rate, bits) != (1, 2, 44100, 16):
        sys.exit(f"{path}: need PCM 44100 Hz / 16-bit / stereo, got "
                 f"format={tag} ch={ch} rate={rate} bits={bits}. Convert with e.g.\n"
                 f'  ffmpeg -i "{path}" -af aresample=resampler=soxr -ar 44100 -ac 2 '
                 f"-sample_fmt s16 out.wav")


def song_sectors(path):
    fmt, _, length = read_wav(path)
    check_cdda(path, fmt)
    return math.ceil(length / SECTOR)


def copy(src, dst, n, buf=1 << 20):
    while n:
        b = src.read(min(buf, n))
        if not b:
            raise IOError("unexpected end of file")
        dst.write(b)
        n -= len(b)


def wav_header(data_len):
    return (b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16)
            + b"data" + struct.pack("<I", data_len))


def parse_duration(text):
    """'4:23', '263.5' or '1:02:03' -> seconds."""
    parts = [float(p) for p in text.split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


# ---------------------------------------------------------------------------
# CD-TEXT
# ---------------------------------------------------------------------------

CDTEXT_ENCODINGS = {"ASCII": "ascii", "ISO_8859_1": "latin-1", "MS_JIS": "cp932"}
CDTEXT_FIELDS = ("title", "performer", "songwriter", "composer", "arranger", "message")


def load_cdtext(path, n_songs):
    with open(path, encoding="utf-8-sig") as f:
        ct = json.load(f)
    langs = ct.get("languages") or sys.exit(f"{path}: 'languages' is required")
    for lang in langs:
        if lang.get("encoding", "ISO_8859_1") not in CDTEXT_ENCODINGS:
            sys.exit(f"{path}: unsupported encoding {lang.get('encoding')} "
                     f"(use one of {', '.join(CDTEXT_ENCODINGS)})")
    tracks = ct.get("tracks", [])
    if len(tracks) != n_songs:
        sys.exit(f"{path}: {len(tracks)} entries in 'tracks' for {n_songs} songs")
    blocks = [ct.get("disc", {})] + tracks + [ct.get("art_track") or sys.exit(
        f"{path}: 'art_track' is required (cdrdao wants CD-TEXT on every track)")]
    for b in blocks:
        for key in ("title", "performer"):
            if len(b.get(key, [])) != len(langs):
                sys.exit(f"{path}: every entry needs '{key}' with {len(langs)} value(s)")
    return langs, blocks


def cdtext_bytes(text, encoding):
    """Encode one CD-TEXT string including its terminator.

    cdrdao 1.2.5 drops the NUL terminators of text items, so all strings of one
    pack type end up concatenated on the disc. Writing the items as binary data
    with explicit terminators avoids that. MS-JIS strings end with two NULs.
    """
    try:
        raw = text.encode(CDTEXT_ENCODINGS[encoding])
    except UnicodeEncodeError:
        sys.exit(f"CD-TEXT: {text!r} cannot be encoded as {encoding}")
    if b"\0" in raw:
        sys.exit(f"CD-TEXT: {text!r} contains a NUL character")
    return raw + (b"\0\0" if encoding == "MS_JIS" else b"\0")


def toc_comment(text):
    return text.replace("\n", " ")


def cdtext_toc(block, langs, disc_level):
    out = ["CD_TEXT {"]
    if disc_level:
        out.append("  LANGUAGE_MAP { " + " ".join(f"{i} : {l['code']}" for i, l in enumerate(langs)) + " }")
    for i, lang in enumerate(langs):
        enc = lang.get("encoding", "ISO_8859_1")
        out.append(f"  LANGUAGE {i} {{")
        if disc_level:
            out.append(f"    ENCODING_{enc}")
        for key in CDTEXT_FIELDS:
            if key in block:
                data = ", ".join(str(b) for b in cdtext_bytes(block[key][i], enc))
                out.append(f"    // {key.upper()}: {toc_comment(block[key][i])}")
                out.append(f"    {key.upper()} {{ {data} }}")
        out.append("  }")
    return out + ["}"]


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


def write_png_gray(path, width, height, row_fn):
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(row_fn(y))
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def cmd_layout(a):
    geo = geometry_from_args(a)
    music = sum(parse_duration(d) for d in a.duration) if a.duration else 0.0
    if a.songs:
        music += sum(song_sectors(p) for p in a.songs) / SECTORS_PER_SECOND
    music_sectors = math.ceil(music * SECTORS_PER_SECOND) + a.gap_sectors if music else 0
    art_start = music_sectors + a.data_pregap if music else 0
    outer = geo.image_outer_radius
    px = a.size / 2 / outer
    rows = [("recordable area edge = canvas edge", outer),
            ("artwork starts", geo.radius(art_start)),
            ("music area ends", geo.radius(music_sectors)),
            ("recordable area starts", geo.r0),
            ("centre hole (15 mm)", 7.5)]
    print(f"music + gap: {msf(music_sectors)} ({music_sectors} sectors), "
          f"artwork from LBA {art_start}, disc {msf(geo.output_sectors)}")
    print(f"canvas {a.size} x {a.size} px, centre ({a.size / 2:g}, {a.size / 2:g}), "
          f"{1 / px:.5f} mm/px")
    for name, r in rows:
        print(f"  {name:36s} r = {r:7.3f} mm  {r * px:8.1f} px")
    print("  (constant-pitch estimate; keep important content ~2 mm away from the boundaries)")
    if a.template:
        c = (a.size - 1) / 2
        r_hub, r_music, r_art = geo.r0 * px, geo.radius(music_sectors) * px, geo.radius(art_start) * px
        r_out = a.size / 2
        r_safe_in, r_safe_out = (geo.radius(art_start) + 2) * px, (outer - 2) * px

        def row(y):
            dy2 = (y - c) ** 2
            out = bytearray(a.size)
            for x in range(a.size):
                r = math.sqrt((x - c) ** 2 + dy2)
                if r > r_out:
                    v = 200
                elif r < r_hub:
                    v = 70
                elif r < r_art:
                    v = 140
                elif abs(r - r_safe_in) < 1.5 or abs(r - r_safe_out) < 1.5:
                    v = 0
                else:
                    v = 255
                out[x] = v
            return out
        write_png_gray(a.template, a.size, a.size, row)
        print(f"template written: {a.template} (dark: not recordable, grey: music, "
              f"thin black circles: suggested safe area)")


# ---------------------------------------------------------------------------
# art
# ---------------------------------------------------------------------------


def cmd_art(a):
    geo = geometry_from_args(a)
    exe = a.dis or os.environ.get("DISC_IMAGE_STUDIO") or "disc-image-studio"
    with tempfile.TemporaryDirectory(prefix="cdarttrack-", dir=a.tmpdir) as empty:
        cmd = [exe, "cd-generate", "--input", a.image, "--output", a.output,
               "--data-dir", empty, "--file-position", "inner", "--volume-label", a.volume_label,
               *geo.cli_args(),
               "--tone-dither", repr(a.tone_dither), "--tone-levels", str(a.tone_levels)]
        print("running:", " ".join(cmd), flush=True)
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            sys.exit(f"cannot run {exe!r}; pass --dis /path/to/disc-image-studio")
        except subprocess.CalledProcessError as e:
            sys.exit(f"disc-image-studio failed with exit code {e.returncode}")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def cmd_build(a):
    songs, cursor, starts = [], 0, []
    for p in a.songs:
        fmt, off, ln = read_wav(p)
        check_cdda(p, fmt)
        secs = math.ceil(ln / SECTOR)
        if secs < 4 * SECTORS_PER_SECOND:
            print(f"warning: {p} is shorter than 4 s (Red Book minimum track length)")
        songs.append((p, off, ln, secs))
        starts.append(cursor)
        cursor += secs
    music_end = cursor + a.gap_sectors
    if a.data_pregap < 0 or a.gap_sectors < 0:
        sys.exit("--data-pregap and --gap-sectors must be >= 0")
    cdtext = load_cdtext(a.cdtext, len(songs)) if a.cdtext else None

    size = os.path.getsize(a.art)
    if size % DATA_SECTOR:
        sys.exit(f"{a.art}: size is not a multiple of 2048")
    total = size // DATA_SECTOR
    art_start = music_end + a.data_pregap
    side = a.art + ".json"
    if os.path.exists(side):
        with open(side, encoding="utf-8-sig") as f:
            meta = {k.lower(): v for k, v in json.load(f).items()}
        ds = meta.get("drawingstartlba")
        if ds is not None and art_start < ds:
            sys.exit(f"artwork would start at LBA {art_start}, before the drawing area (LBA {ds})")
        if meta.get("filesatouterring"):
            sys.exit(f"{a.art}: render it with files at the inner ring and an empty data folder")
    else:
        print(f"warning: {side} not found, the drawing range cannot be checked")
    if art_start >= total - 300:
        sys.exit(f"music needs {art_start} sectors ({msf(art_start)}) "
                 f"but the image has {total} ({msf(total)})")

    os.makedirs(a.out_dir, exist_ok=True)
    wav = os.path.join(a.out_dir, a.name + ".wav")
    binf = os.path.join(a.out_dir, a.name + "-art.bin")
    with open(wav, "wb") as out:
        out.write(wav_header(music_end * SECTOR))
        for p, off, ln, secs in songs:
            with open(p, "rb") as s:
                s.seek(off)
                copy(s, out, ln)
            out.write(b"\0" * (secs * SECTOR - ln))  # pad to a sector boundary
        out.write(b"\0" * (a.gap_sectors * SECTOR))
    with open(a.art, "rb") as iso, open(binf, "wb") as out:
        iso.seek(art_start * DATA_SECTOR)
        copy(iso, out, (total - art_start) * DATA_SECTOR)

    n = len(songs) + 1
    cue = [f'FILE "{os.path.basename(wav)}" WAVE']
    for i, st in enumerate(starts, 1):
        cue += [f"  TRACK {i:02d} AUDIO", f"    INDEX 01 {msf(st)}"]
    cue += [f'FILE "{os.path.basename(binf)}" BINARY', f"  TRACK {n:02d} MODE1/2048"]
    if a.data_pregap:
        cue.append(f"    PREGAP {msf(a.data_pregap)}")
    cue.append("    INDEX 01 00:00:00")
    with open(os.path.join(a.out_dir, a.name + ".cue"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\n".join(cue) + "\n")

    toc = ["CD_ROM", ""]
    if cdtext:
        langs, blocks = cdtext
        toc += cdtext_toc(blocks[0], langs, True) + [""]
    text = (lambda k: cdtext_toc(blocks[k], langs, False)) if cdtext else (lambda k: [])
    ends = starts[1:] + [music_end]
    for i, (st, en) in enumerate(zip(starts, ends), 1):
        toc += ["TRACK AUDIO"] + text(i) + [f'AUDIOFILE "{os.path.basename(wav)}" {msf(st)} {msf(en - st)}', ""]
    toc += ["TRACK MODE1"] + text(n) + ([f"PREGAP {msf(a.data_pregap)}"] if a.data_pregap else []) \
        + [f'DATAFILE "{os.path.basename(binf)}"', ""]
    with open(os.path.join(a.out_dir, a.name + ".toc"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(toc))

    info = {"songs": len(songs), "track_starts": starts, "music_end_lba": music_end,
            "art_track_lba": art_start, "total_sectors": total, "data_pregap": a.data_pregap}
    with open(os.path.join(a.out_dir, a.name + ".layout.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    print(f"songs: {len(songs)}, music + gap ends at LBA {music_end} ({msf(music_end)}), "
          f"disc {msf(total)}")
    print(f"art data track {n} starts at LBA {art_start} ({msf(art_start)})")
    if a.data_pregap:
        print("note: many CD players play the data-mode pregap as loud noise; "
              "--data-pregap 0 avoids it")
    if a.cdtext is None:
        print("note: no CD-TEXT (pass --cdtext)")
    print(f"wrote {a.out_dir}/{a.name}.{{wav,cue,toc,layout.json}} and {a.name}-art.bin")


# ---------------------------------------------------------------------------
# verify (Linux)
# ---------------------------------------------------------------------------

CDROMREADAUDIO = 0x530E
CDROM_LBA = 0x01
SG_IO = 0x2285
SG_DXFER_FROM_DEV = -3


class _ReadAudio(ctypes.Structure):
    _fields_ = [("lba", ctypes.c_int), ("addr_format", ctypes.c_ubyte),
                ("nframes", ctypes.c_int), ("buf", ctypes.c_void_p)]


class _SgIoHdr(ctypes.Structure):
    _fields_ = [("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
                ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
                ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
                ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
                ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
                ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
                ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
                ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
                ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
                ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
                ("duration", ctypes.c_uint), ("info", ctypes.c_uint)]


def read_audio(dev, lba, count):
    import fcntl
    buf = ctypes.create_string_buffer(count * SECTOR)
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, CDROMREADAUDIO, _ReadAudio(lba, CDROM_LBA, count, ctypes.addressof(buf)))
    finally:
        os.close(fd)
    return buf.raw


def read_cdtext_packs(dev):
    import fcntl
    size = 0xFFFF
    buf = ctypes.create_string_buffer(size)
    cdb = (ctypes.c_ubyte * 10)(0x43, 0, 0x05, 0, 0, 0, 0, size >> 8, size & 0xFF, 0)
    sense = ctypes.create_string_buffer(32)
    h = _SgIoHdr(interface_id=ord("S"), dxfer_direction=SG_DXFER_FROM_DEV, cmd_len=10,
                 mx_sb_len=32, dxfer_len=size, dxferp=ctypes.addressof(buf),
                 cmdp=ctypes.addressof(cdb), sbp=ctypes.addressof(sense), timeout=30000)
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, SG_IO, h)
    finally:
        os.close(fd)
    if h.status or h.host_status or h.driver_status:
        return None
    n = int.from_bytes(buf.raw[:2], "big") + 2
    data = buf.raw[4:n]
    return [data[i:i + 18] for i in range(0, len(data) - 17, 18)]


def decode_cdtext(packs):
    """{(block, type): [disc, track1, ...]} plus {block: charset}."""
    charset, raw = {}, {}
    for p in packs:
        t, block = p[0], (p[3] >> 4) & 7
        if t == 0x8F and p[1] == 0:
            charset[block] = p[4]
        if 0x80 <= t <= 0x85:
            raw.setdefault((block, t), bytearray()).extend(p[4:16])
    result = {}
    for (block, t), b in raw.items():
        double = charset.get(block) == 0x80
        items, cur, i = [], bytearray(), 0
        while i < len(b):
            if double and b[i:i + 2] == b"\x09\x09" or not double and b[i] == 0x09:
                items.append(items[-1] if items else "")  # TAB = same as previous
                i += 4 if double else 2
                cur = bytearray()
                continue
            if double and b[i:i + 2] == b"\0\0" or not double and b[i] == 0:
                items.append(bytes(cur).decode("cp932" if double else "latin-1", "replace"))
                cur = bytearray()
                i += 2 if double else 1
                continue
            cur.append(b[i])
            i += 1
        while items and items[-1] == "" and len(items) > 1:
            items.pop()
        result[(block, t)] = items
    return result, charset


def rms(samples_bytes):
    n = len(samples_bytes) // 2
    vals = struct.unpack(f"<{n}h", samples_bytes[:n * 2])
    return math.sqrt(sum(v * v for v in vals) / n) if n else 0.0


def cmd_verify(a):
    if not sys.platform.startswith("linux"):
        sys.exit("verify uses Linux ioctls; on other systems check the disc with your burning tool")
    ok = True
    with open(a.layout, encoding="utf-8") as f:
        lay = json.load(f)
    base = a.layout[:-len(".layout.json")]
    binf = base + "-art.bin"
    music_end, art_lba = lay["music_end_lba"], lay["art_track_lba"]

    # 1. CD-TEXT
    if a.cdtext:
        langs, blocks = load_cdtext(a.cdtext, lay["songs"])
        packs = read_cdtext_packs(a.device)
        if not packs:
            print("CD-TEXT: none found on the disc  [FAIL]")
            ok = False
        else:
            got, charset = decode_cdtext(packs)
            for bi, lang in enumerate(langs):
                for key, t in (("title", 0x80), ("performer", 0x81)):
                    want = [b[key][bi] for b in blocks]
                    have = got.get((bi, t), [])[:len(want)]
                    good = have == want
                    ok &= good
                    print(f"CD-TEXT block {bi} {key:9s} {'[ok]  ' if good else '[FAIL]'} {have}")

    # 2. silence between the last song and the art track
    tail = max(0, music_end - a.gap_check)
    try:
        audio = read_audio(a.device, tail, music_end - tail)
        levels = [rms(audio[i * SECTOR:(i + 1) * SECTOR]) for i in range(music_end - tail)]
        loudest = max(levels) if levels else 0
        print(f"last {music_end - tail} audio sectors before the art track: max RMS {loudest:.0f} "
              f"{'[ok]' if loudest < 64 else '[check: expected digital silence]'}")
    except OSError as e:
        print(f"audio read failed: {e}")
        ok = False

    # 3. data track content
    if os.path.exists(binf):
        span = os.path.getsize(binf) // DATA_SECTOR
        for frac in (0.001, 0.25, 0.5, 0.75, 0.99):
            off = int(span * frac)
            try:
                with open(a.device, "rb", buffering=0) as dev, open(binf, "rb") as ref:
                    dev.seek((art_lba + off) * DATA_SECTOR)
                    ref.seek(off * DATA_SECTOR)
                    same = dev.read(4 * DATA_SECTOR) == ref.read(4 * DATA_SECTOR)
            except OSError as e:
                same = False
                print(f"data LBA {art_lba + off}: read error ({e})")
            ok &= same
            print(f"data LBA {art_lba + off}: {'[ok] matches' if same else '[FAIL] differs'}")
    print("RESULT:", "all checks passed" if ok else "some checks failed")
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("layout", help="radii of the music and artwork areas, design template")
    add_geometry_args(p)
    p.add_argument("--duration", action="append", help="song length, e.g. 4:23 (repeatable)")
    p.add_argument("--songs", nargs="*", help="or CD-format WAV files to measure")
    p.add_argument("--gap-sectors", type=int, default=150, help="silence after the last song (default 150)")
    p.add_argument("--data-pregap", type=int, default=0, help="pregap before the art track (default 0)")
    p.add_argument("--size", type=int, default=2400, help="square canvas size in px (default 2400)")
    p.add_argument("--template", help="write a grayscale PNG design template")
    p.set_defaults(func=cmd_layout)

    p = sub.add_parser("art", help="render the full-disc artwork image with Disc Image Studio")
    add_geometry_args(p)
    p.add_argument("--image", required=True, help="square artwork (PNG/JPEG/BMP/GIF)")
    p.add_argument("--output", required=True, help="output .iso")
    p.add_argument("--dis", help="path to the disc-image-studio CLI (or set DISC_IMAGE_STUDIO)")
    p.add_argument("--volume-label", default="CD_ART")
    p.add_argument("--tone-dither", type=float, default=0.75)
    p.add_argument("--tone-levels", type=int, default=2)
    p.add_argument("--tmpdir", help="where to create the temporary empty data folder")
    p.set_defaults(func=cmd_art)

    p = sub.add_parser("build", help="splice songs + artwork into burnable files")
    p.add_argument("--art", required=True, help="full-disc artwork .iso from 'art'")
    p.add_argument("--out-dir", default=".", help="output directory")
    p.add_argument("--name", default="disc", help="output base name (default 'disc')")
    p.add_argument("--cdtext", help="CD-TEXT JSON (see examples/cdtext.example.json)")
    p.add_argument("--gap-sectors", type=int, default=150,
                   help="digital silence after the last song (default 150 = 2 s)")
    p.add_argument("--data-pregap", type=int, default=0,
                   help="pregap before the data track in sectors (default 0; 150 is the "
                        "Red Book value but players may play it as loud noise)")
    p.add_argument("songs", nargs="+", help="CD-format WAV files (44.1 kHz/16-bit/stereo)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("verify", help="Linux: check a burned disc against the build output")
    p.add_argument("--device", default="/dev/sr0")
    p.add_argument("--layout", required=True, help="<name>.layout.json written by 'build'")
    p.add_argument("--cdtext", help="the CD-TEXT JSON used for 'build'")
    p.add_argument("--gap-check", type=int, default=150, help="audio sectors to check before the art track")
    p.set_defaults(func=cmd_verify)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
