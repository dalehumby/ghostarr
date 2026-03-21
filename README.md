# 👻 ghostarr

ghostarr cross-references your Sonarr library with watch history from the Jellyfin Playback Reporting plugin. Series and seasons that have not been watched within a configurable period are unmonitored in Sonarr and their files deleted from disk, freeing up space.

<!-- TODO: Radarr/movies support is not yet implemented -->

## How it works

- Fetches all series from Sonarr and skips any added recently or tagged with the configured keep tag
- For each season that has downloaded files, queries Jellyfin for the last watched date
- Seasons not watched within the cutoff period are unmonitored in Sonarr and their files deleted
- Ended series where all seasons have been cleaned up are removed from Sonarr entirely
- In dry-run mode (default), only prints a report — nothing is deleted
- In interactive mode, prompts season-by-season and series-by-series before acting

<!-- TODO: add screenshot or sample output here -->

## Requirements

- Python 3.14+
- [Sonarr](https://sonarr.tv) with API access enabled
- [Jellyfin](https://jellyfin.org) with the [Playback Reporting](https://github.com/jellyfin/jellyfin-plugin-playbackreporting) plugin installed and active
- `uv` (recommended) or `pip`

## Setup

### 1. Install the Jellyfin Playback Reporting plugin

1. In Jellyfin, go to Dashboard > Plugins and search for the Playback Reporting plugin. Install and restart Jellyfin.
2. Once restarted, go to Dashboard > Plugins > Playback Reporting.
3. Click the Settings tab and set "Keep data for" to Forever.

This plugin only records statistics from when it is installed — it cannot backfill prior playback history. It is best to leave it running for a few months before running ghostarr.

### 2. Clone the repository

```
git clone https://github.com/dalehumby/ghostarr.git
cd ghostarr
```

### 3. Install dependencies

```
uv sync
```

### 4. Configure

```
cp config.toml.example config.toml
```

Then edit `config.toml` with your values (see Configuration below).

## Configuration

```toml
[sonarr]
url = "http://localhost:8989"      # Base URL of your Sonarr instance
api_key = "your-sonarr-api-key"   # Settings > General > Security > API Key

[jellyfin]
url = "http://localhost:8096"      # Base URL of your Jellyfin instance
api_key = "your-jellyfin-api-key" # Dashboard > Advanced > API Keys

[options]
dry_run = true        # true = report only, false = prompt and delete
cutoff_months = 3     # seasons unwatched longer than this are candidates for deletion
keep_tag = "keep"     # series with this Sonarr tag are never touched
```

`config.toml` is gitignored to prevent accidentally committing credentials.

## Running

```
uv run ghostarr.py
```

By default `dry_run = true` — the script prints a report but makes no changes. Review the output, then set `dry_run = false` in `config.toml` to run in interactive mode.

In interactive mode you are prompted for each candidate season and series:

- `y` — delete the season files (or remove the series from Sonarr)
- `n` — skip, do not delete anything
- `k` — add the keep tag to the series in Sonarr and skip it entirely

## (Optional) Set up a Leaving Soon library in Jellyfin

Rather than deleting series outright, you can route them through Sonarr's Recycle Bin and expose that folder as a separate Jellyfin library. This gives your users a window to watch anything before it disappears, and acts as a safety net if you accidentally delete something you meant to keep.

1. In Sonarr, go to Settings > Media Management.
2. Under File Management > Recycling Bin, select a folder for deleted shows, e.g. `/media/tvshows-leaving-soon`.
3. Set Recycling Bin Cleanup to 14 or 30 days.
4. In Jellyfin, go to Dashboard > Libraries > Add Media Library. Set the content type to Show and point it at the same folder.
5. Ensure your users have access to the library under Users > username > Access.

## License

MIT
