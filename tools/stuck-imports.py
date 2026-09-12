#!/usr/bin/env python3
"""Report the imports Lidarr has given up on and will never retry by itself.

A download can finish completely — every byte fetched, nothing left to pull —
and still fail to import. Lidarr marks the queue record `completed` /
`importFailed` and leaves it exactly there forever; nothing about that state
is transient. reap_stalled() in bridge.py deliberately will not touch it
either: a finished download that will not import is a different failure with
a different cause than a stalled one, and guessing at it there would delete
files somebody may still want. That decision is correct, and this tool exists
because of the gap it leaves open.

Lidarr's own onImportFailure notification fires once, at the moment of
failure, and says nothing about what is still sitting there afterwards.
Three albums did exactly that on this system — stuck at importFailed for
months, invisible, because the one alert that could have named them had
already fired and gone quiet by the time anyone looked.

So this reads the queue on a timer, keeps a ledger of what it has already
seen and already said, and reports only what has been stuck for STUCK_HOURS —
long enough that a timer running every thirty minutes does not shout about an
import Lidarr might still sort out on its own. It never deletes, blocklists,
or otherwise changes anything Lidarr is holding: read, classify, print, and
optionally push a notification is the entire job.

The diagnosis is the point of writing this at all. Three causes account for
every real case seen here, and each has a different fix:

  * a single audio file plus a .cue sheet — Lidarr wants one file per track
    and can never import this. Split it: tools/split-cue.py
  * a release Lidarr's 80% album-match gate is blocking even though the files
    are fine — switch to the release whose track count matches, or force a
    manual import with explicit trackIds to bypass the gate
  * a release genuinely missing most of its tracks — there is nothing to
    import; blocklist it and search again

Anything else is reported with Lidarr's own status message rather than a
guess, because guessing at a cause nobody has confirmed is exactly the
mistake reap_stalled's docstring already warns against.

This also reports dead Prowlarr indexers on the same poll, for the same
reason it reports stuck imports on one. Lidarr's own OnHealthIssue ntfy
notification is fire-and-forget: it fires once, at the moment of the event,
and a send that fails right then — often because whatever tripped the
indexer also broke the network for a moment — is gone forever, with no
retry. A thirty-minute poll that only marks something notified once the
push actually lands does not have that weakness. See tools/indexers.py for
the guard that keeps a Prowlarr that could not be asked from ever being
read as "nothing is down".

Usage:

    stuck-imports.py                    # print what is stuck, change nothing
    stuck-imports.py --notify           # also push a summary to ntfy
    stuck-imports.py --all              # every stuck item and down indexer, any age
    stuck-imports.py --hours 4          # override STUCK_HOURS for this run
    stuck-imports.py --indexer-hours 2  # override INDEXER_HOURS for this run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# search-backlog.py already paid for this lesson: a failed question to
# Prowlarr must never be read as "every indexer is fine", because a network
# blip is often the very thing that took an indexer down in the first place.
# indexers.py is the one place that answer lives now, so both tools ask it
# the same way instead of quietly drifting apart.
import indexers  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIDARR = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")
LIDARR_KEY = os.environ.get("LIDARR_API_KEY", "")
STATE_DIR = os.environ.get("STATE_DIR", os.path.join(ROOT, "state"))
LEDGER = os.path.join(STATE_DIR, "stuck.json")
# Long enough that an import Lidarr is still likely to sort out on its own is
# not reported as a standing failure, short enough that a real backlog is not
# hidden for days. Twelve hours is well past every retry window seen here.
STUCK_HOURS = float(os.environ.get("STUCK_HOURS", "12"))
INDEXER_LEDGER = os.path.join(STATE_DIR, "indexer-health.json")
# Indexers flap. Four went down and quietly self-healed within the hour the
# night this was written, and reporting every blip would teach the user to
# ignore the one channel this whole watchdog exists to protect. Six hours
# matches Lidarr's own IndexerLongTermStatusCheck, so this never fires
# before Lidarr's own health check would already have said the same thing.
INDEXER_HOURS = float(os.environ.get("INDEXER_HOURS", "6"))
# One dead indexer is a nuisance that quite often heals itself. Every
# indexer dead at once is not four coincidences, it is the stack going
# blind — usually from one shared cause that has nothing to do with any
# single indexer (see indexers.py) — and that is worth knowing about long
# before six hours are up.
INDEXER_OUTAGE_MINUTES = float(os.environ.get("INDEXER_OUTAGE_MINUTES", "30"))

# Lidarr and qBittorrent both mount the download tree at /data (see the
# comment beside that bind mount in deploy/docker-compose.yml); a process
# running on the host needs to know where /data actually lives here to read
# the same files. Left unset, outputPath is used as-is, which only works when
# this happens to run somewhere that already sees it at that path.
DATA_DIR = os.environ.get("DATA_DIR", "").rstrip("/")
CONTAINER_DATA_ROOT = "/data"

NTFY_SERVER = "https://ntfy.sh"
AUDIO_EXT = (".flac", ".ape", ".wv", ".wav")


def lidarr(path: str) -> dict:
    """GET one Lidarr endpoint.

    Only GET is implemented, on purpose: this tool must never delete,
    blocklist, or otherwise change a queue record, and a helper that cannot
    write cannot be misused into doing it by accident later.
    """
    if not LIDARR_KEY:
        sys.exit("set LIDARR_API_KEY")
    req = urllib.request.Request(f"{LIDARR}/api/v1/{path}",
                                 headers={"X-Api-Key": LIDARR_KEY})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def load_ledger() -> dict:
    try:
        with open(LEDGER) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_ledger(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, LEDGER)


def load_indexer_ledger() -> dict:
    try:
        with open(INDEXER_LEDGER) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_indexer_ledger(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = INDEXER_LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, INDEXER_LEDGER)


def host_path(container_path: str | None) -> str | None:
    """Where this host actually sees a path Lidarr reported from inside Docker.

    Without DATA_DIR the path is returned unchanged, which is only right when
    this happens to run somewhere that shares the container's view of /data.
    Running it on the host without DATA_DIR set does not fail here — it just
    cannot read the files, and the caller degrades to the status message
    instead (see diagnose()'s fallback for that).
    """
    if not container_path:
        return None
    if not DATA_DIR:
        return container_path
    if container_path == CONTAINER_DATA_ROOT or container_path.startswith(CONTAINER_DATA_ROOT + "/"):
        return DATA_DIR + container_path[len(CONTAINER_DATA_ROOT):]
    return container_path


def files_at(output_path: str | None) -> list[str] | None:
    """The file names in outputPath, or None if the listing could not be read.

    None and an empty list mean different things, the same way they do in
    bridge.py's own file-list reading: nothing could be read, against it was
    read and holds nothing. Folding the two together would let an unreachable
    path masquerade as an empty folder, which is not the same failure at all.
    """
    path = host_path(output_path)
    if not path:
        return None
    try:
        return os.listdir(path)
    except OSError:
        return None


def status_messages(item: dict) -> list[str]:
    """Every message Lidarr recorded for this queue item, flattened."""
    out: list[str] = []
    for entry in item.get("statusMessages") or []:
        if isinstance(entry, dict):
            out.extend(m for m in (entry.get("messages") or []) if isinstance(m, str))
        elif isinstance(entry, str):
            out.append(entry)
    return out


def diagnose(messages: list[str], files: list[str] | None,
            output_path: str | None = None) -> tuple[str, str]:
    """What is actually wrong with a release stuck at importFailed, and the fix.

    Order matters here. A single-file-plus-cue release very often carries the
    exact same "not close enough" score message as a release that is merely
    mis-scored — Lidarr's matcher grades it as a bad match, not as an
    unsplittable image — so the two are indistinguishable from the message
    text alone. The file check has to run first, or a cue image is silently
    reported as a score problem, which is a different fix.
    """
    joined = "; ".join(messages)

    if files is not None:
        audio = [f for f in files if f.lower().endswith(AUDIO_EXT)]
        cue = [f for f in files if f.lower().endswith(".cue")]
        if len(audio) == 1 and cue:
            fix = "tools/split-cue.py"
            if output_path:
                fix += f" {shlex.quote(output_path)}"
            return ("cue-image",
                    f"{audio[0]} plus a cue sheet — Lidarr imports one file per "
                    f"track and never will here. Split it: {fix}")

    lowered = joined.lower()
    # Checked before the score-mismatch pattern below: Lidarr states this one
    # outright, and a release that is really missing most of its tracks is
    # not fixed by bypassing the match gate — there is nothing to import.
    if "has missing tracks" in lowered or "not imported or missing from the release" in lowered:
        return ("incomplete-release",
                "far fewer files than the release needs — nothing here will "
                "complete it. Blocklist it and search again.")

    match = re.search(r"album match is not close enough:\s*([\d.]+)\s*%\s*vs\s*([\d.]+)\s*%",
                      joined, re.I)
    if match:
        got, need = match.groups()
        return ("match-score",
                f"scored {got}% against Lidarr's {need}% album-match gate, but "
                f"the files look fine. Switch to the release whose track "
                f"count matches, or force a manual import with explicit "
                f"trackIds to bypass the gate.")

    return ("unclassified", joined or "no status message recorded")


def label(item: dict) -> str:
    artist = (item.get("artist") or {}).get("artistName")
    album = (item.get("album") or {}).get("title")
    if artist and album:
        return f"{artist} — {album}"
    return album or item.get("title") or "?"


def ntfy_topic() -> str | None:
    """The ntfy topic to push to, read the same way ~/.local/bin/ecoflow-power-watch
    reads it — one topic, in one place, instead of two tools disagreeing about it.
    NTFY_TOPIC in the environment wins, for a run that wants a different topic
    (a test channel, say) without touching gsettings.
    """
    env = os.environ.get("NTFY_TOPIC", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["dconf", "read", "/org/gnome/shell/extensions/brainusage/ntfy-topic"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return out.strip("'") or None
    except Exception:
        return None


def push(title: str, body: str, topic: str) -> None:
    # Priority 3 (default) and the "cd" tag, not the power-outage alert's 4
    # (high): both land on the same topic, and a stuck import competing with
    # a mains outage for the phone's attention would bury the one that
    # actually needs to wake somebody up.
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{topic}", data=body.encode(),
        headers={"Title": title, "Priority": "3", "Tags": "cd"})
    urllib.request.urlopen(req, timeout=10).read()


def report_stuck(hours: float | None = None, all_items: bool = False,
                 notify: bool = False) -> dict:
    """Read the queue, update the ledger, and say what is still stuck.

    The ledger is rebuilt from this run's queue every time: a downloadId not
    seen here is dropped, whatever the reason it left for, so a recurrence of
    the same failure is treated as new and notifies again rather than
    inheriting a stale "already told you" flag from whatever it meant before.
    """
    threshold = STUCK_HOURS if hours is None else hours
    queue = lidarr("queue?pageSize=500&includeAlbum=true&includeArtist=true")
    seen = load_ledger()
    now = time.time()
    fresh: dict[str, dict] = {}
    reportable: list[dict] = []
    eligible: list[str] = []

    for item in queue.get("records", []):
        if item.get("trackedDownloadState") != "importFailed":
            continue
        key = str(item.get("downloadId") or item.get("id"))
        before = seen.get(key)
        first_seen = before["first_seen"] if before else now
        already_notified = bool(before and before.get("notified"))
        fresh[key] = {"first_seen": first_seen, "notified": already_notified}

        elapsed_hours = (now - first_seen) / 3600
        old_enough = elapsed_hours >= threshold
        if old_enough:
            eligible.append(key)
        # --all is for a human looking things over by hand; it must not lower
        # the bar for --notify, or it would defeat the whole point of
        # STUCK_HOURS, which is to give Lidarr a fair chance to sort itself
        # out before anyone is paged about it.
        if old_enough or all_items:
            kind, detail = diagnose(status_messages(item), files_at(item.get("outputPath")),
                                    host_path(item.get("outputPath")))
            reportable.append({"key": key, "label": label(item), "hours": elapsed_hours,
                                "kind": kind, "detail": detail})

    save_ledger(fresh)

    notified: list[str] = []
    pushed = False
    push_error: str | None = None
    if notify and eligible:
        due = [k for k in eligible if not fresh[k]["notified"]]
        if due:
            topic = ntfy_topic()
            if topic:
                lines = [r for r in reportable if r["key"] in eligible]
                title = f"{len(eligible)} import(s) stuck"
                body = "\n".join(f"{r['label']}: {r['detail']}" for r in lines)[:1000]
                try:
                    push(title, body, topic)
                    pushed = True
                except Exception as exc:
                    # A ledger already saved above is not touched again here:
                    # a failed push must cost nothing but the push itself, so
                    # the next run gets to try again instead of silently
                    # losing the one chance to say anything.
                    push_error = f"{type(exc).__name__}: {exc}"
                if pushed:
                    for k in due:
                        fresh[k]["notified"] = True
                    save_ledger(fresh)
                    notified = due

    return {"reportable": reportable, "notified": notified, "pushed": pushed,
            "push_error": push_error, "ledger": fresh}


def report_indexer_health(hours: float | None = None, outage_minutes: float | None = None,
                          all_items: bool = False, notify: bool = False) -> dict:
    """Ask Prowlarr which indexers are down, update the ledger, and say what stands.

    indexers_down() returning None means Prowlarr could not be asked, which
    is not the same as every indexer being healthy — indexers.py explains why
    that distinction matters. Here it means leaving the ledger completely
    untouched and saying so, the same way a failed notification push below
    must never cost the ledger anything either.

    An indexer flaps, so an ordinary failure is only reported once it has
    lasted INDEXER_HOURS. But every indexer failing at once is the stack
    going blind, not INDEXER_HOURS worth of coincidences, so that case is
    escalated on the much shorter INDEXER_OUTAGE_MINUTES fuse instead — and
    the "status" this returns tells the difference between "checked, all
    fine", "checked, something is wrong" and the two ways checking itself
    can fail: no Prowlarr key configured, or Prowlarr unreachable.
    """
    if not indexers.PROWLARR_KEY:
        return {"status": "no-key", "reportable": [], "notified": [], "pushed": False,
                "push_error": None, "total_outage": False}

    down = indexers.indexers_down()
    if down is None:
        return {"status": "unreachable", "reportable": [], "notified": [], "pushed": False,
                "push_error": None, "total_outage": False}

    threshold_hours = INDEXER_HOURS if hours is None else hours
    outage_min = INDEXER_OUTAGE_MINUTES if outage_minutes is None else outage_minutes
    total = indexers.indexer_count()
    # total is None when Prowlarr answered indexers_down() but not this
    # second question — rare, but not a reason to guess. Without a trustworthy
    # total there is no way to tell "every" indexer from merely "several", so
    # this falls back to the ordinary per-indexer threshold instead.
    total_outage = bool(down) and total is not None and total > 0 and len(down) >= total
    threshold_seconds = (outage_min * 60) if total_outage else (threshold_hours * 3600)

    seen = load_indexer_ledger()
    now = time.time()
    fresh: dict[str, dict] = {}
    reportable: list[dict] = []
    eligible: list[str] = []

    for name in down:
        before = seen.get(name)
        first_seen = before["first_seen"] if before else now
        already_notified = bool(before and before.get("notified"))
        fresh[name] = {"first_seen": first_seen, "notified": already_notified}

        elapsed = now - first_seen
        old_enough = elapsed >= threshold_seconds
        if old_enough:
            eligible.append(name)
        # --all is for a human looking things over by hand; it must not lower
        # the bar for --notify, for the same reason report_stuck() draws the
        # same line between them.
        if old_enough or all_items:
            reportable.append({"name": name, "hours": elapsed / 3600})

    save_indexer_ledger(fresh)

    notified: list[str] = []
    pushed = False
    push_error: str | None = None
    if notify and eligible:
        due = [n for n in eligible if not fresh[n]["notified"]]
        if due:
            topic = ntfy_topic()
            if topic:
                title = (f"total Prowlarr outage: all {len(down)} indexer(s) down"
                          if total_outage else f"{len(eligible)} indexer(s) down")
                body = "\n".join(sorted(due))[:1000]
                try:
                    push(title, body, topic)
                    pushed = True
                except Exception as exc:
                    # Nothing saved above is touched again here: a failed push
                    # must cost nothing but the push itself, so the next run
                    # gets to try again instead of silently losing the one
                    # chance to say anything.
                    push_error = f"{type(exc).__name__}: {exc}"
                if pushed:
                    for n in due:
                        fresh[n]["notified"] = True
                    save_indexer_ledger(fresh)
                    notified = due

    return {"status": "ok", "reportable": reportable, "notified": notified,
            "pushed": pushed, "push_error": push_error, "total_outage": total_outage}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notify", action="store_true",
                    help="push a summary to ntfy when something is newly due to be reported")
    ap.add_argument("--hours", type=float,
                    help=f"override STUCK_HOURS for this run (default {STUCK_HOURS:g})")
    ap.add_argument("--indexer-hours", type=float,
                    help=f"override INDEXER_HOURS for this run (default {INDEXER_HOURS:g})")
    ap.add_argument("--indexer-outage-minutes", type=float,
                    help="override INDEXER_OUTAGE_MINUTES for this run "
                         f"(default {INDEXER_OUTAGE_MINUTES:g})")
    ap.add_argument("--all", action="store_true", dest="all_items",
                    help="report every stuck item and every down indexer regardless "
                         "of age, for manual inspection")
    args = ap.parse_args()

    outcome = report_stuck(hours=args.hours, all_items=args.all_items, notify=args.notify)
    reportable = outcome["reportable"]

    if not reportable:
        print("  nothing stuck at importFailed" if args.all_items
              else "  nothing has been stuck long enough to report")
    else:
        print(f"\n  {len(reportable)} stuck import(s):\n")
        for row in sorted(reportable, key=lambda r: -r["hours"]):
            print(f"    {row['label'][:56]:<58} {row['hours']:>5.0f}h  [{row['kind']}]")
            print(f"        {row['detail']}")
        print()

    if args.notify:
        if outcome["pushed"]:
            print(f"  pushed to ntfy: {len(outcome['notified'])} newly notified")
        elif outcome["push_error"]:
            print(f"  ntfy push failed: {outcome['push_error']}")
        elif reportable:
            print("  --notify: nothing new to push")

    indexer_outcome = report_indexer_health(
        hours=args.indexer_hours, outage_minutes=args.indexer_outage_minutes,
        all_items=args.all_items, notify=args.notify)
    indexer_reportable = indexer_outcome["reportable"]

    if indexer_outcome["status"] == "no-key":
        print("\n  no Prowlarr API key configured; skipping indexer health")
    elif indexer_outcome["status"] == "unreachable":
        print("\n  could not ask Prowlarr which indexers are down; "
              "indexer health left unchanged")
    elif not indexer_reportable:
        print("\n  every indexer is up" if args.all_items
              else "\n  no indexer has been down long enough to report")
    else:
        if indexer_outcome["total_outage"]:
            print(f"\n  TOTAL OUTAGE: all {len(indexer_reportable)} indexer(s) down:\n")
        else:
            print(f"\n  {len(indexer_reportable)} indexer(s) down:\n")
        for row in sorted(indexer_reportable, key=lambda r: -r["hours"]):
            print(f"    {row['name'][:56]:<58} {row['hours']:>5.1f}h")
        print()

    if args.notify and indexer_outcome["status"] == "ok":
        if indexer_outcome["pushed"]:
            print(f"  pushed to ntfy: {len(indexer_outcome['notified'])} indexer(s) newly notified")
        elif indexer_outcome["push_error"]:
            print(f"  ntfy push failed (indexer health): {indexer_outcome['push_error']}")
        elif indexer_reportable:
            print("  --notify: nothing new to push for indexer health")


if __name__ == "__main__":
    main()
