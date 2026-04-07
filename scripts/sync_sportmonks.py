import os
import requests
import psycopg2
from datetime import datetime

DB = os.environ["DB_CONNECTION_STRING"]
API_KEY = os.environ["SPORTMONKS_API_KEY"]
BASE_URL = "https://api.sportmonks.com/v3/football"
HEADERS = {"Authorization": API_KEY}

# Scottish Premiership league ID on Sportmonks
SCOTTISH_PREM_ID = 501

def get_conn():
    return psycopg2.connect(DB)

def get_local_league_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM leagues WHERE name = 'Scottish Premiership'")
        row = cur.fetchone()
        return row[0] if row else None

def upsert_team(conn, api_team_id, name, local_league_id):
    with conn.cursor() as cur:
        # Check if we already have this team mapped
        cur.execute("""
            SELECT local_id FROM api_id_map
            WHERE table_name = 'teams' AND api_source = 'sportmonks' AND api_id = %s
        """, (str(api_team_id),))
        row = cur.fetchone()
        if row:
            return row[0]

        # Insert new team
        cur.execute("""
            INSERT INTO teams (name, league_id, active)
            VALUES (%s, %s, TRUE)
            RETURNING id
        """, (name, local_league_id))
        local_id = cur.fetchone()[0]

        # Save the mapping
        cur.execute("""
            INSERT INTO api_id_map (table_name, local_id, api_source, api_id)
            VALUES ('teams', %s, 'sportmonks', %s)
            ON CONFLICT DO NOTHING
        """, (local_id, str(api_team_id)))

        conn.commit()
        return local_id

def upsert_fixture(conn, api_fixture_id, home_id, away_id, league_id, kickoff, season, matchday):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT local_id FROM api_id_map
            WHERE table_name = 'fixtures' AND api_source = 'sportmonks' AND api_id = %s
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
            VALUES ('fixtures', %s, 'sportmonks', %s)
            ON CONFLICT DO NOTHING
        """, (local_id, str(api_fixture_id)))

        conn.commit()
        return local_id

def upsert_result(conn, fixture_local_id, home_goals, away_goals):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO results (fixture_id, home_goals, away_goals)
            VALUES (%s, %s, %s)
            ON CONFLICT (fixture_id) DO UPDATE
            SET home_goals = EXCLUDED.home_goals,
                away_goals = EXCLUDED.away_goals
        """, (fixture_local_id, home_goals, away_goals))
        conn.commit()

def fetch_fixtures(season_id):
    url = f"{BASE_URL}/fixtures?filters=fixtureSeasons:{season_id}&include=participants;scores&per_page=50"
    fixtures = []
    while url:
        r = requests.get(url, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        fixtures.extend(data.get("data", []))
        url = data.get("pagination", {}).get("next_page")
    return fixtures

def get_current_seasons():
    url = f"{BASE_URL}/leagues/{SCOTTISH_PREM_ID}?include=currentSeason"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    season = data["data"].get("currentSeason")
    return [season] if season else []

def main():
    conn = get_conn()
    local_league_id = get_local_league_id(conn)
    if not local_league_id:
        print("Scottish Premiership not found in leagues table")
        return

    seasons = get_current_seasons()
    for season in seasons:
        season_id = season["id"]
        season_label = season.get("name", str(season_id))
        print(f"Syncing season: {season_label}")

        fixtures = fetch_fixtures(season_id)
        print(f"  Found {len(fixtures)} fixtures")

        for f in fixtures:
            participants = {p["meta"]["location"]: p for p in f.get("participants", [])}
            home = participants.get("home")
            away = participants.get("away")
            if not home or not away:
                continue

            home_local = upsert_team(conn, home["id"], home["name"], local_league_id)
            away_local = upsert_team(conn, away["id"], away["name"], local_league_id)

            kickoff = f.get("starting_at")
            matchday = f.get("round_id")

            fixture_local = upsert_fixture(
                conn, f["id"], home_local, away_local,
                local_league_id, kickoff, season_label, matchday
            )

            # Save result if finished
            scores = f.get("scores", [])
            for score in scores:
                if score.get("description") == "CURRENT":
                    home_goals = score.get("score", {}).get("goals") if score.get("score", {}).get("participant") == "home" else None
                    away_goals = score.get("score", {}).get("goals") if score.get("score", {}).get("participant") == "away" else None
                    if home_goals is not None and away_goals is not None:
                        upsert_result(conn, fixture_local, home_goals, away_goals)

    conn.close()
    print("Sportmonks sync complete")

if __name__ == "__main__":
    main()
