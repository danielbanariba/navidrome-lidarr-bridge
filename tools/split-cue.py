#!/usr/bin/env python3
"""Split a CUE image into tracks, so Lidarr can import it.

An EAC rip is often one audio file plus a cue sheet describing where each track
begins. That is a faithful way to keep a CD, and Navidrome reads it happily —
but Lidarr works per track and cannot read a cue sheet at all. A whole album in
one FLAC reaches it as a single unmatched file, and the import fails with
"Couldn't find similar album", which says nothing about the real reason.

This cuts the image into tracks with the tags Lidarr needs, leaving the original
untouched.

Usage:

    split-cue.py <folder-with-the-cue>                  # print a plan
    split-cue.py <folder-with-the-cue> --out <dir>      # write the tracks
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

AUDIO = (".flac", ".ape", ".wav", ".wv")
# Cue sheets are frequently written by Windows tools and are rarely UTF-8; one
# here decoded as UTF-8 turned "The Sunken Norwegian" into mojibake and carried
# it into the file name.
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def read_cue(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def parse(text: str) -> tuple[str, str, list[dict]]:
    album = (re.search(r'^TITLE\s+"(.*)"', text, re.M) or [None, ""])[1]
    artist = (re.search(r'^PERFORMER\s+"(.*)"', text, re.M) or [None, ""])[1]
    tracks = []
    for block in re.split(r"\n(?=\s*TRACK\s)", text)[1:]:
        found = re.search(r"TRACK\s+(\d+)", block)
        index = re.search(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", block)
        if not (found and index):
            continue
        minutes, seconds, frames = (int(x) for x in index.groups())
        tracks.append({
            "n": int(found.group(1)),
            "title": (re.search(r'TITLE\s+"(.*)"', block) or [None, ""])[1],
            # Cue sheets count in frames, and there are 75 of them to a second.
            "start": minutes * 60 + seconds + frames / 75.0,
        })
    return artist, album, tracks


def stamp(seconds: float) -> str:
    return f"{int(seconds) // 60}:{seconds - 60 * (int(seconds) // 60):06.3f}"


def duration(path: str) -> float | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def encode(source: str, out: str, tags: dict) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", source, "-map", "0:a:0",
           "-c:a", "flac", "-compression_level", "5"]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    return subprocess.run(cmd + [out], capture_output=True, text=True)


def cut(audio: str, start: float, end: float | None, out: str, tags: dict) -> bool:
    """One track out of the image, by whichever decoder can manage it.

    ffmpeg is tried first because it cuts and encodes in one pass. It refused one
    track of a verified image — "invalid block size" from its FLAC decoder, on a
    file the reference decoder and the rip's own CRC both call perfect — so when
    it fails the reference decoder takes the excerpt instead and ffmpeg only
    re-encodes it.
    """
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", audio, "-ss", f"{start:.4f}"]
    if end is not None:
        cmd += ["-to", f"{end:.4f}"]
    cmd += ["-map", "0:a:0", "-c:a", "flac", "-compression_level", "5"]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    subprocess.run(cmd + [out], capture_output=True, text=True)
    if duration(out):
        return True

    excerpt = out + ".wav"
    args = ["flac", "-d", "-f", f"--skip={stamp(start)}"]
    if end is not None:
        args.append(f"--until={stamp(end)}")
    done = subprocess.run(args + ["-o", excerpt, audio], capture_output=True, text=True)
    if done.returncode != 0:
        return False
    encode(excerpt, out, tags)
    os.remove(excerpt)
    return bool(duration(out))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="the folder holding the cue sheet and the image")
    ap.add_argument("--out", help="where to write the tracks; without it nothing is written")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe", "flac"):
        if not shutil.which(tool):
            sys.exit(f"needs {tool}")

    names = os.listdir(args.folder)
    cue = next((os.path.join(args.folder, f) for f in names if f.lower().endswith(".cue")), None)
    audio = next((os.path.join(args.folder, f) for f in names if f.lower().endswith(AUDIO)), None)
    if not cue or not audio:
        sys.exit("that folder holds no cue sheet and image pair")

    artist, album, tracks = parse(read_cue(cue))
    length = duration(audio)
    print(f"\n  {artist} — {album}")
    print(f"  {os.path.basename(audio)}  ({int(length or 0) // 60} min)")
    print(f"  {len(tracks)} tracks in the cue sheet\n")
    for i, track in enumerate(tracks):
        end = tracks[i + 1]["start"] if i + 1 < len(tracks) else length
        span = (end or 0) - track["start"]
        print(f"    {track['n']:>2}. {track['title'][:40]:<42} "
              f"{int(span) // 60}:{int(span) % 60:02d}")

    if not args.out:
        print("\n  nothing was written — pass --out to cut the tracks")
        return

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    print()
    failed = []
    for i, track in enumerate(tracks):
        end = tracks[i + 1]["start"] if i + 1 < len(tracks) else None
        safe = re.sub(r'[/\\:*?"<>|]', "_", track["title"]) or f"Track {track['n']}"
        out = os.path.join(args.out, f"{track['n']:02d} - {safe}.flac")
        tags = {"title": track["title"], "artist": artist, "albumartist": artist,
                "album": album, "track": f"{track['n']}/{len(tracks)}"}
        if cut(audio, track["start"], end, out, tags):
            print(f"    {track['n']:>2}. {track['title'][:44]:<46} ok")
        else:
            failed.append(track["n"])
            print(f"    {track['n']:>2}. {track['title'][:44]:<46} FAILED")
    print(f"\n  {len(tracks) - len(failed)} of {len(tracks)} written to {args.out}")
    if failed:
        print(f"  tracks {failed} could not be cut — the image is left untouched")


if __name__ == "__main__":
    main()
