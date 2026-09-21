#!/usr/bin/env python3
"""
ghostarr.py — Identify Sonarr TV series that are candidates for deletion
based on Jellyfin watch history.

Run with DRY_RUN=true (default) to only print candidates without mutating data.
"""

import itertools
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

    def create_tag(self, label: str) -> dict:
        r = self.session.post(f"{self.base}/api/v3/tag", json={"label": label})
        r.raise_for_status()
        return r.json()

    def add_tag_to_series(self, series: dict, tag_id: int) -> dict:
        """PUT the full series back with tag_id added (idempotent)."""
        updated = dict(series)
        existing = list(series.get("tags", []))
        if tag_id not in existing:
            existing.append(tag_id)
        updated["tags"] = existing
        return self._put(f"/api/v3/series/{series['id']}", updated)


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
                "Authorization": f'MediaBrowser Token="{api_key}"',
            }
        )
        self._series_map: dict[str, str] = {}
        # (jellyfin_series_id, season_number) -> latest LastPlayedDate across all users
        self._watch_cache: dict[tuple[str, int], datetime] = {}

    def _get(self, path: str, **params) -> dict | list:
        r = self.session.get(f"{self.base}{path}", params=params)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _parse_jellyfin_dt(raw: str) -> datetime:
        if "." in raw:
            date_part, frac = raw.rsplit(".", 1)
            raw = f"{date_part}.{frac[:6]}"
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)

    def load_lookups(self):
        """Fetch all series, users, and watch history upfront."""
        data = self._get(
            "/Items", IncludeItemTypes="Series", Recursive="true", Limit=10000
        )
        self._series_map = {item["Name"]: item["Id"] for item in data["Items"]}

        users = self._get("/Users")
        for user in users:
            try:
                played = self._get(
                    f"/Users/{user['Id']}/Items",
                    IncludeItemTypes="Episode",
                    Recursive="true",
                    Fields="UserData",
                    Filters="IsPlayed",
                    Limit=10000,
                )
            except requests.RequestException:
                continue
            for item in played.get("Items", []):
                raw = item.get("UserData", {}).get("LastPlayedDate")
                if not raw:
                    continue
                series_id = item.get("SeriesId", "")
                season_num = item.get("ParentIndexNumber", -1)
                key = (series_id, season_num)
                dt = self._parse_jellyfin_dt(raw)
                prev = self._watch_cache.get(key)
                if prev is None or dt > prev:
                    self._watch_cache[key] = dt

    def get_last_watched(self, series_name: str, season_number: int) -> datetime | None:
        jellyfin_id = self._series_map.get(series_name)
        if jellyfin_id is None:
            return None
        return self._watch_cache.get((jellyfin_id, season_number))


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _parse_dt(iso: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string into a timezone-aware datetime."""
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
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
    now = datetime.now(tz=UTC)
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
    if all_clean and series.get("status") == "ended":
        actions.append(
            {
                "type": "delete_series",
                "series_id": series_id,
                "series_title": title,
                "reason": "all seasons cleaned up and series has ended",
            }
        )
    elif actions and series.get("status") != "ended":
        # We cleaned up some seasons but the series is still airing — keep it in Sonarr
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


def confirm_and_apply_actions(
    sonarr: SonarrClient,
    all_actions: list[dict],
    all_skips: list[dict],
    series_map: dict,
    keep_tag: str,
    tags_map: dict[int, str],
):
    """Interactively confirm and execute mutations against Sonarr.

    Shows the summary totals first, then per-series detail with inline prompts.
    """
    from collections import defaultdict

    _print_summary(all_actions)

    tag_id_by_label: dict[str, int] = {v: k for k, v in tags_map.items()}

    def _get_or_create_keep_tag_id() -> int:
        if keep_tag not in tag_id_by_label:
            new_tag = sonarr.create_tag(keep_tag)
            tag_id_by_label[keep_tag] = new_tag["id"]
        return tag_id_by_label[keep_tag]

    actions_by_series = _group_by_series(all_actions)
    skips_by_series = _group_by_series(all_skips)

    sorted_sids = sorted(
        actions_by_series,
        key=lambda sid: _series_size_gib(actions_by_series[sid]),
        reverse=True,
    )

    deleted_seasons = 0
    deleted_files = 0
    deleted_gib = 0.0
    deleted_series = 0

    for sid in sorted_sids:
        series_actions = actions_by_series[sid]
        title = series_actions[0]["series_title"]

        _print_series_detail(sid, title, series_actions, skips_by_series[sid])

        season_actions: dict[int, list[dict]] = defaultdict(list)
        series_level_actions: list[dict] = []
        for a in series_actions:
            if "season_number" in a:
                season_actions[a["season_number"]].append(a)
            else:
                series_level_actions.append(a)

        kept = False
        for season_number in sorted(season_actions):
            s_actions = season_actions[season_number]
            delete_action = next(
                (a for a in s_actions if a["type"] == "delete_season_files"), None
            )
            info = ""
            if delete_action:
                info = (
                    f" ({delete_action.get('file_count', '?')} files,"
                    f" {delete_action.get('size_gib', '?')} GiB)"
                )
            answer = (
                input(f"  Delete Season {season_number}{info}? (y/n/k=keep series): ")
                .strip()
                .lower()
            )
            if answer == "k":
                tag_id = _get_or_create_keep_tag_id()
                series_map[sid] = sonarr.add_tag_to_series(series_map[sid], tag_id)
                print(f'  {GREEN}[KEEP] Added "{keep_tag}" tag — skipping series.{_R}')
                kept = True
                break
            elif answer == "y":
                for a in s_actions:
                    if a["type"] == "unmonitor_season":
                        updated = sonarr.unmonitor_season(
                            series_map[sid], season_number
                        )
                        series_map[sid] = updated
                    elif a["type"] == "delete_season_files":
                        ep_files = sonarr.get_episode_files(sid)
                        sonarr.delete_season_files(ep_files, season_number)
                        deleted_seasons += 1
                        deleted_files += a.get("file_count", 0)
                        deleted_gib += a.get("size_gib", 0.0)

        if kept:
            continue

        for a in series_level_actions:
            if a["type"] == "delete_series":
                answer = (
                    input(
                        f'  Delete entire series "{title}" from Sonarr? (y/n/k=keep series): '
                    )
                    .strip()
                    .lower()
                )
                if answer == "k":
                    tag_id = _get_or_create_keep_tag_id()
                    series_map[sid] = sonarr.add_tag_to_series(series_map[sid], tag_id)
                    print(f'  {GREEN}[KEEP] Added "{keep_tag}" tag.{_R}')
                elif answer == "y":
                    sonarr.delete_series(sid)
                    deleted_series += 1

    _print_results(deleted_seasons, deleted_series, deleted_files, deleted_gib)


def _print_series_detail(
    sid: int,
    title: str,
    series_actions: list[dict],
    series_skips: list[dict],
):
    """Print the season-by-season detail block for a single series."""
    from collections import defaultdict

    print(f"\n{BOLD}{CYAN}{title} (id={sid}){_R}")

    for skip in series_skips:
        stype = skip["type"]
        if stype == "series_added_recently":
            print(f"  {YELLOW}[SKIP] Added recently ({skip['added']}){_R}")
        elif stype == "series_keep_tag":
            print(f'  {YELLOW}[SKIP] Has tag "{skip["tag"]}"{_R}')
        elif stype == "series_continuing":
            print(f"  {GREEN}[CONTINUING] Series not ended — keeping in Sonarr{_R}")

    season_items: dict[int, list[dict]] = defaultdict(list)
    for a in series_actions:
        if "season_number" in a:
            season_items[a["season_number"]].append(("action", a))
    for s in series_skips:
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

    for a in series_actions:
        if a["type"] == "delete_series":
            print(f"  {RED}{BOLD}[DELETE SERIES] — {a['reason']}{_R}")


def _print_summary(all_actions: list[dict]):
    """Print the totals summary line."""
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


def _print_results(
    deleted_seasons: int, deleted_series: int, deleted_files: int, deleted_gib: float
):
    """Print what was actually deleted after interactive confirmation."""
    deleted_gib = round(deleted_gib, 1)
    print(f"\n{DIM}{'─' * 50}{_R}")
    print(f"{BOLD}Results{_R}")
    print(f"  Seasons deleted:   {deleted_seasons}")
    print(f"  Series removed:    {deleted_series}")
    print(f"  Files deleted:     {deleted_files}")
    print(f"  {BOLD}Space freed:       {deleted_gib} GiB{_R}")


def _group_by_series(items: list[dict]) -> dict[int, list[dict]]:
    """Group items by series_id, preserving encounter order."""
    from collections import defaultdict

    grouped: dict[int, list[dict]] = defaultdict(list)
    for item in items:
        grouped[item["series_id"]].append(item)
    return grouped


def _series_size_gib(actions: list[dict]) -> float:
    """Total GiB across all delete_season_files actions for a series."""
    return sum(
        a.get("size_gib", 0.0) for a in actions if a["type"] == "delete_season_files"
    )


def print_report(all_actions: list[dict], all_skips: list[dict]):
    seen: dict[int, str] = {}
    for item in all_actions + all_skips:
        sid = item["series_id"]
        if sid not in seen:
            seen[sid] = item["series_title"]

    if not seen:
        print(f"{GREEN}No candidates found.{_R}")
        return

    actions_by_series = _group_by_series(all_actions)
    skips_by_series = _group_by_series(all_skips)

    sorted_sids = sorted(
        seen, key=lambda sid: _series_size_gib(actions_by_series[sid]), reverse=True
    )

    for sid in sorted_sids:
        _print_series_detail(
            sid, seen[sid], actions_by_series[sid], skips_by_series[sid]
        )

    _print_summary(all_actions)


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
    cutoff_months: int = cfg.get("options", {}).get("cutoff_months", 6)
    keep_tag: str = cfg.get("options", {}).get("keep_tag", "keep")

    sonarr = SonarrClient(
        url=cfg["sonarr"]["url"],
        api_key=cfg["sonarr"]["api_key"],
    )
    jellyfin = JellyfinClient(
        url=cfg["jellyfin"]["url"],
        api_key=cfg["jellyfin"]["api_key"],
    )

    with Spinner("Fetching series list from Sonarr and Jellyfin…"):
        all_series = sonarr.get_all_series()
        tags = sonarr.get_tags()
        jellyfin.load_lookups()
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

    if dry_run:
        print_report(all_actions, all_skips)
        print(
            f"\n{YELLOW}[DRY RUN] No changes made. Set dry_run = false in config.toml to apply.{_R}"
        )
    else:
        confirm_and_apply_actions(
            sonarr, all_actions, all_skips, series_map, keep_tag, tags_map
        )
        print(f"\n{GREEN}Done.{_R}")


if __name__ == "__main__":
    main()
