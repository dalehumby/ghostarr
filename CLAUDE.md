# CLAUDE.md — Developer and AI Context

This file contains implementation notes and algorithm detail for ghostarr. It is intended for developers and AI assistants working on this codebase.

## Development environment

This project uses [uv](https://docs.astral.sh/uv/) for dependency and environment management. Always run commands through `uv` so the correct Python version and virtualenv are used automatically.

```
uv sync          # install/update dependencies from uv.lock
uv run python ghostarr.py   # run the script inside the managed environment
```

The required Python version is pinned in `.python-version` (currently 3.14). `uv` will download and manage that version automatically if it is not already installed. Do not rely on the system Python.

The only runtime dependency is `requests` (declared in `pyproject.toml`). There are no dev or test dependencies at this time.

## Series evaluation algorithm

All logic lives in `process_series()` in `ghostarr.py`.

**Series-level filters (applied first; early exit if matched):**

1. If the series was added within `cutoff_months` — skip. It may be new and unwatched by design.
2. If the series has the `keep_tag` in Sonarr — skip. User has explicitly marked it to keep.

**Per-season loop (seasons iterated in ascending order):**

3. If a season aired within `cutoff_months` — stop the loop entirely. The series is still active.
4. If a season has no downloaded episode files — skip this season and continue to the next.
5. If a season has files — query Jellyfin (Playback Reporting plugin SQL) for the last watched datetime.
   - If never watched, or last watched more than `cutoff_months` ago: mark for deletion (unmonitor the season in Sonarr, then bulk-delete all episode files).
   - If watched within `cutoff_months`: stop the loop entirely. Someone is actively watching and may continue.

**Series-level deletion (after loop completes):**

6. If all seasons are now unmonitored with no remaining files:
   - If the series status is `ended`: delete the entire series from Sonarr.
   - If the series is still airing: keep it in Sonarr so future seasons are downloaded automatically.

## Key design decisions

- **Loop stops on first recent season or recent watch.** Higher seasons are not evaluated once a stop condition is hit, avoiding false positives on shows someone is mid-way through.
- **Season 0 (specials) is treated as a normal season.** No special-casing.
- **The `k` key in interactive mode** calls `SonarrClient.add_tag_to_series()` to PUT the keep tag onto the series immediately, so it is protected on future runs without any manual Sonarr UI step.
- **`apply_actions()` is kept** alongside `confirm_and_apply_actions()` for potential scripted/non-interactive use in future.
- **Jellyfin watch history** is queried via the Playback Reporting plugin's custom SQL endpoint (`/user_usage_stats/submit_custom_query`). The standard Jellyfin API does not expose per-episode watch timestamps in a usable form.
