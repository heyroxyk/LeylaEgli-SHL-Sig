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
import sys
import urllib.error
import urllib.request

PLAYER_ID = 2500  # portal pid, from portal.simulationhockey.com/player/2500

PORTAL_PLAYER = "https://portal.simulationhockey.com/api/v1/player?pid={pid}"
INDEX_TEAMS = "https://index.simulationhockey.com/api/v1/teams?league={league}"
INDEX_STATS = "https://index.simulationhockey.com/api/v1/players/stats/{iid}?league={league}"

# The index numbers its leagues in /api/v1/leagues, and the portal's own
# indexRecords use that same numbering, so one map serves both lookups.
LEAGUE_IDS = {"SHL": 0, "SMJHL": 1, "IIHF": 2, "WJC": 3}

DATA_PATH = pathlib.Path(__file__).parent / "data.json"

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

STAT_FIELDS = (
    "season", "gamesPlayed", "goals", "assists", "points", "plusMinus", "pim",
    "hits", "shotsBlocked", "takeaways", "giveaways", "shotsOnGoal", "timeOnIce",
    "ppPoints", "shPoints", "ppTimeOnIce", "shTimeOnIce",
)

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


def find_index_id(player, league_id):
    records = player.get("indexRecords")
    if not isinstance(records, list):
        raise ShapeError("portal player record: 'indexRecords' is not a list")
    for record in records:
        if isinstance(record, dict) and record.get("leagueID") == league_id:
            index_id = record.get("indexID")
            if not isinstance(index_id, int):
                raise ShapeError(f"indexRecords entry for league {league_id} has no usable indexID")
            return index_id
    raise ShapeError(
        f"no indexRecords entry for league {league_id} "
        f"({player['currentLeague']}); saw {[r.get('leagueID') for r in records]}"
    )


def fetch_team(team_id, league_id):
    teams = get_json(INDEX_TEAMS.format(league=league_id))
    if not isinstance(teams, list) or not teams:
        raise ShapeError(f"index /teams?league={league_id} returned no teams")
    for team in teams:
        if isinstance(team, dict) and team.get("id") == team_id:
            require_fields(team, {"name": str, "abbreviation": str}, f"index team {team_id}")
            return {"name": team["name"], "abbreviation": team["abbreviation"]}
    raise ShapeError(
        f"no team with id {team_id} in league {league_id}; "
        f"saw ids {sorted(t.get('id') for t in teams if isinstance(t, dict))}"
    )


def fetch_stats(index_id, league_id):
    seasons = get_json(INDEX_STATS.format(iid=index_id, league=league_id))
    if not isinstance(seasons, list) or not seasons:
        raise ShapeError(f"index /players/stats/{index_id} returned no seasons")
    for record in seasons:
        require_fields(record, {f: int for f in STAT_FIELDS}, f"index stats {index_id}")

    # Prefer the newest season she has actually played. At a season rollover the
    # index publishes the new season before any games are simmed, and a rail full
    # of zeroes is worse than last season's real line. The S-number on the sig
    # names whichever season is shown, so this stays honest either way.
    played = [r for r in seasons if r["gamesPlayed"] > 0]
    latest = max(played or seasons, key=lambda r: r["season"])

    advanced = latest.get("advancedStats")
    if not isinstance(advanced, dict):
        raise ShapeError(f"index stats S{latest['season']}: 'advancedStats' is not an object")
    missing = [f for f in ADVANCED_FIELDS if not isinstance(advanced.get(f), (int, float))]
    if missing:
        raise ShapeError(
            f"index stats S{latest['season']}: advancedStats missing {', '.join(missing)}"
        )

    stats = {field: latest[field] for field in STAT_FIELDS}
    stats["advanced"] = {field: advanced[field] for field in ADVANCED_FIELDS}
    return stats


def collect():
    player = fetch_player()
    league_id = LEAGUE_IDS[player["currentLeague"]]
    index_id = find_index_id(player, league_id)
    return {
        "player": {field: player[field] for field in PLAYER_FIELDS},
        "attributes": {name: player["attributes"][name] for name in ATTRIBUTES},
        "team": fetch_team(player["currentTeamID"], league_id),
        "stats": fetch_stats(index_id, league_id),
    }


def main():
    try:
        data = collect()
    except ShapeError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    # sort_keys and a trailing newline keep the file byte-stable between runs,
    # so an unchanged API produces an unchanged file and the workflow can tell.
    DATA_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    player = data["player"]
    print(
        f"{player['name']}: {player['totalTPE']} TPE "
        f"({player['appliedTPE']} applied, {player['bankedTPE']} banked), "
        f"{data['team']['name']}, S{data['stats']['season']} stats"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
