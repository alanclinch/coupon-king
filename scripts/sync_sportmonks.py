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

def upsert_team(cur, api_team_id, name, local_league_id, logo_url=None):
    cur.execute("""
        SELECT local_id FROM api_id_map
        WHERE table_name = 'teams' AND api_source = 'sportmonks' AND api_id = %s
    """, (str(api_team_id),))
    row = cur.fetchone()
    if row:
        local_id = row[0]
        if logo_url:
            cur.execute("UPDATE teams SET logo_url = %s WHERE id = %s AND logo_url IS NULL",
                        (logo_url, local_id))
        return local_id

    cur.execute("""
        INSERT INTO teams (name, league_id, active, logo_url)
        VALUES (%s, %s, TRUE, %s)
        RETURNING id
    """, (name, local_league_id, logo_url))
    local_id = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO api_id_map (table_name, local_id, api_source, api_id)
        VALUES ('teams', %s, 'sportmonks', %s)
        ON CONFLICT DO NOTHING
    """, (local_id, str(api_team_id)))

    return local_id

def upsert_fixture(cur, api_fixture_id, home_id, away_id, league_id, kickoff, season, matchday):
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

    return local_id

def upsert_result(cur, fixture_local_id, home_goals, away_goals):
    cur.execute("""
        INSERT INTO results (fixture_id, home_goals, away_goals)
        VALUES (%s, %s, %s)
        ON CONFLICT (fixture_id) DO UPDATE
        SET home_goals = EXCLUDED.home_goals,
            away_goals = EXCLUDED.away_goals
    """, (fixture_local_id, home_goals, away_goals))

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

def get_seasons():
    url = f"{BASE_URL}/seasons?filters=seasonLeagues:{SCOTTISH_PREM_ID}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    all_seasons = data.get("data", [])
    print(f"  Found {len(all_seasons)} seasons for Scottish Premiership")
    for s in all_seasons:
        print(f"    Season: {s.get('name')} id={s.get('id')}")

    now = datetime.now()
    current_year = now.year if now.month >= 7 else now.year - 1

    if os.environ.get('FULL_SYNC') == '1':
        cutoff = current_year - 2
        return [s for s in all_seasons if s.get('name', '').startswith(str(cutoff)) or
                any(str(y) in s.get('name', '') for y in range(cutoff, current_year + 1))]
    else:
        return [s for s in all_seasons if str(current_year) in s.get('name', '')]

def main():
    conn = get_conn()
    local_league_id = get_local_league_id(conn)
    if not local_league_id:
        print("Scottish Premiership not found in leagues table")
        return

    seasons = get_seasons()
    for season in seasons:
        season_id = season["id"]
        season_label = season.get("name", str(season_id))
        print(f"Syncing season: {season_label}")

        fixtures = fetch_fixtures(season_id)
        print(f"  Found {len(fixtures)} fixtures")

        with conn.cursor() as cur:
            for f in fixtures:
                participants = {p["meta"]["location"]: p for p in f.get("participants", [])}
                home = participants.get("home")
                away = participants.get("away")
                if not home or not away:
                    continue

                home_local = upsert_team(cur, home["id"], home["name"], local_league_id, home.get("image_path"))
                away_local = upsert_team(cur, away["id"], away["name"], local_league_id, away.get("image_path"))

                kickoff = f.get("starting_at")
                matchday = f.get("round_id")

                fixture_local = upsert_fixture(
                    cur, f["id"], home_local, away_local,
                    local_league_id, kickoff, season_label, matchday
                )

                # Save result if finished
                scores = f.get("scores", [])
                home_goals = away_goals = None
                for score in scores:
                    if score.get("description") == "CURRENT":
                        participant = score.get("score", {}).get("participant")
                        goals = score.get("score", {}).get("goals")
                        if participant == "home":
                            home_goals = goals
                        elif participant == "away":
                            away_goals = goals
                if home_goals is not None and away_goals is not None:
                    upsert_result(cur, fixture_local, home_goals, away_goals)

        conn.commit()
        print(f"  Committed season {season_label}")

    conn.close()
    print("Sportmonks sync complete")

if __name__ == "__main__":
    main()
