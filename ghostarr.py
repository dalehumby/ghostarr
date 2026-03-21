#!/usr/bin/env python3
"""
ghostarr.py — Identify Sonarr TV series that are candidates for deletion
based on Jellyfin watch history.

Run with DRY_RUN=true (default) to only print candidates without mutating data.
"""

import itertools
import threading
import time
import tomllib
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------

_R = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------


class Spinner:
    """Context manager that shows an animated spinner on a background thread."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message: str):
        self.message = message
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                break
            print(f"\r{CYAN}{frame}{_R} {self.message}", end="", flush=True)
            time.sleep(0.1)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        # Clear the spinner line
        print(f"\r{' ' * (len(self.message) + 4)}\r", end="", flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: str = "config.toml") -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Sonarr API helpers
# ---------------------------------------------------------------------------


class SonarrClient:
    def __init__(self, url: str, api_key: str):
        self.base = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = api_key

    def _get(self, path: str, **params) -> list | dict:
        r = self.session.get(f"{self.base}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: dict) -> dict:
        r = self.session.put(f"{self.base}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, body: dict | None = None, **params):
        r = self.session.delete(f"{self.base}{path}", json=body, params=params)
        r.raise_for_status()

    def get_all_series(self) -> list[dict]:
        return self._get("/api/v3/series")

    def get_episode_files(self, series_id: int) -> list[dict]:
        return self._get("/api/v3/episodefile", seriesId=series_id)

    def get_tags(self) -> list[dict]:
        return self._get("/api/v3/tag")

    def unmonitor_season(self, series: dict, season_number: int) -> dict:
        """PUT the full series back with the given season set to monitored=False."""
        updated = dict(series)
        updated["seasons"] = [
            {**s, "monitored": False} if s["seasonNumber"] == season_number else s
            for s in series["seasons"]
        ]
        return self._put(f"/api/v3/series/{series['id']}", updated)

    def delete_season_files(self, episode_files: list[dict], season_number: int):
        """Bulk-delete episode files belonging to the given season."""
        ids = [
            ef["id"] for ef in episode_files if ef.get("seasonNumber") == season_number
        ]
        if ids:
            self._delete("/api/v3/episodefile/bulk", body={"episodeFileIds": ids})

    def delete_series(self, series_id: int):
        self._delete(f"/api/v3/series/{series_id}", deleteFiles="true")


# ---------------------------------------------------------------------------
# Jellyfin API helpers
# ---------------------------------------------------------------------------


class JellyfinClient:
    def __init__(self, url: str, api_key: str):
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "content-type": "application/json",
            }
        )

    def _post_query(self, sql: str) -> dict:
        r = self.session.post(
            f"{self.base}/user_usage_stats/submit_custom_query",
            params={"api_key": self.api_key},
            json={"CustomQueryString": sql, "ReplaceUserId": True},
        )
        r.raise_for_status()
        return r.json()

    def get_last_watched(
        self, series_name: str, season_number: int
    ) -> Optional[datetime]:
        """Query Jellyfin Playback Reporting plugin for the last watched datetime
        of any episode in the given series/season."""
        escaped = series_name.replace("'", "''")
        sql = f"""SELECT
    SUBSTR(ItemName, 1, INSTR(ItemName, ' - ') - 1) AS ShowName,
    CAST(SUBSTR(ItemName, INSTR(ItemName, ' - s') + 4, 2) AS integer) AS Season,
    MIN(DateCreated) AS FirstWatched,
    MAX(DateCreated) AS LatestWatched,
    COUNT(*) AS NumberOfWatches,
    ROUND(SUM(PlayDuration) / 3600.0, 2) AS TotalHours
FROM PlaybackActivity
WHERE ItemType = 'Episode'
  AND ItemName LIKE '% - s%e% - %'
  AND ShowName = '{escaped}'
  AND Season = {season_number}
