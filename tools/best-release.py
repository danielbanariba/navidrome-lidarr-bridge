#!/usr/bin/env python3
"""Audition several releases of one album and keep the best one.

Release titles are unreliable, and often silent. Of ten torrents for one album
here not one named its format, so Lidarr classified every one as "Unknown" and
would never take any of them as an upgrade — correctly, since it could not prove
any was better. The one with 39 seeders turned out to be MP3. The only way to
know what a release is, is to look inside it.

Three checks, cheapest first:

1.  The torrent's own file list. This costs no download at all and rules out
    every release carrying no lossless file, which is most of them.
2.  The audio streams of whatever survives: codec, bit depth, sample rate.
3.  The spectrum. A FLAC decoded from an MP3 is still an MP3, it just weighs
    more, and a brick wall at 16 kHz gives it away. A 24-bit file padded from
    16-bit is the same lie told about depth, and the bottom eight bits give
    that one away.

The verdict is per album, not per track: one odd file does not decide a record.
Whatever loses is removed from the download client; the winner is handed to
Lidarr, which imports it the ordinary way.

Nothing outside this script's own tag is ever touched.

Usage:

    best-release.py --album-id 42                 # free pass: look, download nothing
    best-release.py --album-id 42 --download      # audition for real
    best-release.py --artist Alestorm --album "No Grave but the Sea" --download
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import numpy as np
except ImportError:
    sys.exit("needs numpy: pip install numpy")

LIDARR_URL = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")
LIDARR_API_KEY = os.environ.get("LIDARR_API_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://localhost:9696").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")
QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8090").rstrip("/")
# What Prowlarr is called from inside the download client's network.
CLIENT_PROWLARR = os.environ.get("CLIENT_PROWLARR_URL", "http://prowlarr:9696").rstrip("/")

# qBittorrent reports the paths it sees. This script runs outside that container.
PATH_FROM = os.environ.get("QBIT_PATH_FROM", "/data")
PATH_TO = os.environ.get("QBIT_PATH_TO", "/mnt/Entretenimiento")

# Everything this script adds carries this tag, and it only ever deletes torrents
# that carry it. A bug here would delete somebody's library, so the blast radius
# is fixed in one place rather than argued about at each call site.
TAG = "ndlb-audition"
LIDARR_CATEGORY = os.environ.get("QBIT_LIDARR_CATEGORY", "lidarr")

LOSSLESS_EXT = {"flac", "ape", "wv", "alac", "m4a", "aiff", "wav"}
LOSSY_EXT = {"mp3", "ogg", "opus", "aac", "wma", "m4b"}
UA = "navidrome-lidarr-bridge-audition/1.0"


# ── talking to the stack ──────────────────────────────────────────────────

def _json(url: str, headers: dict, data=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def lidarr(path: str):
    if not LIDARR_API_KEY:
        sys.exit("set LIDARR_API_KEY")
    return _json(f"{LIDARR_URL}/api/v1{path}", {"X-Api-Key": LIDARR_API_KEY})


def prowlarr_search(term: str) -> list[dict]:
    if not PROWLARR_API_KEY:
        sys.exit("set PROWLARR_API_KEY")
    query = urllib.parse.urlencode(
        {"query": term, "categories": "3000", "type": "search", "limit": 100})
    return _json(f"{PROWLARR_URL}/api/v1/search?{query}",
                 {"X-Api-Key": PROWLARR_API_KEY}, timeout=240)


def qbt(path: str, fields: dict | None = None, timeout=60):
    url = f"{QBIT_URL}/api/v2{path}"
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode(errors="replace").strip()
    if not body or body == "Ok.":
        return body
    try:
        return json.loads(body)
    except ValueError:
        return body


def qbt_add(raw: bytes | None, url: str, fields: dict) -> None:
    """Hand a torrent to the client, by value when possible.

    Prowlarr's download link points at the host that asked for it, and the
    client is in a container where that address is itself — it fetched nothing
    and reported nothing, and every candidate silently failed to start. The
    .torrent is already in hand from the file-list pass, so it is uploaded
    directly and no address has to be reachable from anywhere.

    A magnet or a refused fetch still has to go by link, so that address is
    rewritten to one the client can actually resolve.
    """
    if raw:
        boundary = "----ndlb" + os.urandom(12).hex()
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; '
                         f'name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; '
                     f'filename="release.torrent"\r\n'
                     f'Content-Type: application/x-bittorrent\r\n\r\n'.encode())
        parts.append(raw + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"{QBIT_URL}/api/v2/torrents/add", data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "User-Agent": UA})
        urllib.request.urlopen(req, timeout=120).read()
        return
    qbt("/torrents/add", {**fields, "urls": rewrite_for_client(url)})


def rewrite_for_client(url: str) -> str:
    """An address this script can reach is not always one the client can."""
    if not CLIENT_PROWLARR:
        return url
    return url.replace(PROWLARR_URL, CLIENT_PROWLARR, 1)


# ── looking inside a torrent without downloading it ───────────────────────

def bdecode(data: bytes, i: int = 0):
    head = data[i:i + 1]
    if head == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            key, i = bdecode(data, i)
            out[key], i = bdecode(data, i)
        return out, i + 1
    if head == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            item, i = bdecode(data, i)
            out.append(item)
        return out, i + 1
    if head == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    colon = data.index(b":", i)
    length = int(data[i:colon])
    return data[colon + 1:colon + 1 + length], colon + 1 + length


class _KeepMagnet(urllib.request.HTTPRedirectHandler):
    """Stop at a redirect that leaves HTTP behind, and keep where it pointed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith("magnet:"):
            self.magnet = newurl
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_release(url: str) -> tuple[bytes | None, str]:
    """The .torrent if there is one, and the address that actually holds it.

    An indexer link is not always a torrent. Several here answer with a
    redirect to a magnet, which urllib refuses to follow — so the fetch failed,
    the file list stayed unknown, and with no metadata there was no id either.
    The candidate went into the client unidentified and could never be found
    again, which read as "the client started none of them" while three were
    downloading.

    Following that redirect ourselves yields the magnet, which carries the id
    in its own address and which the client can take directly.
    """
    if not url:
        return None, url
    if url.startswith("magnet:"):
        return None, url
    handler = _KeepMagnet()
    opener = urllib.request.build_opener(handler)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return opener.open(req, timeout=60).read(), url
    except Exception:
        return None, getattr(handler, "magnet", url)


