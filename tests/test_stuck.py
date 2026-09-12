"""Judging a download that finished and still will not import.

reap_stalled() in bridge.py will not touch these on purpose — a completed
download that fails to import is a different failure with a different cause,
and guessing at it there would delete files somebody may still want. That
correctness leaves a gap: nothing else ever looks at what is sitting at
importFailed, so three albums did exactly that for months here, unnoticed,
because Lidarr's own onImportFailure alert had already fired once and gone
quiet. stuck-imports.py is the periodic look that decision left out — it
never deletes or blocklists anything, only reads, classifies, and reports.
"""

import importlib.util
import os
import sys
import time

import pytest

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


stuck = load("stuck_imports", "tools/stuck-imports.py")


def stuck_item(download_id, artist, album, messages=None, output_path="/data/torrents/x"):
    return {
        "id": 1,
        "downloadId": download_id,
        "trackedDownloadState": "importFailed",
        "outputPath": output_path,
        "artist": {"artistName": artist},
        "album": {"title": album},
        "statusMessages": [{"title": album, "messages": messages or ["Has missing tracks"]}],
    }


class FakeLidarr:
    """Lidarr's queue, and the ledger stuck-imports.py wrote to it."""

    def __init__(self, records):
        self.records = records
        self.saved = {}

    def install(self, monkeypatch, hours=6):
        monkeypatch.setattr(stuck, "STUCK_HOURS", hours)
        monkeypatch.setattr(stuck, "lidarr", lambda path: {"records": self.records})
        monkeypatch.setattr(stuck, "load_ledger", lambda: dict(self.saved))
        monkeypatch.setattr(stuck, "save_ledger", lambda data: self.saved.update(data) or
                            [self.saved.pop(k) for k in set(self.saved) - set(data)])


def age(fake, hours):
    for entry in fake.saved.values():
        entry["first_seen"] = time.time() - hours * 3600


class FakeProwlarr:
    """Prowlarr's indexer status, and the ledger stuck-imports.py wrote about it."""

    def __init__(self, down=None, total=None):
        self.down = [] if down is None else down
        self.total = total
        self.saved = {}

    def install(self, monkeypatch, hours=6, outage_minutes=30, key="prowlarr-key"):
        monkeypatch.setattr(stuck, "INDEXER_HOURS", hours)
        monkeypatch.setattr(stuck, "INDEXER_OUTAGE_MINUTES", outage_minutes)
        monkeypatch.setattr(stuck.indexers, "PROWLARR_KEY", key)
        monkeypatch.setattr(stuck.indexers, "indexers_down", lambda: self.down)
        monkeypatch.setattr(stuck.indexers, "indexer_count", lambda: self.total)
        monkeypatch.setattr(stuck, "load_indexer_ledger", lambda: dict(self.saved))
        monkeypatch.setattr(stuck, "save_indexer_ledger", lambda data: self.saved.update(data) or
                            [self.saved.pop(k) for k in set(self.saved) - set(data)])


# ── the ledger: first sighting, threshold, drop and recurrence ────────────

def test_nothing_is_reported_on_the_first_sighting(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch)
    outcome = stuck.report_stuck()
    assert outcome["reportable"] == []
    assert "A" in fake.saved and fake.saved["A"]["notified"] is False


