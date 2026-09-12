"""One place to ask whether Prowlarr still has working indexers.

search-backlog.py learned this the expensive way: a burst of searches
tripped Prowlarr's own rate limits, Prowlarr disabled indexer after indexer,
and every search after that returned nothing at all while reporting it as
though the releases did not exist. Sixty-seven searches went out before
anyone noticed, because a failure to even ask Prowlarr was being read the
same as "nothing is down."

indexers_down() is the fix for that specific mistake, and stuck-imports.py
now needs the same answer to the same question on its own polling cycle.
Two separate copies of "is an indexer down" could silently drift apart —
one of them quietly re-learning that a slow Prowlarr means "nothing is
down" — so this is the one place both tools ask it instead.
"""

from __future__ import annotations

import json
import os
import urllib.request

PROWLARR = os.environ.get("PROWLARR_URL", "http://localhost:9696").rstrip("/")
PROWLARR_KEY = os.environ.get("PROWLARR_API_KEY", "")


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


def indexer_count() -> int | None:
    """How many indexers Prowlarr has configured, or None if it could not say.

    indexers_down() only ever names the ones that are down, never how many
    are supposed to exist, so it cannot by itself tell one dead indexer
    apart from every indexer being dead. stuck-imports.py needs that second
    number to recognise a total outage and escalate it, so this asks the
    other half of the same question — with the same rule indexers_down()
    already learned: a failure here is None, never a count to act on.
    """
    if not PROWLARR_KEY:
        return None
    try:
        head = {"X-Api-Key": PROWLARR_KEY}
        req = urllib.request.Request(f"{PROWLARR}/api/v1/indexer", headers=head)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return len(json.load(resp))
    except Exception:
        return None