def infohash(raw: bytes) -> str | None:
    """The torrent's own id, so it can be found again without guessing.

    Taken from the exact bytes of the info dictionary rather than by re-encoding
    a parsed copy, because a hash that disagrees with the client's identifies
    nothing.
    """
    key = raw.find(b"4:info")
    if key < 0:
        return None
    start = key + len(b"4:info")
    try:
        _, end = bdecode(raw, start)
    except Exception:
        return None
    return hashlib.sha1(raw[start:end], usedforsecurity=False).hexdigest()


def magnet_hash(url: str) -> str | None:
    """A magnet names the torrent it points at, which is all we need to find it.

    Without this the only way to tell which torrent the client just took was to
    diff its list before and after, which races against every other thing
    downloading on the machine.
    """
    found = re.search(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", url or "")
    if not found:
        return None
    value = found.group(1)
    if len(value) == 40:
        return value.lower()
    try:
        return base64.b32decode(value.upper()).hex()
    except Exception:
        return None


def torrent_extensions(raw: bytes | None) -> dict[str, int] | None:
    """What file types a torrent holds, read from its metadata alone."""
    if raw is None:
        return None
    try:
        meta, _ = bdecode(raw)
        info = meta[b"info"]
    except Exception:
        return None
    if b"files" in info:
        names = [b"/".join(f[b"path"]).decode("utf8", "replace") for f in info[b"files"]]
    else:
        names = [info[b"name"].decode("utf8", "replace")]
    counts: dict[str, int] = {}
    for name in names:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "?"
        counts[ext] = counts.get(ext, 0) + 1
    return counts


def classify(counts: dict[str, int] | None) -> str:
    if counts is None:
        return "unknown"
    if any(counts.get(e) for e in LOSSLESS_EXT):
        return "lossless"
    if any(counts.get(e) for e in LOSSY_EXT):
        return "lossy"
    return "empty"


# ── looking inside the audio ──────────────────────────────────────────────

def probe(path: str) -> dict | None:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "a:0", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        return None
    streams = json.loads(out.stdout or "{}").get("streams") or []
    return streams[0] if streams else None


def spectral_cutoff(path: str, seconds: int = 40) -> tuple[float | None, float]:
    """The frequency above which a track carries nothing.

    Measured as a cliff, not as a threshold. An earlier version compared high
    frequencies against the track's loudest bin — which is always bass, forty
    to sixty decibels up — and so read every honest record as cut off around
    17 kHz, including a known 320 kbps file whose real shelf is at 20.5 kHz.

    What an encoder leaves behind is a step: the spectrum runs along, then
    falls off a wall and stays down. So the reference is the track's own level
    at 10 kHz, well inside whatever it kept, and the answer is the frequency
    where it drops far below that and does not come back.

    Averaged over the whole excerpt rather than read off one frame: a quiet
    passage has no high content either, and would frame an honest file as a
    transcode.
    """
    rate = 48000
    proc = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-ss", "30", "-t", str(seconds), "-i", path,
         "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
        capture_output=True)
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size < rate * 5:
        return None, 0.0

    size = 16384
    frames = samples[:samples.size - samples.size % size].reshape(-1, size)
    if not frames.size:
        return None, 0.0
    window = np.hanning(size).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(frames * window, axis=1)).mean(axis=0)
    if not spectrum.any():
        return None, 0.0

    freqs = np.fft.rfftfreq(size, 1 / rate)
    power = 20 * np.log10(spectrum + 1e-20)
    # Smoothed over roughly 300 Hz, so one narrow tone cannot pass for a shelf.
    span = max(1, int(300 / (freqs[1] - freqs[0])))
    power = np.convolve(power, np.ones(span) / span, mode="same")

    reference = power[np.argmin(np.abs(freqs - 10000))]
    band = (freqs >= 11000) & (freqs <= rate / 2 - 500)
    if not band.any():
        return None, 0.0
    idx = np.where(band)[0]

    # How steeply it falls matters more than how high it reaches. A 2009 metal
    # master runs out of content around 21 kHz all by itself, which sits right
    # on top of where a 320 kbps encoder puts its wall — so a frequency alone
    # cannot tell an honest quiet record from a transcode. The drop can: an
    # encoder's lowpass falls tens of decibels inside a few hundred hertz, and
    # nothing a band plays does that.
    # Measured below 21.5 kHz only. Everything here is resampled to a common
    # rate first, so a 44.1 kHz file — genuine or not — carries the resampler's
    # own edge at 22.05 kHz, and counting that would call every CD rip a fake.
    step = max(1, int(700 / (freqs[1] - freqs[0])))
    inner = np.where((freqs >= 11000) & (freqs <= 21500))[0]
    if inner.size > step:
        drops = power[inner[:-step]] - power[inner[step:]]
        wall = float(drops.max())
    else:
        wall = 0.0

    cutoff = float(freqs[idx[-1]])
    for pos in idx:
        if power[pos] < reference - 30 and (power[pos:idx[-1] + 1] < reference - 25).all():
            cutoff = float(freqs[pos])
            break
    return cutoff, wall