def test_an_item_stuck_past_the_threshold_is_reported(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    stuck.report_stuck()
    age(fake, 7)
    outcome = stuck.report_stuck()
    assert [r["label"] for r in outcome["reportable"]] == ["Delirium — Abismo"]


def test_an_item_short_of_the_threshold_is_not_yet_reported(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    stuck.report_stuck()
    age(fake, 2)
    assert stuck.report_stuck()["reportable"] == []


def test_the_all_flag_reports_even_a_fresh_sighting(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    outcome = stuck.report_stuck(all_items=True)
    assert [r["label"] for r in outcome["reportable"]] == ["Delirium — Abismo"]


def test_records_that_are_not_import_failed_are_ignored(monkeypatch):
    downloading = stuck_item("A", "Delirium", "Abismo")
    downloading["trackedDownloadState"] = "downloading"
    fake = FakeLidarr([downloading])
    fake.install(monkeypatch, hours=6)
    outcome = stuck.report_stuck(all_items=True)
    assert outcome["reportable"] == []
    assert fake.saved == {}


def test_an_item_that_leaves_the_queue_is_dropped_from_the_ledger(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    stuck.report_stuck()
    assert "A" in fake.saved
    fake.records = []  # resolved, or fixed by hand — either way it is gone
    stuck.report_stuck()
    assert "A" not in fake.saved


def test_a_recurrence_notifies_again(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda title, body, topic: pushed.append(title))

    stuck.report_stuck()
    age(fake, 7)
    stuck.report_stuck(notify=True)
    assert len(pushed) == 1

    fake.records = []
    stuck.report_stuck()
    assert fake.saved == {}

    fake.records = [stuck_item("A", "Delirium", "Abismo")]
    stuck.report_stuck()
    age(fake, 7)
    stuck.report_stuck(notify=True)
    assert len(pushed) == 2


# ── notification: at most once per download id, failures never corrupt it ──

def test_an_item_is_not_notified_twice(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda title, body, topic: pushed.append(title))

    stuck.report_stuck()
    age(fake, 7)
    stuck.report_stuck(notify=True)
    assert len(pushed) == 1
    # Still stuck, still --notify, nothing new: a thirty-minute timer must
    # not nag about a download it already told somebody about.
    stuck.report_stuck(notify=True)
    assert len(pushed) == 1


def test_the_notification_names_the_total_stuck_count(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo"),
                       stuck_item("B", "Spasm", "Rise", output_path="/data/torrents/y")])
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda title, body, topic: pushed.append(title))

    stuck.report_stuck()
    age(fake, 7)
    stuck.report_stuck(notify=True)
    assert pushed == ["2 import(s) stuck"]


def test_notify_without_a_resolved_topic_just_prints(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: None)
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda *a: pushed.append(a))

    stuck.report_stuck()
    age(fake, 7)
    outcome = stuck.report_stuck(notify=True)
    assert outcome["pushed"] is False
    assert pushed == []


def test_a_notification_failure_does_not_crash_the_run_or_corrupt_the_ledger(monkeypatch):
    fake = FakeLidarr([stuck_item("A", "Delirium", "Abismo")])
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")

    def boom(title, body, topic):
        raise OSError("ntfy.sh unreachable")

    monkeypatch.setattr(stuck, "push", boom)
    stuck.report_stuck()
    age(fake, 7)
    outcome = stuck.report_stuck(notify=True)  # must not raise
    assert outcome["pushed"] is False
    assert outcome["push_error"]
    # Never marked notified after a failed send — the next run gets to try
    # again instead of silently losing the one chance to say anything.
    assert fake.saved["A"]["notified"] is False
    assert fake.saved["A"]["first_seen"]


# ── the diagnosis: three real causes, and a fallback for everything else ──

DIAGNOSES = [
    # Cue image: exactly one audio file plus a cue sheet. Lidarr imports per
    # track and can never take this, however long it sits in the queue.
    (["Album match is not close enough: 71.4 % vs 80 % [tracks, missing tracks]"],
     ["Sunken Norwegian.flac", "Sunken Norwegian.cue"], "cue-image"),
    # The same message, but a sane number of files: the files are fine, the
    # 80% album-match gate is what is actually wrong.
    (["Album match is not close enough: 71.4 % vs 80 % [tracks, missing tracks]"],
     [f"{n:02d}.flac" for n in range(1, 13)], "match-score"),
    # An explicit statement that most of the release never arrived.
    (["One or more tracks expected in this release were not imported or "
      "missing from the release"], ["01.flac"], "incomplete-release"),
    # The short form of the same statement.
    (["Has missing tracks"], ["01.flac"], "incomplete-release"),
    # Nothing recognisable: reported as-is, never guessed at.
    (["some brand-new Lidarr message nobody here has seen before"],
     ["01.flac", "02.flac"], "unclassified"),
]


@pytest.mark.parametrize("messages,files,expected", DIAGNOSES)
def test_the_diagnosis_matches_the_real_failure(messages, files, expected):
    assert stuck.diagnose(messages, files)[0] == expected


def test_an_unreadable_file_list_falls_back_to_the_status_message():
    # files is None (outputPath could not be listed), not empty — a cue image
    # degrades to being read off its score message rather than going
    # unclassified or raising.
    kind, _ = stuck.diagnose(
        ["Album match is not close enough: 71.4 % vs 80 % [tracks, missing tracks]"], None)
    assert kind == "match-score"


def test_the_cue_image_fix_points_at_the_repos_own_tool():
    kind, detail = stuck.diagnose(
        ["Album match is not close enough: 71.4 % vs 80 % [tracks, missing tracks]"],
        ["Sunken Norwegian.flac", "Sunken Norwegian.cue"],
        "/mnt/data/torrents/Sunken Norwegian")
    assert kind == "cue-image"
    assert "tools/split-cue.py" in detail
    assert "/mnt/data/torrents/Sunken Norwegian" in detail


def test_the_match_score_percentages_are_reported():
    _, detail = stuck.diagnose(
        ["Album match is not close enough: 71.4 % vs 80 % [tracks, missing tracks]"],
        [f"{n:02d}.flac" for n in range(1, 13)])
    assert "71.4" in detail and "80" in detail


# ── translating a container path to wherever the host actually sees it ────

def test_the_download_path_is_translated_from_the_container_to_the_host(monkeypatch):
    # Lidarr and qBittorrent both mount the download tree at /data; a process
    # running on the host needs DATA_DIR, the same variable docker-compose.yml
    # already requires, to find the identical files under their real path.
    monkeypatch.setattr(stuck, "DATA_DIR", "/mnt/servarr/data")
    assert (stuck.host_path("/data/torrents/Delirium - Abismo")
            == "/mnt/servarr/data/torrents/Delirium - Abismo")


def test_a_path_outside_data_dir_is_left_alone(monkeypatch):
    monkeypatch.setattr(stuck, "DATA_DIR", "/mnt/servarr/data")
    assert stuck.host_path("/config/something") == "/config/something"


def test_with_no_data_dir_configured_the_path_is_used_as_is(monkeypatch):
    monkeypatch.setattr(stuck, "DATA_DIR", "")
    assert stuck.host_path("/data/torrents/x") == "/data/torrents/x"


# ── the ntfy topic and the push itself ─────────────────────────────────────

def test_the_ntfy_topic_env_var_wins_over_dconf(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "from-env")
    calls = []
    monkeypatch.setattr(stuck.subprocess, "run", lambda *a, **k: calls.append(a))
    assert stuck.ntfy_topic() == "from-env"
    assert calls == []


def test_the_dconf_reply_has_its_quotes_stripped(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    class Result:
        stdout = "'my-topic'\n"

    monkeypatch.setattr(stuck.subprocess, "run", lambda *a, **k: Result())
    assert stuck.ntfy_topic() == "my-topic"


def test_the_push_uses_the_agreed_priority_and_tag(monkeypatch):
    sent = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=10):
        sent["url"] = req.full_url
        sent["priority"] = req.get_header("Priority")
        sent["tags"] = req.get_header("Tags")
        sent["title"] = req.get_header("Title")
        return FakeResponse()

    monkeypatch.setattr(stuck.urllib.request, "urlopen", fake_urlopen)
    stuck.push("3 import(s) stuck", "Delirium — Abismo: split it", "my-topic")
    assert sent["url"] == "https://ntfy.sh/my-topic"
    # ntfy's top priority buzzes the phone with a long, insistent pattern —
    # that is the power-outage alert on this same topic, at priority 4/high,
    # and this must never compete with it.
    assert sent["priority"] == "3"
    assert sent["tags"] == "cd"
    assert sent["title"] == "3 import(s) stuck"


# ── indexer health: the same ledger discipline, Prowlarr instead of Lidarr ─
#
# Lidarr's own OnHealthIssue ntfy notification is fire-and-forget: it fires
# once, at the moment of the event, and a send that fails right then — often
# because whatever tripped the indexer also broke the network for a moment —
# is gone forever. Riding this tool's own poll instead means a failed push
# just gets tried again next cycle. indexers_down() returning None (Prowlarr
# could not be asked) must never be read as "every indexer is fine" — that
# exact mistake once let sixty-seven searches disable every indexer here.

def test_indexers_down_returning_none_is_not_read_as_all_healthy(monkeypatch):
    fake = FakeProwlarr()
    fake.install(monkeypatch)
    fake.saved = {"Knaben": {"first_seen": time.time() - 7 * 3600, "notified": False}}
    before = dict(fake.saved)
    monkeypatch.setattr(stuck.indexers, "indexers_down", lambda: None)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda title, body, topic: pushed.append(title))

    outcome = stuck.report_indexer_health(notify=True)

    assert outcome["status"] == "unreachable"
    assert outcome["reportable"] == []
    assert pushed == []
    assert fake.saved == before  # left exactly as it was, never cleared


