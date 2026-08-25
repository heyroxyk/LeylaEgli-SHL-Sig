"""Pull Leyla Egli's live figures from the SHL APIs into data.json.

Two hosts are involved. The portal is the live source of record for identity,
TPE and the 28 attributes, and gives all of it in one call. The index is the
sim's output and supplies the club name and the on-ice stats.

data.json holds raw API values only, never anything derived or formatted. That
keeps it stable between runs so the workflow can skip commits when nothing
moved, and leaves the commit log usable as a dated record of TPE progression.
"""
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

PLAYER_ID = 2500  # portal pid, from portal.simulationhockey.com/player/2500

PORTAL_PLAYER = "https://portal.simulationhockey.com/api/v1/player?pid={pid}"
INDEX_TEAM = "https://index.simulationhockey.com/api/v1/teams/{team}?league={league}"
INDEX_STATS = "https://index.simulationhockey.com/api/v1/players/stats/{iid}?league={league}&type={phase}"

# Club crests are not an API. Each league ships one SVG sprite stack holding
# every team's mark as a nested <svg> keyed by the team's city, spaces becoming
# underscores. Checked against the live team list: 24/24 SHL and 16/16 SMJHL
# resolve by that rule. (The national leagues key on nameDetails.second instead,
# which a club signature never needs.)
INDEX_STACK = "https://index.simulationhockey.com/stack/{league}.stack.svg"

# The index documents these as "rs", "ps" and "po". Those values are silently
# ignored and fall through to regular season, so passing them looks like it
# works and quietly gives the wrong data. Only the full words select anything.
REGULAR = "regular"
PLAYOFFS = "playoffs"

# The index numbers its leagues in /api/v1/leagues, and the portal's own
# indexRecords use that same numbering, so one map serves both lookups.
LEAGUE_IDS = {"SHL": 0, "SMJHL": 1, "IIHF": 2, "WJC": 3}

# Only these two carry a club season. IIHF and WJC are tournaments, and a
# national-team run must never stand in for a league record on a club signature.
CLUB_LEAGUES = ("SHL", "SMJHL")

DATA_PATH = pathlib.Path(__file__).parent / "data.json"
LOGO_PATH = pathlib.Path(__file__).parent / "logo.svg"

# The portal answers 403 to urllib's default "Python-urllib/3.x". Identify the
# job and where it comes from, so whoever runs the API can see who is calling.
USER_AGENT = "LeylaEgli-SHL-Sig/1.0 (+https://github.com/heyroxyk/LeylaEgli-SHL-Sig)"

PLAYER_FIELDS = {
    "name": str,
    "position": str,
    "handedness": str,
    "height": str,
    "weight": int,
    "birthplace": str,
    "jerseyNumber": int,
    "draftSeason": int,
    "totalTPE": int,
    "appliedTPE": int,
    "bankedTPE": int,
    "currentLeague": str,
    "currentTeamID": int,
}

ATTRIBUTES = (
    "screening", "gettingOpen", "passing", "puckhandling", "shootingAccuracy",
    "shootingRange", "offensiveRead", "checking", "hitting", "positioning",
    "stickchecking", "shotBlocking", "faceoffs", "defensiveRead", "acceleration",
    "agility", "balance", "speed", "stamina", "strength", "fighting", "aggression",
    "bravery", "determination", "teamPlayer", "leadership", "temperament",
    "professionalism",
)

PHASE_FIELDS = (
    "gamesPlayed", "goals", "assists", "points", "plusMinus", "pim",
    "hits", "shotsBlocked", "takeaways", "giveaways", "shotsOnGoal", "timeOnIce",
    "ppPoints", "shPoints", "ppTimeOnIce", "shTimeOnIce",
)
STAT_FIELDS = ("season",) + PHASE_FIELDS

ADVANCED_FIELDS = ("CFPct", "FFPct", "PDO", "GF60", "GA60", "SF60", "SA60")


class ShapeError(Exception):
    """The API answered, but not with what we need to build a valid signature."""


