"""
One-off backfill script: pulls 3 seasons of historical data from API-Sports
for all leagues covered by sync_api_sports.py.

This is NOT part of the nightly sync. Run once to fill historical data gaps.
Uses ~36 API calls (12 leagues × 3 seasons).

Usage:
    DB_CONNECTION_STRING=... API_SPORTS_KEY=... python scripts/backfill_api_sports.py
"""
import os
import requests
import psycopg2
import time
from datetime import datetime

DB = os.environ["DB_CONNECTION_STRING"]
API_KEY = os.environ["API_SPORTS_KEY"]
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUES = {
    180: "Scottish Championship",
    183: "Scottish League One",
    184: "Scottish League Two",
    528: "Scottish Cup",
    529: "Scottish League Cup",
    41:  "League One",
    42:  "League Two",
    43:  "National League",
    45:  "FA Cup",
    48:  "EFL Cup",
    3:   "Europa League",
    848: "Conference League",
}

SEASONS = [2022, 2023, 2024, 2025]


def get_conn():
    return psycopg2.connect(DB)


def get_local_league_id(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM leagues WHERE name = %s", (name,))
        row = cur.fetchone()
        return row[0] if row else None


def upsert_team(cur, api_team_id, name, local_league_id):
    cur.execute("""
        SELECT local_id FROM api_id_map
        WHERE table_name = 'teams' AND api_source = 'api-sports' AND api_id = %s
    """, (str(api_team_id),))
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute("""
        INSERT INTO teams (name, league_id, active)
        VALUES (%s, %s, TRUE)
        RETURNING id
    """, (name, local_league_id))
    local_id = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO api_id_map (table_name, local_id, api_source, api_id)
        VALUES ('teams', %s, 'api-sports', %s)
        ON CONFLICT DO NOTHING
    """, (local_id, str(api_team_id)))

    return local_id


def upsert_fixture(cur, api_fixture_id, home_id, away_id, league_id, kickoff, season, matchday):
    cur.execute("""
        SELECT local_id FROM api_id_map
        WHERE table_name = 'fixtures' AND api_source = 'api-sports' AND api_id = %s
    """, (str(api_fixture_id),))
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute("""
        INSERT INTO fixtures (home_team_id, away_team_id, league_id, season, kickoff_time, status, matchday)
        VALUES (%s, %s, %s, %s, %s, 'scheduled', %s)
        RETURNING id
    """, (home_id, away_id, league_id, season, kickoff, matchday))
    local_id = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO api_id_map (table_name, local_id, api_source, api_id)
        VALUES ('fixtures', %s, 'api-sports', %s)
        ON CONFLICT DO NOTHING
    """, (local_id, str(api_fixture_id)))

    return local_id


def upsert_result(cur, fixture_local_id, home_goals, away_goals, home_ht, away_ht):
    cur.execute("""
        INSERT INTO results (fixture_id, home_goals, away_goals, home_goals_ht, away_goals_ht)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (fixture_id) DO UPDATE
        SET home_goals = EXCLUDED.home_goals,
            away_goals = EXCLUDED.away_goals,
            home_goals_ht = EXCLUDED.home_goals_ht,
            away_goals_ht = EXCLUDED.away_goals_ht
    """, (fixture_local_id, home_goals, away_goals, home_ht, away_ht))


def fetch_fixtures(league_id, season):
    url = f"{BASE_URL}/fixtures?league={league_id}&season={season}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json().get("response", [])


def main():
    total_fixtures = 0
    total_results = 0
    api_calls = 0

    print(f"Backfill starting: {len(LEAGUES)} leagues × {len(SEASONS)} seasons = up to {len(LEAGUES) * len(SEASONS)} API calls")
    print()

    for api_league_id, league_name in LEAGUES.items():
        # Fresh connection per league to avoid Neon idle timeouts
        conn = get_conn()
        local_league_id = get_local_league_id(conn, league_name)
        if not local_league_id:
            print(f"SKIP: League not found in DB: {league_name}")
            conn.close()
            continue

        for season in SEASONS:
            print(f"Fetching {league_name} ({api_league_id}) season {season}...")
            try:
                fixtures = fetch_fixtures(api_league_id, season)
                api_calls += 1
            except Exception as e:
                print(f"  ERROR: {e}")
                time.sleep(2)
                continue

            print(f"  {len(fixtures)} fixtures from API")

            season_fixtures = 0
            season_results = 0

            # Reconnect before each season's DB writes
            try:
                conn.cursor().execute("SELECT 1")
            except Exception:
                conn = get_conn()

            with conn.cursor() as cur:
                for f in fixtures:
                    fixture = f.get("fixture", {})
                    teams = f.get("teams", {})
                    goals = f.get("goals", {})
                    score = f.get("score", {})

                    home = teams.get("home", {})
                    away = teams.get("away", {})
                    if not home.get("id") or not away.get("id"):
                        continue

                    kickoff = fixture.get("date")
                    if not kickoff or len(kickoff) < 10:
                        continue

                    home_local = upsert_team(cur, home["id"], home["name"], local_league_id)
                    away_local = upsert_team(cur, away["id"], away["name"], local_league_id)

                    matchday = f.get("league", {}).get("round")
                    season_label = str(season)

                    fixture_local = upsert_fixture(
                        cur, fixture["id"], home_local, away_local,
                        local_league_id, kickoff, season_label, matchday
                    )
                    season_fixtures += 1

                    status = fixture.get("status", {}).get("short")
                    if status == "FT":
                        home_goals = goals.get("home")
                        away_goals = goals.get("away")
                        ht = score.get("halftime", {})
                        if home_goals is not None and away_goals is not None:
                            upsert_result(
                                cur, fixture_local,
                                home_goals, away_goals,
                                ht.get("home"), ht.get("away")
                            )
                            season_results += 1

            conn.commit()
            total_fixtures += season_fixtures
            total_results += season_results
            print(f"  Committed: {season_fixtures} fixtures, {season_results} results")

            time.sleep(1)

        conn.close()
    print()
    print(f"Backfill complete: {api_calls} API calls, {total_fixtures} fixtures, {total_results} results")
    print("Next step: run build_form_cache.py and build_ratings.py to rebuild caches.")


if __name__ == "__main__":
    main()