def test_an_indexer_down_under_the_threshold_is_not_yet_reported(monkeypatch):
    fake = FakeProwlarr(down=["Knaben"], total=5)
    fake.install(monkeypatch, hours=6)
    stuck.report_indexer_health()
    age(fake, 2)
    outcome = stuck.report_indexer_health()
    assert outcome["reportable"] == []
    assert outcome["total_outage"] is False


def test_an_indexer_down_past_indexer_hours_is_reported(monkeypatch):
    fake = FakeProwlarr(down=["Knaben"], total=5)
    fake.install(monkeypatch, hours=6)
    stuck.report_indexer_health()
    age(fake, 7)
    outcome = stuck.report_indexer_health()
    assert [r["name"] for r in outcome["reportable"]] == ["Knaben"]
    assert outcome["total_outage"] is False


def test_every_indexer_down_triggers_the_short_fuse_outage_path_and_says_so(monkeypatch):
    fake = FakeProwlarr(down=["The Pirate Bay", "LimeTorrents", "Knaben"], total=3)
    fake.install(monkeypatch, hours=6, outage_minutes=30)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda title, body, topic: pushed.append(title))

    stuck.report_indexer_health()
    age(fake, 1)  # one hour: past the 30-minute outage fuse, nowhere near 6h
    outcome = stuck.report_indexer_health(notify=True)

    assert outcome["total_outage"] is True
    assert {r["name"] for r in outcome["reportable"]} == {
        "The Pirate Bay", "LimeTorrents", "Knaben"}
    assert len(pushed) == 1
    assert "outage" in pushed[0].lower()


