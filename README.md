# CDArtTrack

[简体中文](README.zh-CN.md)

Make a CD-R that **plays music in an ordinary CD player** and also shows a
**picture on its recording surface**.

The songs are written as normal CD-DA tracks on the inner part of the disc. The
rest of the disc is a Mode 1 data track whose bytes are arranged by
[Disc Image Studio](https://github.com/JiaFeiMiao-K-Cat/DiscImageStudio) so that
the burned surface forms a visible image. CD players don't play data tracks, so
listeners only hear the music.

```
 centre ── not recordable ── music tracks ── 2 s silence ── artwork (data track) ── edge
```

CDArtTrack is a single Python script (standard library only) that glues the
pieces together:

| Command  | What it does |
|----------|--------------|
| `layout` | Computes the radius where the artwork starts for your songs and draws a design template PNG. |
| `art`    | Runs the Disc Image Studio CLI to render the full-disc artwork image (`.iso`). It can read a Disc Image Studio `.calibration.json`. |
| `build`  | Splices the songs and the artwork into `.wav` + `-art.bin` + `.cue` + cdrdao `.toc` (with CD-TEXT). |
| `verify` | Linux only: checks a burned disc (CD-TEXT, silence before the artwork, data track contents). |

This project is not affiliated with Disc Image Studio. It only runs its CLI.

## Requirements

- Python 3.8+
- [Disc Image Studio](https://github.com/JiaFeiMiao-K-Cat/DiscImageStudio/releases) CLI (`disc-image-studio`), to render the artwork
- [cdrdao](https://cdrdao.sourceforge.net/) 1.2.5 or later, to burn the `.toc` with CD-TEXT. Any burner that accepts a mixed-mode CUE sheet works without CD-TEXT.
- ffmpeg (optional), to convert songs to CD format
- A blank CD-R and a burner that supports DAO/SAO writing

## Workflow

### 1. Get the disc geometry

The picture only lines up if the generator knows the exact spiral of *your* discs:
inner/outer radius, sector count and track-pitch coefficients. Use one of these:

- a built-in Disc Image Studio preset that matches your media, or
- calibrate with the Disc Image Studio GUI (assisted calibration) and keep the `.calibration.json`.

Calibration files that have produced a correct picture are collected in
[`calibrations/`](calibrations/):

| File | Media | ATIP | Mold code (hub ring) |
|------|-------|------|----------------------|
| [`verbatim-phono-r-cmc-cwlht-1744.calibration.json`](calibrations/verbatim-phono-r-cmc-cwlht-1744.calibration.json) | Verbatim Phono-R CD-R 80 min (Japan) | CMC Magnetics, 359,846 sectors | `CWLHT-1744` |

Only use a file for discs with the same mold code. The same Phono-R pack also
contained `CWLHT-1579` discs, and this file does **not** fit those.

Every geometry option below accepts either `--calibration file.calibration.json`
or `--r0 --r1 --sectors [--usable-sectors] [--pitch-linear --pitch-quadratic --pitch-cubic]`.

> **Discs from the same pack can come from different molds.** Look at the code
> engraved in the clear ring around the centre hole and calibrate each mold
> separately. The same parameters on a disc from another mold produce a
> completely twisted picture.

### 2. Plan the layout and design the artwork

```sh
python3 cdarttrack.py layout --calibration my-disc.calibration.json \
    --duration 3:45 --duration 4:10 --template template.png
```

This prints the radii in mm and in pixels for a 2400 × 2400 canvas, and writes a
template:

- dark: not recordable
- grey: music area (no picture here)
- thin circles: a suggested safe area, about 2 mm inside the boundaries

Design rules:

- Square canvas. The canvas centre is the disc centre and the canvas edge is the end of the recordable area.
- High contrast. Only brightness matters, so black on white works best. Lines thinner than about 0.3 mm tend to vanish.
- Pictures on CD-R are faint. Look at them under a single point light, such as a flashlight, at an angle.

### 3. Convert the songs to CD format

```sh
ffmpeg -i song.flac -af aresample=resampler=soxr -ar 44100 -ac 2 -sample_fmt s16 song.wav
```

### 4. Render the full-disc artwork

```sh
python3 cdarttrack.py art --calibration my-disc.calibration.json \
    --image artwork.png --output art.iso --dis /path/to/disc-image-studio
```

This creates `art.iso` and `art.iso.json`. The artwork covers the whole disc; `build` keeps only the part outside the music.

### 5. Build the burn files

```sh
python3 cdarttrack.py build --art art.iso --out-dir out --name mydisc \
    --cdtext examples/cdtext.example.json song1.wav song2.wav
```

| Output | Contents |
|--------|----------|
| `mydisc.wav` | The songs, padded to whole sectors, plus 2 s of digital silence |
| `mydisc-art.bin` | The artwork sectors (Mode 1, 2048 bytes per sector) |
| `mydisc.toc` | Burn file for cdrdao, with CD-TEXT |
| `mydisc.cue` | Burn file for other tools (no CD-TEXT) |
| `mydisc.layout.json` | Used by `verify` |

CD-TEXT is described in JSON, with one value per language block. See
[`examples/cdtext.example.json`](examples/cdtext.example.json). Encodings:
`MS_JIS` (Japanese), `ISO_8859_1`, `ASCII`. Most players show only block 0,
so put the language you want shown first.

### 6. Burn with cdrdao

Run cdrdao **inside the output directory**, because file names in the TOC are
relative to the current directory.

```sh
cd out
cdrdao simulate --device /dev/sr0 --driver generic-mmc -n mydisc.toc
eject /dev/sr0 && eject -t /dev/sr0      # reload the disc after a simulation
cdrdao write    --device /dev/sr0 --driver generic-mmc --speed 16 -n mydisc.toc
```

### 7. Verify (Linux)

```sh
eject /dev/sr0 && eject -t /dev/sr0
python3 cdarttrack.py verify --device /dev/sr0 --layout out/mydisc.layout.json \
    --cdtext examples/cdtext.example.json
```

## Things learned the hard way

- **The data-track pregap plays as loud noise.** The Red Book wants a 2 s
  pregap in front of a data track. It consists of scrambled Mode 1 sectors, and
  many CD players play the next track's pregap as the end of the previous track.
  The result is about 2 s of near full-scale noise after the last song. `build`
  therefore defaults to `--data-pregap 0`: the 2 s of digital silence ends
  exactly where the data track starts, and the player stops there. (The burner
  used in testing labelled those silent sectors as the data track's index 00 by
  itself.) The picture is unaffected. Use `--data-pregap 150` if you need the standard
  layout.
- **cdrdao 1.2.5 corrupts CD-TEXT.** Its new character-encoding support drops
  the NUL terminators, so all titles run together ("AlbumTrack OneDisc Art").
  CDArtTrack writes every CD-TEXT item as binary data with explicit
  terminators, which works around this. `cdrdao read-toc` can also crash on
  MS-JIS CD-TEXT; use `cdarttrack.py verify` to read it back instead.
- **Reload the disc after `cdrdao simulate`.** The drive keeps reporting the
  simulated state, and the next run sees a "written" disc.
- **Emulated optical drives in virtual machines can fail mid-write.** If the
  drive's buffer keeps running empty, pass the real device through to the
  guest, or burn on the host.
- **The brand doesn't tell you the dye or the geometry.** Read the ATIP
  manufacturer, but treat its dye field as a hint only, and calibrate per mold.
- Very old CD players may try to play the data track. Modern players, cars and
  computers skip it.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