def unused_low_bits(path: str, seconds: int = 20) -> int | None:
    """How many bits at the bottom of each sample carry nothing.

    A 16-bit master padded out to 24 is sold as hi-res and is not: every sample
    ends in a run of zeros where the extra resolution should be. Counting them
    settles it, and it is the only test that can — the header says 24 either way.

    Read as a count rather than converted to a depth. ffmpeg lays a 24-bit
    sample near the top of a 32-bit word but not exactly at it: a genuine
    24-bit record here leaves seven low bits clear, not eight, which made a
    subtraction report it as 25-bit. What matters is the size of the run, and a
    padded file leaves roughly twice as many.

    Taken from the fifth percentile rather than the minimum, since a stray
    sample carrying one bit lower than the rest should not answer for the file.
    """
    proc = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-ss", "30", "-t", str(seconds), "-i", path,
         "-ac", "1", "-f", "s32le", "-"],
        capture_output=True)
    samples = np.frombuffer(proc.stdout, dtype=np.int32)
    samples = samples[samples != 0]
    if samples.size < 1000:
        return None
    values = np.abs(samples.astype(np.int64))
    trailing = np.zeros(values.size, dtype=np.int8)
    remaining = values.copy()
    for _ in range(32):
        still = (remaining & 1) == 0
        if not still.any():
            break
        trailing[still] += 1
        remaining[still] >>= 1
    return int(np.percentile(trailing, 5))