def test_a_recovered_indexer_is_dropped_and_notifies_again_if_it_returns(monkeypatch):
    fake = FakeProwlarr(down=["Knaben"], total=5)
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda title, body, topic: pushed.append(title))

    stuck.report_indexer_health()
    age(fake, 7)
    stuck.report_indexer_health(notify=True)
    assert len(pushed) == 1

    fake.down = []
    stuck.report_indexer_health()
    assert fake.saved == {}

    fake.down = ["Knaben"]
    stuck.report_indexer_health()
    age(fake, 7)
    stuck.report_indexer_health(notify=True)
    assert len(pushed) == 2


def test_a_failed_indexer_push_leaves_everything_unnotified(monkeypatch):
    fake = FakeProwlarr(down=["Knaben"], total=5)
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")

    def boom(title, body, topic):
        raise OSError("ntfy.sh unreachable")

    monkeypatch.setattr(stuck, "push", boom)
    stuck.report_indexer_health()
    age(fake, 7)
    outcome = stuck.report_indexer_health(notify=True)
    assert outcome["pushed"] is False
    assert outcome["push_error"]
    # Never marked notified after a failed send — the next run gets to try
    # again instead of silently losing the one chance to say anything.
    assert fake.saved["Knaben"]["notified"] is False


def test_indexer_health_is_not_notified_twice(monkeypatch):
    fake = FakeProwlarr(down=["Knaben"], total=5)
    fake.install(monkeypatch, hours=6)
    monkeypatch.setattr(stuck, "ntfy_topic", lambda: "topic")
    pushed = []
    monkeypatch.setattr(stuck, "push", lambda title, body, topic: pushed.append(title))

    stuck.report_indexer_health()
    age(fake, 7)
    stuck.report_indexer_health(notify=True)
    assert len(pushed) == 1
    stuck.report_indexer_health(notify=True)
    assert len(pushed) == 1


def test_with_no_prowlarr_key_the_indexer_section_is_skipped_not_errored(monkeypatch):
    fake = FakeProwlarr(down=["Knaben"], total=5)
    fake.install(monkeypatch, key="")
    outcome = stuck.report_indexer_health()
    assert outcome["status"] == "no-key"
    assert outcome["reportable"] == []
