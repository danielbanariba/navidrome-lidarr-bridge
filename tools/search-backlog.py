#!/usr/bin/env python3
"""Work through the albums Lidarr wants, without getting the indexers banned.

Lidarr never searches its own backlog. The RSS sync every fifteen minutes sees
only what an indexer has just published, so an album from 2016 sits wanted
forever while nothing looks for it — which is why a library with sixty-seven
wanted albums has three downloads running.

Lidarr's own "Search for Missing" asks for all of them at once. That is exactly
what tripped the indexers' rate limits here: Prowlarr disabled The Pirate Bay
and LimeTorrents, and every search after that returned nothing at all while
reporting it as though the releases did not exist.

So this asks for a few at a time, stops when enough downloads are running, and
gives up for now the moment an indexer goes out of service — because past that
point it is not searching, it is only making things worse.

Usage:

    search-backlog.py --target 15      # keep about fifteen downloads going
    search-backlog.py --dry-run        # name what it would ask for
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import time
import urllib.error
import urllib.request

# This runs for minutes and prints as it goes. Redirected to a file, the default
# buffering held every line until it ended, so a run that was working looked
# exactly like one that had hung.
print = functools.partial(print, flush=True)  # noqa: A001 - deliberate shadow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIDARR = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")
LIDARR_KEY = os.environ.get("LIDARR_API_KEY", "")
PROWLARR = os.environ.get("PROWLARR_URL", "http://localhost:9696").rstrip("/")
PROWLARR_KEY = os.environ.get("PROWLARR_API_KEY", "")
STATE_DIR = os.environ.get("STATE_DIR", os.path.join(ROOT, "state"))
LEDGER = os.path.join(STATE_DIR, "searched.json")
# Long enough that five indexers are not asked five times in a minute, short
# enough to be worth running. The audition queue waits half an hour because it
# downloads; this only asks a question.
PAUSE = int(os.environ.get("SEARCH_PAUSE", "300"))
# An album asked about recently is not asked about again: the answer does not
# change in an afternoon, and re-asking spends the budget that would have found
# something new.
RETRY_DAYS = float(os.environ.get("SEARCH_RETRY_DAYS", "7"))


def lidarr(path: str, method: str = "GET", body=None):
    if not LIDARR_KEY:
        sys.exit("set LIDARR_API_KEY")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{LIDARR}/api/v1/{path}", data=data, method=method,
        headers={"X-Api-Key": LIDARR_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def indexers_down() -> list[str] | None:
    """Indexers Prowlarr has taken out of service, by name, or None.

    None means the question could not be asked, and it is not the same as an
    empty list. Swallowing the failure and returning [] said "nothing is down"
    whenever Prowlarr was slow — so the guard that exists to stop this tool the
    moment an indexer falls over could be silenced by the very load that was
    knocking them over. Sixty-seven searches went out and every indexer here
    ended up disabled.

    Checked before every batch rather than once at the start. The point of
    pausing is to notice when the pause was not enough.
    """
    if not PROWLARR_KEY:
        return []
    try:
        head = {"X-Api-Key": PROWLARR_KEY}

        def get(path):
            req = urllib.request.Request(f"{PROWLARR}/api/v1/{path}", headers=head)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)

        names = {i["id"]: i.get("name", str(i["id"])) for i in get("indexer")}
        return [names.get(s.get("indexerId"), "?") for s in get("indexerstatus")]
    except Exception as exc:
        print(f"    could not ask Prowlarr which indexers are up: "
              f"{type(exc).__name__}")
        return None


def load() -> dict:
    try:
        with open(LEDGER) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, LEDGER)


def queued() -> int:
    return lidarr("queue?pageSize=1")["totalRecords"]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=int, default=15,
                    help="stop once this many downloads are running (default 15)")
    ap.add_argument("--batch", type=int, default=2,
                    help="albums per round (default 2)")
    ap.add_argument("--most", type=int, default=12,
                    help="never ask about more than this in one run (default 12). "
                         "Sixty-seven in thirty-three minutes disabled every "
                         "indexer on this machine for a day.")
    ap.add_argument("--dry-run", action="store_true",
                    help="name what it would ask for and stop")
    args = ap.parse_args()

    # Ignoring the ones already known to be out: 1337x is Cloudflare-banned for
    # this address and waiting for it would mean never starting.
    first = indexers_down()
    if first is None:
        sys.exit("  Prowlarr could not be reached. Not searching blind — that is "
                 "how every indexer here ended up disabled.")
    before = set(first)
    try:
        head = {"X-Api-Key": PROWLARR_KEY}
        req = urllib.request.Request(f"{PROWLARR}/api/v1/indexer", headers=head)
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = len(json.load(resp))
    except Exception:
        total = 0
    # Nothing to ask with. Firing searches at a Lidarr whose every indexer is
    # disabled produces empty answers that look exactly like real ones, and the
    # requests themselves are part of why they are disabled.
    if total and len(before) >= total:
        sys.exit(f"  all {total} indexer(s) are out of service. Nothing to "
                 f"search with; waiting is the only thing that helps.")
    ledger = load()
    cutoff = time.time() - RETRY_DAYS * 86400

    wanted = lidarr("wanted/missing?pageSize=500&includeArtist=true")["records"]
    due = [a for a in wanted if ledger.get(str(a["id"]), 0) < cutoff]
    print(f"\n  {len(wanted)} album(s) wanted, {len(due)} not asked about in "
          f"the last {RETRY_DAYS:g} days")
    if before:
        print(f"  already out of service, and not waited for: {', '.join(sorted(before))}")

    running = queued()
    print(f"  {running} download(s) running, aiming for {args.target}\n")
    if running >= args.target:
        print("  enough already; nothing to ask for")
        return
    if not due:
        print("  nothing due to be asked about")
        return

    asked = 0
    due = due[:args.most]
    for start in range(0, len(due), args.batch):
        if queued() >= args.target:
            print(f"\n  {queued()} running — that is enough")
            break
        # An indexer that went down since the last batch is the signal to stop.
        # Searching past it produces empty answers that look like real ones.
        current = indexers_down()
        if current is None:
            print("\n  stopping: Prowlarr stopped answering, so there is no way "
                  "to tell whether the indexers are still up.")
            break
        now_down = set(current) - before
        if now_down:
            print(f"\n  stopping: {', '.join(sorted(now_down))} went out of "
                  f"service. Asked about {asked} album(s); the rest keep.")
            break

        batch = due[start:start + args.batch]
        for album in batch:
            artist = (album.get("artist") or {}).get("artistName", "?")
            print(f"    asking for {artist[:24]} — {album.get('title', '?')[:34]}")
            if args.dry_run:
                continue
            try:
                lidarr("command", "POST",
                       {"name": "AlbumSearch", "albumIds": [album["id"]]})
                ledger[str(album["id"])] = int(time.time())
                asked += 1
            except urllib.error.HTTPError as exc:
                print(f"      refused: HTTP {exc.code}")
        if args.dry_run:
            continue
        save(ledger)
        if start + args.batch < len(due):
            time.sleep(PAUSE)

    if args.dry_run:
        print(f"\n  --dry-run: nothing was asked for")
        return
    print(f"\n  asked about {asked} album(s); {queued()} download(s) running")


if __name__ == "__main__":
    main()