def audit_album(folder: str, sample: int = 3) -> dict:
    """One verdict for a whole folder of audio.

    Judged per album because that is the unit anyone keeps: a single odd track
    should not condemn a record, and a single good one should not redeem it. A
    sample is taken rather than the whole thing, and it says how many it looked at.
    """
    tracks = sorted(
        os.path.join(root, name)
        for root, _, names in os.walk(folder)
        for name in names
        if name.rsplit(".", 1)[-1].lower() in LOSSLESS_EXT | LOSSY_EXT
    )
    if not tracks:
        return {"tracks": 0, "verdict": "no audio"}

    # The longest tracks carry the most spectrum to judge, and a short intro or
    # a silent outro is the easiest way to mistake an honest file for a transcode.
    by_size = sorted(tracks, key=lambda p: os.path.getsize(p), reverse=True)
    chosen = by_size[:sample]

    codecs, depths, rates, bitrates, cutoffs, walls, real_depths = (
        [], [], [], [], [], [], [])
    for path in chosen:
        info = probe(path)
        if not info:
            continue
        codecs.append(info.get("codec_name", "?"))
        rates.append(int(info.get("sample_rate") or 0))
        claimed = info.get("bits_per_raw_sample") or info.get("bits_per_sample") or 0
        depths.append(int(claimed or 0))
        if info.get("bit_rate"):
            bitrates.append(int(info["bit_rate"]) // 1000)
        cut, wall = spectral_cutoff(path)
        if cut:
            cutoffs.append(cut)
            walls.append(wall)
        if int(claimed or 0) > 16:
            clear = unused_low_bits(path)
            if clear is not None:
                # Bounded by what the codec declares: the decoder's own layout
                # can leave one bit more than the sample really has, and no file
                # holds more resolution than its header claims.
                real_depths.append(min(int(claimed), 32 - clear))

    codec = max(set(codecs), key=codecs.count) if codecs else "?"
    lossless = codec in {"flac", "alac", "ape", "wavpack", "pcm_s16le", "pcm_s24le"}
    depth = max(depths) if depths else 0
    rate = max(rates) if rates else 0
    cutoff = float(np.median(cutoffs)) if cutoffs else None
    wall = float(np.median(walls)) if walls else 0.0
    honest_depth = max(real_depths) if real_depths else None

    # A lossless container holding a decoded MP3 is the one case worth naming
    # separately: it passes every check but the spectrum, and it is common.
    #
    # Two ways to catch it, because encoders differ. A constant 320 leaves a
    # wall — 57 dB here against 3 dB for a real 24/96 master, which is not a
    # judgement call. A V0 rolls off gently and leaves only 11 dB, so that one
    # is caught by where it stops instead: it ran out at 20.0 kHz where a
    # genuine rip of the same era still had content at 20.9 kHz.
    #
    # That second margin is under a kilohertz, so this will occasionally demote
    # an honest but dull master. The cost of being wrong that way is choosing
    # between two lossless copies; the cost the other way is an audiophile
    # keeping a decoded MP3 and never knowing. The numbers are reported either
    # way, so a person can overrule it.
    transcoded = bool(
        lossless and cutoff
        and (wall >= 25 or (cutoff < 20200 and wall >= 10))
    )
    padded = bool(depth > 16 and honest_depth is not None and honest_depth <= 16)

    return {
        "tracks": len(tracks),
        "sampled": len(chosen),
        "codec": codec,
        "lossless": lossless,
        "claimed_depth": depth,
        "real_depth": honest_depth,
        "sample_rate": rate,
        "bitrate": int(np.median(bitrates)) if bitrates else None,
        "cutoff_hz": int(cutoff) if cutoff else None,
        "wall_db": round(wall, 1),
        "transcoded": transcoded,
        "padded_depth": padded,
        "bytes": sum(os.path.getsize(t) for t in tracks),
    }


def score(audit: dict) -> tuple:
    """Rank a release. Higher wins, compared left to right.

    The ladder the library asked for: real hi-res, then lossless, then the best
    lossy available. A transcode and a padded depth are demoted to what they
    actually are rather than refused outright — a fake 24-bit FLAC is still a
    perfectly good 16-bit FLAC, and a transcode is still a copy of the record.
    """
    if audit.get("tracks", 0) == 0:
        return (-1,)
    lossless = audit.get("lossless") and not audit.get("transcoded")
    hires = bool(
        lossless
        and not audit.get("padded_depth")
        and ((audit.get("real_depth") or audit.get("claimed_depth") or 0) > 16
             or (audit.get("sample_rate") or 0) > 48000)
    )
    tier = 3 if hires else (2 if lossless else (1 if audit.get("lossless") else 0))
    return (tier,
            audit.get("sample_rate") or 0,
            audit.get("bitrate") or 0,
            audit.get("tracks", 0))


def describe(audit: dict) -> str:
    if audit.get("tracks", 0) == 0:
        return "no audio"
    bits = [audit.get("codec", "?")]
    depth = audit.get("real_depth") or audit.get("claimed_depth")
    if depth:
        bits.append(f"{depth}bit")
    if audit.get("sample_rate"):
        bits.append(f"{audit['sample_rate'] / 1000:g}kHz")
    if audit.get("bitrate"):
        bits.append(f"{audit['bitrate']}kbps")
    if audit.get("cutoff_hz"):
        bits.append(f"cutoff {audit['cutoff_hz'] / 1000:.1f}kHz")
    if audit.get("wall_db"):
        bits.append(f"wall {audit['wall_db']:.0f}dB")
    if audit.get("transcoded"):
        bits.append("TRANSCODED")
    if audit.get("padded_depth"):
        bits.append("FAKE 24bit")
    return " ".join(bits)


# ── the audition ──────────────────────────────────────────────────────────

def host_path(reported: str) -> str:
    return reported.replace(PATH_FROM, PATH_TO, 1) if reported.startswith(PATH_FROM) else reported


def incumbent(album_id: int) -> dict | None:
    """What the library already holds for this album, judged the same way.

    Without this the audition can only say which candidate is best, not whether
    any of them is worth having: handing Lidarr a copy no better than the one it
    imported last week costs a download and changes nothing.
    """
    try:
        files = lidarr(f"/trackfile?albumId={album_id}")
    except Exception:
        return None
    folders = {os.path.dirname(host_path(f["path"])) for f in files if f.get("path")}
    folders = {d for d in folders if os.path.isdir(d)}
    if not folders:
        return None
    best = None
    for folder in folders:
        audit = audit_album(folder)
        if audit.get("tracks") and (best is None or score(audit) > score(best)):
            best = audit
    return best


def find_album(args) -> dict:
    if args.album_id:
        album = lidarr(f"/album/{args.album_id}")
        return {"id": album["id"], "title": album["title"],
                "artist": (album.get("artist") or {}).get("artistName", ""),
                "mbid": album.get("foreignAlbumId")}
    artists = lidarr("/artist")
    match = next((a for a in artists
                  if a["artistName"].lower() == (args.artist or "").lower()), None)
    if not match:
        sys.exit(f"Lidarr has no artist called {args.artist!r}")
    wanted = (args.album or "").lower()
    album = next((a for a in lidarr(f"/album?artistId={match['id']}")
                  if a["title"].lower() == wanted), None)
    if not album:
        sys.exit(f"{match['artistName']} has no album called {args.album!r} in Lidarr")
    return {"id": album["id"], "title": album["title"],
            "artist": match["artistName"], "mbid": album.get("foreignAlbumId")}


def shortlist(album: dict, want: int, min_seeders: int) -> list[dict]:
    term = f"{album['artist']} {album['title']}"
    print(f"  searching {term!r}")
    results = prowlarr_search(term)
    print(f"  {len(results)} results\n")

    seen, candidates, dead = set(), [], 0
    for rel in sorted(results, key=lambda r: -(r.get("seeders") or 0)):
        title = rel.get("title") or ""
        key = re.sub(r"[^a-z0-9]+", "", title.lower())
        if key in seen:
            continue
        seen.add(key)
        # A release with nobody seeding it is not a candidate, it is a wish. It
        # would hold the audition open for the full timeout and arrive never.
        if (rel.get("seeders") or 0) < min_seeders:
            dead += 1
            continue
        raw, link = fetch_release(rel.get("downloadUrl") or rel.get("guid") or "")
        counts = torrent_extensions(raw)
        rel["_kind"], rel["_ext"] = classify(counts), counts
        rel["_raw"], rel["_link"] = raw, link
        rel["_hash"] = infohash(raw) if raw else magnet_hash(link)
        kind = rel["_kind"]
        candidates.append(rel)
        shown = counts if counts else "file list unavailable"
        print(f"    {kind:<9} seed={rel.get('seeders', 0):<4} {title[:58]}")
        print(f"              {shown}")

    # Lossless first, then the ones that would not say. A release proven lossy is
    # only worth downloading when nothing better is on offer.
    rank = {"lossless": 0, "unknown": 1, "lossy": 2, "empty": 3}
    candidates.sort(key=lambda r: (rank[r["_kind"]], -(r.get("seeders") or 0)))
    picked = [c for c in candidates if c["_kind"] != "empty"][:want]
    dropped = len(candidates) - len(picked)
    if dropped or dead:
        print(f"\n  auditioning {len(picked)}; {dropped} ranked out, "
              f"{dead} with fewer than {min_seeders} seeders")
    return picked


def wait_for(hashes: list[str], minutes: int) -> list[dict]:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        torrents = [t for t in qbt(f"/torrents/info?tag={TAG}")
                    if t["hash"].lower() in hashes]
        done = [t for t in torrents
                if t["state"] in ("uploading", "stalledUP", "pausedUP",
                                  "queuedUP", "forcedUP", "stoppedUP")
                or t.get("progress", 0) >= 1.0]
        pending = len(torrents) - len(done)
        left = int(deadline - time.time())
        print(f"\r  {len(done)}/{len(torrents)} complete, {pending} running, "
              f"{left // 60}m{left % 60:02d}s left   ", end="", flush=True)
        if torrents and not pending:
            print()
            return done
        time.sleep(10)
    print()
    return [t for t in qbt(f"/torrents/info?tag={TAG}")
            if t["hash"].lower() in hashes and t.get("progress", 0) >= 1.0]


def cleanup(hashes: list[str], keep: str | None) -> None:
    """Remove the losers, and only ever ours.

    Filtered against the live tagged list rather than trusting the caller's
    hashes: this call deletes files, so the one place that could delete the
    wrong thing checks first.
    """
    ours = {t["hash"].lower() for t in qbt(f"/torrents/info?tag={TAG}")}
    keep = (keep or "").lower()
    doomed = [h for h in hashes if h in ours and h != keep]
    if not doomed:
        return
    qbt("/torrents/delete", {"hashes": "|".join(doomed), "deleteFiles": "true"})
    print(f"  {len(doomed)} losers removed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--album-id", type=int, help="Lidarr album id")
    ap.add_argument("--artist", help="artist name, if no album id")
    ap.add_argument("--album", help="album title, if no album id")
    ap.add_argument("--candidates", type=int, default=4,
                    help="how many releases to download and compare (default 4)")
    ap.add_argument("--download", action="store_true",
                    help="actually download; without it only the free file-list pass runs")
    ap.add_argument("--wait", type=int, default=45, help="minutes to wait (default 45)")
    ap.add_argument("--min-seeders", type=int, default=1,
                    help="ignore releases with fewer seeders than this (default 1)")
    ap.add_argument("--keep-losers", action="store_true",
                    help="leave the losing downloads in place instead of deleting them")
    args = ap.parse_args()
    if not args.album_id and not (args.artist and args.album):
        ap.error("give --album-id, or both --artist and --album")

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"needs {tool}")

    album = find_album(args)
    print(f"\n  {album['artist']} — {album['title']}\n")

    picked = shortlist(album, args.candidates, args.min_seeders)
    if not picked:
        sys.exit("  nothing worth auditioning")

    if not args.download:
        print("\n  free pass: nothing was downloaded. --download to audition for real")
        return

    print()
    qbt("/torrents/createTags", {"tags": TAG})
    fields = {"category": TAG, "tags": TAG,
              "savepath": f"{PATH_FROM}/torrents/{TAG}", "paused": "false"}
    # Added one at a time so the client's own list says which torrent each
    # release became. An indexer link can be a torrent, a redirect to a magnet,
    # or a page that refuses to be read at all, and only some of those yield an
    # id up front — but every one of them lands under this tag and nothing else
    # does, so the arrival itself is the identification.
    added: list[str] = []
    for rel in picked:
        url = rel.get("_link") or rel.get("downloadUrl") or rel.get("guid") or ""
        title = rel.get("title", "")[:52]
        # Against the whole list, not the tagged one: a torrent shows up before
        # its tag is applied, so a tag-filtered read can miss the very thing it
        # was asked about and report a release that started fine as never
        # started.
        before = {t["hash"].lower() for t in qbt("/torrents/info")}
        try:
            qbt_add(rel.get("_raw"), url, fields)
        except urllib.error.HTTPError as exc:
            # 409 means the client already holds this exact torrent, usually
            # Lidarr's own copy. Nothing to audition and nothing wrong: what it
            # downloaded is judged as the incumbent further down.
            print(f"  {'already held:' if exc.code == 409 else 'refused:     '} {title}")
            continue
        except Exception as exc:
            print(f"  refused:      {title} ({type(exc).__name__})")
            continue

        # The client answers before the torrent exists, and a magnet has to find
        # its metadata on the network first.
        arrived = None
        for _ in range(12):
            time.sleep(5)
            live = {t["hash"].lower() for t in qbt("/torrents/info")}
            fresh = live - before
            if rel.get("_hash") and rel["_hash"] in live:
                arrived = rel["_hash"]
                break
            if fresh:
                arrived = fresh.pop()
                break
        if arrived:
            added.append(arrived)
            print(f"  added:        {title}")
        else:
            print(f"  never started: {title}")

    if not added:
        sys.exit("  qBittorrent started none of them")

    print()
    done = wait_for(added, args.wait)
    if not done:
        print("  none finished in time; they are still going and nothing was deleted")
        return

    print()
    results = []
    for torrent in done:
        folder = host_path(torrent.get("content_path") or torrent.get("save_path", ""))
        if not os.path.exists(folder):
            print(f"  cannot find {folder}")
            continue
        audit = audit_album(folder)
        results.append({"torrent": torrent, "audit": audit, "score": score(audit)})
        print(f"  {torrent['name'][:56]}")
        print(f"      {describe(audit)}  ({audit.get('sampled', 0)} de "
              f"{audit.get('tracks', 0)} tracks sampled)")

    results = [r for r in results if r["score"][0] >= 0]
    if not results:
        print("\n  nothing could be analysed")
        return

    results.sort(key=lambda r: r["score"], reverse=True)
    winner = results[0]
    print(f"\n  best candidate: {winner['torrent']['name'][:56]}")
    print(f"      {describe(winner['audit'])}")

    have = incumbent(album["id"])
    if have:
        print(f"  already held:   {describe(have)}")
        if score(have) >= winner["score"]:
            print("\n  nothing on offer beats what the library already has")
            if not args.keep_losers:
                cleanup(added, None)
            return

    print("\n  WINNER: this is an upgrade")
    if not args.keep_losers:
        cleanup(added, winner["torrent"]["hash"].lower())

    # Handing it over rather than importing it here: Lidarr owns naming, the
    # root folder and the file it replaces, and it already does all of that.
    qbt("/torrents/setCategory",
        {"hashes": winner["torrent"]["hash"], "category": LIDARR_CATEGORY})
    print(f"  handed to Lidarr (category {LIDARR_CATEGORY})")


if __name__ == "__main__":
    main()