def get_json(url):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ShapeError(f"{url} returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ShapeError(f"{url} unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ShapeError(f"{url} returned malformed JSON: {exc}") from exc


def require_fields(record, spec, where):
    """Check presence and type, reporting every problem at once rather than the first."""
    problems = []
    for field, want in spec.items():
        if field not in record:
            problems.append(f"missing {field!r}")
        elif not isinstance(record[field], want) or isinstance(record[field], bool):
            problems.append(f"{field!r} is {type(record[field]).__name__}, want {want.__name__}")
    if problems:
        raise ShapeError(f"{where}: " + "; ".join(problems))


def fetch_player():
    payload = get_json(PORTAL_PLAYER.format(pid=PLAYER_ID))
    if not isinstance(payload, list) or len(payload) != 1:
        raise ShapeError(
            f"portal /player?pid={PLAYER_ID} returned "
            f"{len(payload) if isinstance(payload, list) else type(payload).__name__} "
            "records, want exactly 1"
        )
    player = payload[0]
    require_fields(player, PLAYER_FIELDS, "portal player record")

    attributes = player.get("attributes")
    if not isinstance(attributes, dict):
        raise ShapeError("portal player record: 'attributes' is not an object")
    missing = [a for a in ATTRIBUTES if a not in attributes]
    if missing:
        raise ShapeError(f"portal attributes missing {len(missing)}: {', '.join(missing)}")
    unexpected = sorted(set(attributes) - set(ATTRIBUTES))
    if unexpected:
        raise ShapeError(f"portal returned unrecognised attributes: {', '.join(unexpected)}")
    for name, value in attributes.items():
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 20:
            raise ShapeError(f"attribute {name!r} is {value!r}, want an integer 0-20")

    if player["currentLeague"] not in LEAGUE_IDS:
        raise ShapeError(
            f"unknown league {player['currentLeague']!r}; known: {', '.join(LEAGUE_IDS)}"
        )
    return player


def index_ids_by_league(player):
    """Every league the index has opened a record for her in, as {leagueID: indexID}."""
    records = player.get("indexRecords")
    if not isinstance(records, list):
        raise ShapeError("portal player record: 'indexRecords' is not a list")
    found = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        league_id, index_id = record.get("leagueID"), record.get("indexID")
        if isinstance(league_id, int) and isinstance(index_id, int):
            found[league_id] = index_id
    if not found:
        raise ShapeError("portal player record: no usable indexRecords entries")
    return found


def find_stats_source(player):
    """Which league's index record the on-ice numbers come from.

    Normally her current one. But a call-up joins a club before the index has
    any record of her in that league, so currentLeague can legitimately have no
    indexID for weeks. Falling back to the club league she does have keeps her
    last real season on the signature instead of blanking it, and the fallback
    stops applying by itself the moment the index opens the new record.

    Returns (league name, league id, index id).
    """
    available = index_ids_by_league(player)
    current = player["currentLeague"]

    candidates = [current] if current in CLUB_LEAGUES else []
    candidates += [name for name in CLUB_LEAGUES if name != current]
    for name in candidates:
        league_id = LEAGUE_IDS[name]
        if league_id in available:
            return name, league_id, available[league_id]

    raise ShapeError(
        f"no index record in any club league ({', '.join(CLUB_LEAGUES)}); "
        f"she has records for league IDs {sorted(available)}"
    )


def fetch_team(team_id, league_id):
    team = get_json(INDEX_TEAM.format(team=team_id, league=league_id))
    if not isinstance(team, dict) or not team:
        raise ShapeError(f"index /teams/{team_id}?league={league_id} returned no team")
    require_fields(team, {"name": str, "abbreviation": str}, f"index team {team_id}")
    if team.get("id") != team_id:
        raise ShapeError(f"asked index for team {team_id}, got {team.get('id')}")
    # Returned whole rather than trimmed here: the crest lookup needs
    # nameDetails, which does not belong in data.json.
    return team


def fetch_phase(index_id, league_id, phase):
    records = get_json(INDEX_STATS.format(iid=index_id, league=league_id, phase=phase))
    if not isinstance(records, list):
        raise ShapeError(f"index {phase} stats for {index_id} is not a list")
    spec = {f: int for f in STAT_FIELDS}
    spec["team"] = str  # who she played these games for, which is not always her current club
    for record in records:
        require_fields(record, spec, f"index {phase} stats {index_id}")
    return records


def fetch_stats(index_id, league_id):
    """The newest season she has played, and that season's playoffs once they start.

    One rule drives the whole lifecycle: the display season is the newest season
    with regular-season games, and playoffs only ever attach to that same season.

    So the playoff figures appear when the run begins and stay through the
    offseason, because nothing newer has regular-season games yet. Preseason is
    ignored entirely, which is why exhibition games never displace a real season.
    The moment the next regular season is simmed, the display season advances and
    the playoff figures drop with it.
    """
    regular_records = fetch_phase(index_id, league_id, REGULAR)
    if not regular_records:
        raise ShapeError(f"index returned no regular season records for {index_id}")

    played = [r for r in regular_records if r["gamesPlayed"] > 0]
    regular = max(played or regular_records, key=lambda r: r["season"])
    season = regular["season"]

    advanced = regular.get("advancedStats")
    if not isinstance(advanced, dict):
        raise ShapeError(f"index stats S{season}: 'advancedStats' is not an object")
    missing = [f for f in ADVANCED_FIELDS if not isinstance(advanced.get(f), (int, float))]
    if missing:
        raise ShapeError(f"index stats S{season}: advancedStats missing {', '.join(missing)}")

    stats = {
        "season": season,
        # Whose sweater these numbers were earned in. Usually her current club,
        # but not between a call-up and her first game in the new league, which
        # is exactly when the signature must not imply otherwise.
        "team": regular["team"],
        "regular": {field: regular[field] for field in PHASE_FIELDS},
    }
    stats["regular"]["advanced"] = {field: advanced[field] for field in ADVANCED_FIELDS}

    playoffs = next(
        (
            record
            for record in fetch_phase(index_id, league_id, PLAYOFFS)
            if record["season"] == season and record["gamesPlayed"] > 0
        ),
        None,
    )
    if playoffs:
        stats["playoffs"] = {field: playoffs[field] for field in PHASE_FIELDS}
    return stats


def get_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        # The stacks run to a few megabytes, so this wants a longer rope than the
        # JSON calls get.
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise ShapeError(f"{url} returned HTTP {response.status}")
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise ShapeError(f"{url} unreachable: {exc}") from exc


def symbol_id(team):
    """The sprite key for a club: its city with spaces as underscores."""
    details = team.get("nameDetails")
    if not isinstance(details, dict) or not isinstance(details.get("first"), str):
        raise ShapeError(f"index team {team.get('name')!r} has no nameDetails.first to key on")
    return details["first"].replace(" ", "_")


def fetch_mark(team, league):
    """The club crest, lifted out of its league's sprite stack.

    Written to its own file rather than into data.json: it is markup, not data,
    and data.json is documented as raw API values only. It also changes only on
    a trade, so an unchanged club leaves the file byte-identical and the nightly
    workflow stays quiet.
    """
    key = symbol_id(team)
    stack = get_text(INDEX_STACK.format(league=league.lower()))
    match = re.search(r'<svg[^>]*\sid="%s"[^>]*>.*?</svg>' % re.escape(key), stack, re.DOTALL)
    if not match:
        available = sorted(set(re.findall(r'<svg[^>]*\sid="([^"]+)"', stack)))
        raise ShapeError(
            f"no mark {key!r} in the {league} stack; it holds {len(available)} symbols "
            f"including {', '.join(available[:6])}"
        )
    mark = match.group(0)
    if 'viewBox="' not in mark[: mark.index(">") + 1]:
        raise ShapeError(f"mark {key!r} has no viewBox, so the build cannot scale it")
    return mark


def collect():
    """Everything the build needs: the JSON payload, and the club crest markup."""
    player = fetch_player()
    club_league = player["currentLeague"]
    team = fetch_team(player["currentTeamID"], LEAGUE_IDS[club_league])
    stats_league, stats_league_id, index_id = find_stats_source(player)

    stats = fetch_stats(index_id, stats_league_id)
    stats["league"] = stats_league

    data = {
        "player": {field: player[field] for field in PLAYER_FIELDS},
        "attributes": {name: player["attributes"][name] for name in ATTRIBUTES},
        "team": {"name": team["name"], "abbreviation": team["abbreviation"]},
        "stats": stats,
    }
    return data, fetch_mark(team, club_league)


def main():
    try:
        data, mark = collect()
    except ShapeError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    # sort_keys and a trailing newline keep the file byte-stable between runs,
    # so an unchanged API produces an unchanged file and the workflow can tell.
    DATA_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    LOGO_PATH.write_text(mark + "\n", encoding="utf-8", newline="\n")

    player, stats, team = data["player"], data["stats"], data["team"]
    playoffs = stats.get("playoffs")
    # Name the source of the stats whenever it is not the club she plays for now,
    # so a call-up is visible in the log rather than looking like stale data.
    elsewhere = (
        "" if stats["team"] == team["abbreviation"] and stats["league"] == player["currentLeague"]
        else f" [stats from {stats['league']} {stats['team']}]"
    )
    print(
        f"{player['name']}: {player['totalTPE']} TPE "
        f"({player['appliedTPE']} applied, {player['bankedTPE']} banked), "
        f"{team['name']} ({player['currentLeague']}), S{stats['season']} "
        f"{stats['regular']['gamesPlayed']}gp regular"
        + (f" + {playoffs['gamesPlayed']}gp playoffs" if playoffs else " (no playoff games)")
        + elsewhere
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
