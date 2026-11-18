"""The audition: which files it takes, what it hands over, and what it leaves.

Each of these encodes something that went wrong on the real library. A live
single was "upgraded" with the studio take of the same song, twice, because the
title was folded down to the song's name before anything was compared. A
singles compilation picked out of a discography was handed to Lidarr, which
read the torrent's name, filed it under three other albums and never imported
it. And fifty-six torrents, 13.9 GB, sat in the audition's folder because the
run that added them was stopped before it could clean up.
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load(name: str, path: str):
    """Import a tool whose file name is not a Python identifier."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    module = importlib.util.module_from_spec(spec)
    saved, sys.argv = sys.argv, [name]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = saved
    return module


best = load("best_release_audition", "tools/best-release.py")

RAMMSTEIN = [
    "Rammstein - Discography (1995-2022)/2019 - Rammstein/01.- Deutschland.flac",
    "Rammstein - Discography (1995-2022)/2022 - Zeit/01.- Armee Der Tristen.flac",
    "Rammstein - Discography (1995-2022)/2022 - Zeit/02.- Zeit.flac",
]


# ── which files an audition takes ─────────────────────────────────────────

def test_a_live_single_does_not_take_the_studio_take_of_the_same_song():
    assert best.wanted_indexes(RAMMSTEIN, "Armee der Tristen (live in Mexico City)") == []


def test_a_live_single_does_not_take_one_track_off_the_studio_album():
    names = ["Twenty One Pilots - Breach [2025]/01. City Walls.flac",
             "Twenty One Pilots - Breach [2025]/02. RAWFEAR.flac",
             "Twenty One Pilots - Breach [2025]/03. Drum Show.flac"]
    assert best.wanted_indexes(names, "RAWFEAR (Live From The Bellwether)") == []


def test_a_live_single_still_finds_its_live_recording():
    names = RAMMSTEIN + [
        "Rammstein - Discography (1995-2022)/Singles/Armee der Tristen (Live in Mexico City).flac"]
    assert best.wanted_indexes(names, "Armee der Tristen (live in Mexico City)") == [3]


def test_a_title_without_a_qualifier_still_finds_its_album_in_a_discography():
    assert best.wanted_indexes(RAMMSTEIN, "Zeit") == [1, 2]