GROUP BY ShowName, Season
ORDER BY TotalHours"""
        try:
            data = self._post_query(sql)
        except requests.RequestException as e:
            print(
                f"  [WARN] Jellyfin query failed for {series_name} S{season_number}: {e}",
                file=sys.stderr,
            )
            return None
        results = data.get("results") or []
        if not results:
            return None
        raw = results[0][3]  # LatestWatched — column order is fixed by our SQL
        # Jellyfin returns 7 fractional-second digits; Python fromisoformat supports max 6
        if "." in raw:
            date_part, frac = raw.rsplit(".", 1)
            raw = f"{date_part}.{frac[:6]}"
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _parse_dt(iso: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string into a timezone-aware datetime."""
    if not iso:
        return None
    # Python 3.11+ fromisoformat handles 'Z' suffix
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def process_series(
    series: dict,
    tags_map: dict[int, str],
    episode_files: list[dict],
    cutoff_months: int,
    keep_tag: str,
    jellyfin: JellyfinClient,
) -> tuple[list[dict], list[dict]]:
    """
    Evaluate a single series and return (actions, skips).

    actions: dicts with type "unmonitor_season" | "delete_season_files" | "delete_series"
    skips:   dicts explaining why a series/season was NOT actioned
    Both include series_id and series_title.
    """
    actions: list[dict] = []
    skips: list[dict] = []
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=cutoff_months * 30)

    title = series["title"]
    series_id = series["id"]

    def _skip(stype: str, **extra) -> dict:
        return {"type": stype, "series_id": series_id, "series_title": title, **extra}

    # --- Skip: added < cutoff months ago ---
    added = _parse_dt(series.get("added"))
    if added and added > cutoff:
        skips.append(_skip("series_added_recently", added=added.date()))
        return actions, skips

    # --- Skip: has "keep" tag ---
    series_tag_names = {tags_map.get(tid, "") for tid in series.get("tags", [])}
    if keep_tag in series_tag_names:
        skips.append(_skip("series_keep_tag", tag=keep_tag))
        return actions, skips

    # Track whether all seasons end up unmonitored with no files
    all_clean = True
    sorted_seasons = sorted(series.get("seasons", []), key=lambda s: s["seasonNumber"])
    visited: set[int] = set()

    for season in sorted_seasons:
        season_number = season["seasonNumber"]
        visited.add(season_number)

        # Season 0 (specials) — treat as normal seasons
        stats = season.get("statistics", {})
        file_count = stats.get("episodeFileCount", 0)

        # --- Skip: season aired < cutoff months ago ---
        previous_airing = _parse_dt(stats.get("previousAiring"))
        if previous_airing and previous_airing > cutoff:
            skips.append(
                _skip(
                    "season_aired_recently",
                    season_number=season_number,
                    previous_airing=previous_airing.date(),
                )
            )
            all_clean = False
            break  # this season is recent; stop here

        # --- Only act if there are downloaded files ---
        if file_count < 1:
            skips.append(_skip("season_no_files", season_number=season_number))
            if season.get("monitored", True):
                all_clean = False
            continue

        # Has files — check watch history
        last_watched = jellyfin.get_last_watched(title, season_number)

        if last_watched is None or last_watched < cutoff:
            # Candidate: unmonitor + delete files
            reason = (
                "no watch record"
                if last_watched is None
                else f"last watched {last_watched.date()} (>{cutoff_months}mo ago)"
            )
            actions.append(
                {
                    "type": "unmonitor_season",
                    "series_id": series_id,
                    "series_title": title,
                    "season_number": season_number,
                    "reason": reason,
                }
            )
            season_bytes = sum(
                ef.get("size", 0)
                for ef in episode_files
                if ef.get("seasonNumber") == season_number
            )
            actions.append(
                {
                    "type": "delete_season_files",
                    "series_id": series_id,
                    "series_title": title,
                    "season_number": season_number,
                    "file_count": file_count,
                    "size_gib": round(season_bytes / (1024**3), 1),
                    "reason": "season marked for deletion",
                }
            )
        else:
            # Someone is actively watching — stop here
            skips.append(
                _skip(
                    "season_recently_watched",
                    season_number=season_number,
                    last_watched=last_watched.date(),
                )
            )
            all_clean = False
            break

    # --- Note any seasons that were never evaluated (loop broke early) ---
    for season in sorted_seasons:
        sn = season["seasonNumber"]
        if sn not in visited:
            skips.append(_skip("season_not_evaluated", season_number=sn))

    # --- Possibly remove entire series from Sonarr ---
    if all_clean:
        if series.get("status") == "ended":
            actions.append(
                {
                    "type": "delete_series",
                    "series_id": series_id,
                    "series_title": title,
                    "reason": "all seasons cleaned up and series has ended",
                }
            )
        else:
            skips.append(_skip("series_continuing"))

    return actions, skips


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------


