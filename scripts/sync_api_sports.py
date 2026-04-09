import os
import requests
import psycopg2
import time
from datetime import datetime

DB = os.environ["DB_CONNECTION_STRING"]
API_KEY = os.environ["API_SPORTS_KEY"]
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# API-Sports league IDs mapped to our league names
LEAGUES = {
    179: "Scottish Championship",
    180: "Scottish League One",
    181: "Scottish League Two",
    528: "Scottish Cup",
    529: "Scottish League Cup",
    39:  "League One",
    40:  "League Two",
    41:  "National League",
    45:  "FA Cup",
    48:  "EFL Cup",
    3:   "Europa League",
    848: "Conference League",
}

def get_conn():
    return psycopg2.connect(DB)

def get_local_league_id(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM leagues WHERE name = %s", (name,))
        row = cur.fetchone()
        return row[0] if row else None

def upsert_team(conn, api_team_id, name, local_league_id):
    with conn.cursor() as cur:
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

        conn.commit()
        return local_id

def upsert_fixture(conn, api_fixture_id, home_id, away_id, league_id, kickoff, season, matchday):
    with conn.cursor() as cur:
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

        conn.commit()
        return local_id

def upsert_result(conn, fixture_local_id, home_goals, away_goals, home_ht, away_ht):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO results (fixture_id, home_goals, away_goals, home_goals_ht, away_goals_ht)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fixture_id) DO UPDATE
            SET home_goals = EXCLUDED.home_goals,
                away_goals = EXCLUDED.away_goals,
                home_goals_ht = EXCLUDED.home_goals_ht,
                away_goals_ht = EXCLUDED.away_goals_ht
        """, (fixture_local_id, home_goals, away_goals, home_ht, away_ht))
        conn.commit()

def fetch_fixtures(league_id, season):
    url = f"{BASE_URL}/fixtures?league={league_id}&season={season}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json().get("response", [])

def main():
    conn = get_conn()

    now = datetime.now()
    # Football seasons start in July/August, so April 2026 = season 2025
    current_season = now.year if now.month >= 7 else now.year - 1
    if os.environ.get('FULL_SYNC') == '1':
        seasons = [current_season - i for i in range(3)]
        print("Full sync mode: syncing 3 seasons")
    else:
        seasons = [current_season]
        print(f"Nightly sync mode: syncing current season {current_season} only")

    for api_league_id, league_name in LEAGUES.items():
        local_league_id = get_local_league_id(conn, league_name)
        if not local_league_id:
            print(f"League not found: {league_name}")
            continue

        for season in seasons:
            print(f"Syncing {league_name} season {season}...")
            try:
                fixtures = fetch_fixtures(api_league_id, season)
            except Exception as e:
                print(f"  Error fetching {league_name} {season}: {e}")
                continue

            print(f"  Found {len(fixtures)} fixtures")

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

                home_local = upsert_team(conn, home["id"], home["name"], local_league_id)
                away_local = upsert_team(conn, away["id"], away["name"], local_league_id)

                matchday = f.get("league", {}).get("round")
                season_label = str(season)

                fixture_local = upsert_fixture(
                    conn, fixture["id"], home_local, away_local,
                    local_league_id, kickoff, season_label, matchday
                )

                # Save result if finished
                status = fixture.get("status", {}).get("short")
                if status == "FT":
                    home_goals = goals.get("home")
                    away_goals = goals.get("away")
                    ht = score.get("halftime", {})
                    if home_goals is not None and away_goals is not None:
                        upsert_result(
                            conn, fixture_local,
                            home_goals, away_goals,
                            ht.get("home"), ht.get("away")
                        )

            # Pause between calls to respect rate limits
            time.sleep(1)

    conn.close()
    print("API-Sports sync complete")

if __name__ == "__main__":
    main()
