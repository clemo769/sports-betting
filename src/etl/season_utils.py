"""Season utilities for converting between SofaScore and football-data.co.uk formats.

Provides `sofascore_season_to_fdco` to convert a 4-digit year like '2023'
to the FDCO style '2223' used in football-data.co.uk filenames/URLs.
"""

def sofascore_season_to_fdco(season: str) -> str:
    """Convert SofaScore season '2023' -> FDCO '2223'.

    Example: '2023' -> '2223' (22/23 season)
    """
    if not season or len(season) != 4 or not season.isdigit():
        return season
    yy = season[2:4]
    next_yy = str((int(yy) + 1) % 100).zfill(2)
    return f"{yy}{next_yy}"
