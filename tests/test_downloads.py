"""Judging a download: when it is stuck, and what it contains.

Two of these encode bugs that shipped and had to be found by watching the thing
run. `size == 0 and sizeleft == 0` is a torrent that never learned what it was
supposed to fetch, not a finished one — reading the remaining bytes alone made
the reaper blind to exactly the failure it was written for. And an empty search
with one indexer out is still an answer; treating it as unasked meant a queue
that could never settle.
"""

import importlib.util
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bridge  # noqa: E402


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


best = load("best_release", "tools/best-release.py")
queue = load("audit_queue", "tools/audit-queue.py")


# ── the reaper ────────────────────────────────────────────────────────────

class FakeQueue:
    """Lidarr's queue, and a record of what was removed from it."""

    def __init__(self, records):
        self.records = records
        self.removed = []
        self.saved = {}

    def install(self, monkeypatch, hours=6):
        monkeypatch.setattr(bridge, "STALLED_HOURS", hours)
        monkeypatch.setattr(bridge, "lidarr_get", lambda path: {"records": self.records})
        monkeypatch.setattr(bridge, "_delete", lambda url, headers=None: self.removed.append(url))
        monkeypatch.setattr(bridge, "_load", lambda path, default: dict(self.saved))
        monkeypatch.setattr(bridge, "_save", lambda path, data: self.saved.update(data) or
                            [self.saved.pop(k) for k in set(self.saved) - set(data)])


def item(ident, download_id, size, left, title):
    return {"id": ident, "downloadId": download_id, "size": size,
            "sizeleft": left, "album": {"title": title}}


def age(fake, hours):
    for entry in fake.saved.values():
        entry["since"] = time.time() - hours * 3600


def test_nothing_is_dropped_on_the_first_look(monkeypatch):
    fake = FakeQueue([item(1, "A", 900, 900, "Stuck")])
    fake.install(monkeypatch)
    assert bridge.reap_stalled()["reaped"] == []
    assert fake.removed == []


def test_a_download_that_moved_is_left_alone(monkeypatch):
    fake = FakeQueue([item(1, "A", 500, 400, "Crawling")])
    fake.install(monkeypatch)
    bridge.reap_stalled()
    age(fake, 7)
    # Slow is not stuck. Measured on a real one: 17.8% to 19.3% in two hours.
    fake.records[0]["sizeleft"] = 100
    assert bridge.reap_stalled()["reaped"] == []


def test_a_download_that_never_moved_is_dropped(monkeypatch):
    fake = FakeQueue([item(1, "A", 900, 900, "Stuck")])
    fake.install(monkeypatch)
    bridge.reap_stalled()
    age(fake, 7)
    reaped = bridge.reap_stalled()["reaped"]
    assert [r["album"] for r in reaped] == ["Stuck"]
    # Blocklisted so the same dead copy is not grabbed again, and searched for
    # afresh so the album stays wanted.
    assert "blocklist=true" in fake.removed[0]
    assert "skipRedownload=false" in fake.removed[0]


def test_a_torrent_with_no_metadata_is_dropped(monkeypatch):
    # size and sizeleft both zero. Reading the remaining bytes alone called this
    # finished and skipped it forever — and five of these sat here for up to
    # twenty-five hours, which is the reason the reaper exists.
    fake = FakeQueue([item(1, "A", 0, 0, "No metadata")])
    fake.install(monkeypatch)
    bridge.reap_stalled()
    age(fake, 7)
    assert [r["album"] for r in bridge.reap_stalled()["reaped"]] == ["No metadata"]


def test_a_finished_download_is_never_touched(monkeypatch):
    # A finished download that will not import is a different failure with a
    # different cause. Guessing at it here would delete files somebody wants.
    fake = FakeQueue([item(1, "A", 700, 0, "Done")])
    fake.install(monkeypatch)
    bridge.reap_stalled()
    age(fake, 99)
    assert bridge.reap_stalled()["reaped"] == []
    assert fake.removed == []


def test_a_torrent_that_learns_its_size_gets_its_clock_reset(monkeypatch):
    fake = FakeQueue([item(1, "A", 0, 0, "Waking up")])
    fake.install(monkeypatch)
    bridge.reap_stalled()
    age(fake, 5)
    fake.records[0].update(size=800, sizeleft=800)
    assert bridge.reap_stalled()["reaped"] == []


def test_zero_hours_turns_it_off(monkeypatch):
    fake = FakeQueue([item(1, "A", 900, 900, "Stuck")])
    fake.install(monkeypatch, hours=0)
    outcome = bridge.reap_stalled()
    assert outcome["disabled"] and outcome["reaped"] == []


# ── what a release contains ───────────────────────────────────────────────

CONTENTS = [
    (["01.flac", "02.flac", "cover.jpg"], "lossless"),
    (["01.mp3", "02.mp3"], "lossy"),
    (["01.ape"], "lossless"),
    (["01.m4a"], "lossless"),
    (["readme.txt", "cover.jpg"], "empty"),
    ([], "empty"),
    (None, "unknown"),
]


@pytest.mark.parametrize("names,expected", CONTENTS)
def test_what_a_file_list_says_a_release_is(names, expected):
    assert best.classify(best.extensions(names)) == expected


def test_an_unreadable_file_list_is_unknown_not_empty():
    # These mean opposite things: nothing could be read, against it was read and
    # holds no audio. One leaves a release in the running; the other rules it out.
    assert best.extensions(None) is None
    assert best.classify(None) == "unknown"
    assert best.classify(best.extensions([])) == "empty"


MAGNETS = [
    ("magnet:?xt=urn:btih:F6BC60FDD7C7A4428D5F465BCBDB018F4AC8EF90&dn=x",
     "f6bc60fdd7c7a4428d5f465bcbdb018f4ac8ef90"),
    ("http://localhost:9696/4/download?apikey=x&link=y", None),
    ("", None),
]


@pytest.mark.parametrize("url,expected", MAGNETS)
def test_the_id_is_read_out_of_the_magnet(url, expected):
    # Without this there is no id, and a release with no id cannot be opened —
    # which left the audition judging by name, the one thing it exists to avoid.
    assert best.magnet_hash(url) == expected


# ── what an audition concluded ────────────────────────────────────────────

VERDICTS = [
    ("NOTHING WAS SEARCHED: every indexer is disabled", "unanswered"),
    ("  1 proven lossless, wanted 4", "found-not-taken"),
    ("  no lossless copy found in 5 release(s)", "no-lossless"),
    ("  nothing worth auditioning", "no-candidates"),
    ("", "unclear"),
]


@pytest.mark.parametrize("output,expected", VERDICTS)
def test_what_the_queue_writes_down(output, expected):
    assert queue.read_verdict(output)[0] == expected


def test_not_having_asked_is_never_recorded_as_a_verdict():
    # An indexer that is not being asked cannot tell you the release is not
    # there. Filed as unanswered, the album comes back around instead of being
    # taken as settled.
    output = ("NOTHING WAS SEARCHED: every indexer is disabled by Prowlarr.\n"
              "  no lossless copy found in 0 release(s)\n")
    assert queue.read_verdict(output)[0] == "unanswered"
