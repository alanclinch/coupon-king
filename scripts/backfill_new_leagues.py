"""
Backfill Premier League, Championship, Scottish Premiership, Champions League
from API-Sports for 2022-2025 seasons.

Existing fixtures/teams from football-data and sportmonks stay in place.
This adds api-sports mappings so the nightly sync won't create duplicates.
16 API calls total (4 leagues x 4 seasons).
"""
import os
import requests
import psycopg2
import time

DB = os.environ["DB_CONNECTION_STRING"]
API_KEY = os.environ["API_SPORTS_KEY"]
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

LEAGUES = {
    39:  "Premier League",
    40:  "Championship",
    179: "Scottish Premiership",
    2:   "Champions League",
}

SEASONS = [2022, 2023, 2024, 2025]


def get_conn():
    return psycopg2.connect(DB)


def upsert_team(cur, api_team_id, name, local_league_id):
    logo_url = f"https://media.api-sports.io/football/teams/{api_team_id}.png"
    cur.execute("""
        SELECT local_id FROM api_id_map
        WHERE table_name = 'teams' AND api_source = 'api-sports' AND api_id = %s
    """, (str(api_team_id),))
    row = cur.fetchone()
    if row:
        local_id = row[0]
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
        VALUES ('teams', %s, 'api-sports', %s) ON CONFLICT DO NOTHING
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
        VALUES (%s, %s, %s, %s, %s, 'scheduled', %s) RETURNING id
    """, (home_id, away_id, league_id, kickoff, season, matchday))
    local_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO api_id_map (table_name, local_id, api_source, api_id)
        VALUES ('fixtures', %s, 'api-sports', %s) ON CONFLICT DO NOTHING
    """, (local_id, str(api_fixture_id)))
    return local_id


def upsert_result(cur, fixture_local_id, home_goals, away_goals, home_ht, away_ht):
    cur.execute("""
        INSERT INTO results (fixture_id, home_goals, away_goals, home_goals_ht, away_goals_ht)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (fixture_id) DO UPDATE
        SET home_goals = EXCLUDED.home_goals, away_goals = EXCLUDED.away_goals,
            home_goals_ht = EXCLUDED.home_goals_ht, away_goals_ht = EXCLUDED.away_goals_ht
    """, (fixture_local_id, home_goals, away_goals, home_ht, away_ht))


def main():
    for api_league_id, league_name in LEAGUES.items():
        for season in SEASONS:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM leagues WHERE name = %s", (league_name,))
                row = cur.fetchone()
            if not row:
                print(f"SKIP: {league_name} not found in DB")
                conn.close()
                continue
            local_league_id = row[0]

            print(f"Fetching {league_name} season {season}...")
            r = requests.get(f"{BASE_URL}/fixtures?league={api_league_id}&season={season}",
                             headers=HEADERS)
            r.raise_for_status()
            fixtures = r.json().get("response", [])
            print(f"  {len(fixtures)} fixtures from API")

            count_f = count_r = 0
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
                    fixture_local = upsert_fixture(cur, fixture["id"], home_local, away_local,
                                                   local_league_id, kickoff, str(season), matchday)
                    count_f += 1
                    if fixture.get("status", {}).get("short") == "FT":
                        hg, ag = goals.get("home"), goals.get("away")
                        if hg is not None and ag is not None:
                            ht = score.get("halftime", {})
                            upsert_result(cur, fixture_local, hg, ag, ht.get("home"), ht.get("away"))
                            count_r += 1

            conn.commit()
            conn.close()
            print(f"  Committed: {count_f} fixtures, {count_r} results")
            time.sleep(1)

    print("Done.")


if __name__ == "__main__":
    main()
