"""What a cue sheet claims a track is called, and what it actually is.

A cue sheet is not metadata, it is whatever the person who ripped the disc
typed. One here filled every per-track TITLE with the line they had copied out
of a tracklist — number, name and running time in a single string:

    TRACK 01 AUDIO
      TITLE "1. Kong @ the Gates 1:24"

Split from that cue, every file carried `1. Kong @ the Gates 1:24` as its title
tag, and Navidrome showed exactly that. The file *names* were right, because
those came from Lidarr and MusicBrainz; only the tags were wrong, which is the
half a listener actually sees.

The stripping is deliberately timid. A leading number is only removed when it
is the track's own number, so a record that really is called "7 Seconds" on
track three keeps its name. A trailing time is only removed when something is
left afterwards. Neither rule ever empties a title.
"""

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


cue = load("split_cue", "tools/split-cue.py")


def sheet(*entries: tuple[int, str]) -> str:
    """A minimal cue sheet naming the given tracks."""
    head = 'PERFORMER "A Band"\nTITLE "A Record"\nFILE "image.wav" WAVE\n'
    body = ""
    for number, title in entries:
        body += (f'  TRACK {number:02d} AUDIO\n'
                 f'    TITLE "{title}"\n'
                 f'    INDEX 01 {number:02d}:00:00\n')
    return head + body


def titles(text: str) -> list[str]:
    return [track["title"] for track in cue.parse(text)[2]]


# ── the sloppy tracklist ──────────────────────────────────────────────────

def test_the_number_and_running_time_are_not_part_of_the_name():
    """The bug that shipped, in the shape it shipped in."""
    assert titles(sheet((1, "1. Kong @ the Gates 1:24"))) == ["Kong @ the Gates"]


def test_a_two_digit_track_is_stripped_the_same_way():
    assert titles(sheet((10, "10. Scarecrow Man 3:12"))) == ["Scarecrow Man"]


@pytest.mark.parametrize("written", [
    "1. Witch Hunt 1:33",
    "1 - Witch Hunt 1:33",
    "1) Witch Hunt 1:33",
    "01. Witch Hunt 1:33",
])
def test_the_separators_people_actually_type(written):
    assert titles(sheet((1, written))) == ["Witch Hunt"]


def test_a_clean_sheet_is_left_exactly_alone():
    assert titles(sheet((1, "Witch Hunt"), (2, "Them"))) == ["Witch Hunt", "Them"]


# ── what must never be stripped ───────────────────────────────────────────

def test_a_number_that_is_not_this_track_stays():
    """Track three is not called "7." — so that 7 belongs to the title.

    Without this guard the rule is just "drop a leading number", and a record
    whose name opens with one loses it.
    """
    assert titles(sheet((3, "7. Something"))) == ["7. Something"]


def test_a_name_that_only_looks_like_a_time_survives():
    assert titles(sheet((1, "4:33"))) == ["4:33"]


def test_a_title_is_never_emptied():
    """Stripping both halves of "1. 2:34" would leave nothing at all."""
    assert titles(sheet((1, "1. 2:34"))) == ["1. 2:34"]


def test_a_time_in_the_middle_is_part_of_the_name():
    assert titles(sheet((1, "1. 9:12 in the Morning"))) == ["9:12 in the Morning"]


def test_an_untitled_track_stays_untitled():
    assert titles(sheet((1, ""))) == [""]


# ── everything else parse() promises is unchanged ─────────────────────────

def test_the_record_itself_is_still_read():
    artist, album, tracks = cue.parse(sheet((1, "1. Anything 2:00")))
    assert (artist, album, len(tracks)) == ("A Band", "A Record", 1)


def test_the_offsets_are_untouched_by_any_of_this():
    """Frames are seventy-fifths of a second, and stripping text must not
    disturb the arithmetic that decides where the knife falls."""
    tracks = cue.parse(
        'PERFORMER "A Band"\nTITLE "A Record"\nFILE "image.wav" WAVE\n'
        '  TRACK 01 AUDIO\n    TITLE "1. First 1:00"\n    INDEX 01 00:00:00\n'
        '  TRACK 02 AUDIO\n    TITLE "2. Second 2:00"\n    INDEX 01 01:23:45\n'
    )[2]
    assert tracks[0]["start"] == 0.0
    assert tracks[1]["start"] == pytest.approx(60 + 23 + 45 / 75.0)