def apply_actions(sonarr: SonarrClient, actions: list[dict], series_map: dict):
    """Execute the planned mutations against Sonarr."""
    for action in actions:
        atype = action["type"]
        sid = action["series_id"]

        if atype == "unmonitor_season":
            series_obj = series_map[sid]
            updated = sonarr.unmonitor_season(series_obj, action["season_number"])
            # Keep the local copy in sync so later actions see updated state
            series_map[sid] = updated

        elif atype == "delete_season_files":
            ep_files = sonarr.get_episode_files(sid)
            sonarr.delete_season_files(ep_files, action["season_number"])

        elif atype == "delete_series":
            sonarr.delete_series(sid)


def print_report(all_actions: list[dict], all_skips: list[dict]):
    # Build an ordered list of unique (series_id, series_title) keys preserving encounter order
    seen: dict[int, str] = {}
    for item in all_actions + all_skips:
        sid = item["series_id"]
        if sid not in seen:
            seen[sid] = item["series_title"]

    if not seen:
        print(f"{GREEN}No candidates found.{_R}")
        return

    # Group actions and skips by series_id for easy lookup
    from collections import defaultdict

    actions_by_series: dict[int, list[dict]] = defaultdict(list)
    skips_by_series: dict[int, list[dict]] = defaultdict(list)
    for a in all_actions:
        actions_by_series[a["series_id"]].append(a)
    for s in all_skips:
        skips_by_series[s["series_id"]].append(s)

    for sid, title in seen.items():
        print(f"\n{BOLD}{CYAN}{title} (id={sid}){_R}")

        # Series-level skips first
        for skip in skips_by_series[sid]:
            stype = skip["type"]
            if stype == "series_added_recently":
                print(f"  {YELLOW}[SKIP] Added recently ({skip['added']}){_R}")
            elif stype == "series_keep_tag":
                print(f'  {YELLOW}[SKIP] Has tag "{skip["tag"]}"{_R}')
            elif stype == "series_continuing":
                print(f"  {GREEN}[CONTINUING] Series not ended — keeping in Sonarr{_R}")

        # Season-level items, grouped by season number
        season_items: dict[int, list[dict]] = defaultdict(list)
        for a in actions_by_series[sid]:
            if "season_number" in a:
                season_items[a["season_number"]].append(("action", a))
        for s in skips_by_series[sid]:
            if "season_number" in s:
                season_items[s["season_number"]].append(("skip", s))

        for season_number in sorted(season_items):
            label = f"  Season {season_number}:"
            pad = " " * len(label)
            first = True
            for kind, item in season_items[season_number]:
                prefix = label if first else pad
                first = False
                if kind == "action":
                    atype = item["type"]
                    if atype == "unmonitor_season":
                        print(f"{prefix} {RED}[UNMONITOR] — {item['reason']}{_R}")
                    elif atype == "delete_season_files":
                        print(
                            f"{prefix} {RED}[DELETE FILES] ({item.get('file_count', '?')} files,"
                            f" {item.get('size_gib', '?')} GiB) — {item['reason']}{_R}"
                        )
                else:
                    stype = item["type"]
                    if stype == "season_no_files":
                        print(f"{prefix} {DIM}[NO FILES]{_R}")
                    elif stype == "season_aired_recently":
                        print(
                            f"{prefix} {YELLOW}[SKIP] Aired recently"
                            f" ({item['previous_airing']}){_R}"
                        )
                    elif stype == "season_recently_watched":
                        print(
                            f"{prefix} {GREEN}[WATCHED] Last watched"
                            f" {item['last_watched']} — kept{_R}"
                        )
                    elif stype == "season_not_evaluated":
                        print(
                            f"{prefix} {DIM}[NOT EVALUATED] (loop stopped at earlier season){_R}"
                        )

        # Series-level delete action (no season_number)
        for a in actions_by_series[sid]:
            if a["type"] == "delete_series":
                print(f"  {RED}{BOLD}[DELETE SERIES] — {a['reason']}{_R}")

    # --- Summary ---
    season_deletes = [a for a in all_actions if a["type"] == "delete_season_files"]
    series_deletes = [a for a in all_actions if a["type"] == "delete_series"]
    total_files = sum(a.get("file_count", 0) for a in season_deletes)
    total_gib = round(sum(a.get("size_gib", 0.0) for a in season_deletes), 1)

    print(f"\n{DIM}{'─' * 50}{_R}")
    print(f"{BOLD}Summary{_R}")
    print(f"  Seasons to clean:  {len(season_deletes)}")
    print(f"  Series to remove:  {len(series_deletes)}")
    print(f"  Files to delete:   {total_files}")
    print(f"  {BOLD}Space to free:     {total_gib} GiB{_R}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    config_path = Path("config.toml")
    if not config_path.exists():
        print(
            "config.toml not found. Copy config.toml.example and fill in your values."
        )
        sys.exit(1)

    cfg = load_config(str(config_path))
    dry_run: bool = cfg.get("options", {}).get("dry_run", True)
    cutoff_months: int = cfg.get("options", {}).get("cutoff_months", 3)
    keep_tag: str = cfg.get("options", {}).get("keep_tag", "keep")

    sonarr = SonarrClient(
        url=cfg["sonarr"]["url"],
        api_key=cfg["sonarr"]["api_key"],
    )
    jellyfin = JellyfinClient(
        url=cfg["jellyfin"]["url"],
        api_key=cfg["jellyfin"]["api_key"],
    )

    with Spinner("Fetching series list from Sonarr…"):
        all_series = sonarr.get_all_series()
        tags = sonarr.get_tags()
    tags_map: dict[int, str] = {t["id"]: t["label"] for t in tags}
    print(f"Found {len(all_series)} series.")

    # Build a series-id → series-object map for mutation phase
    series_map: dict[int, dict] = {s["id"]: s for s in all_series}

    all_actions: list[dict] = []
    all_skips: list[dict] = []
    total = len(all_series)
    for i, series in enumerate(all_series, 1):
        title_trunc = series["title"][:45]
        print(f"\r  [{i}/{total}] {title_trunc:<45}", end="", flush=True)
        episode_files = sonarr.get_episode_files(series["id"])
        actions, skips = process_series(
            series=series,
            tags_map=tags_map,
            episode_files=episode_files,
            cutoff_months=cutoff_months,
            keep_tag=keep_tag,
            jellyfin=jellyfin,
        )
        all_actions.extend(actions)
        all_skips.extend(skips)
    print()  # end the progress line

    print_report(all_actions, all_skips)

    if dry_run:
        print(
            f"\n{YELLOW}[DRY RUN] No changes made. Set dry_run = false in config.toml to apply.{_R}"
        )
    else:
        print(f"\nApplying {len(all_actions)} action(s)…")
        apply_actions(sonarr, all_actions, series_map)
        print(f"{GREEN}Done.{_R}")


if __name__ == "__main__":
    main()
