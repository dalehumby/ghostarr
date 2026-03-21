# ghostarr
Remove unwatched Jellyfin content by querying Sonarr (series) and Radarr (movies) and getting the watch history for Jellyfin Playback Reporting plugin.


## Series algorithm

Find a series
If it was added in last 3 months:
  Stop processing, someone might still want to watch this

If it as a keep tag:
  Stop processing

For each season:
  If the season has aired < 3 months ago:
    Stop processing, the series may have been added a long time ago but this season is recent
  If the season has > 1 downloaded episode:
    Find when anything in the season was last watched (Jellyfin sql query)
    If last watched > 3 months ago OR no watch record:
      Delete: Mark season as untracked and delete all files
    Else:
      Stop processing this season and any higher seasons: Someone is watching this and may want to continue series

If all seasons are untracked, no files:
  If series is ended:
    Delete entire series from Sonarr
  Else:
    Keep series so future seasons are downloaded

