"""
Record the top 20 fixtures per bet type for the upcoming weekend.
Runs Friday night so picks are captured before Saturday kickoffs.
Stores in weekend_picks table for Monday grading.
"""
import os
from datetime import datetime, timezone, timedelta

import psycopg2

DB = os.environ["DB_CONNECTION_STRING"]
BET_TYPES = ["BTTS", "OVER25", "WIN", "BTTS_WIN", "BTTS_NODRAW", "BTTS_OVER25"]
TOP_N = 20


def get_conn():
    return psycopg2.connect(DB)


def get_week_start(today):
    """Return the Saturday of the current/upcoming weekend."""
    days_until_sat = (5 - today.weekday()) % 7
    if days_until_sat == 0:
        days_until_sat = 7
    return today + timedelta(days=days_until_sat)


def main():
    conn = get_conn()
    now = datetime.now(timezone.utc)
    today = now.date()
    week_start = get_week_start(today)
    weekend_end = week_start + timedelta(days=1)  # Saturday + Sunday

    print(f"Recording weekend picks for {week_start} – {weekend_end}")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.id, f.home_team_id, f.away_team_id,
                   ht.name AS home_team, at.name AS away_team,
                   l.name AS league_name, f.kickoff_time,
                   fs.bet_type, fs.score, fs.pick
            FROM fixtures f
            JOIN teams ht ON ht.id = f.home_team_id
            JOIN teams at ON at.id = f.away_team_id
            JOIN leagues l ON l.id = f.league_id
            JOIN fixture_scores fs ON fs.fixture_id = f.id
            LEFT JOIN results r ON r.fixture_id = f.id
            WHERE f.kickoff_time::date >= %s
              AND f.kickoff_time::date <= %s
              AND r.fixture_id IS NULL
            ORDER BY fs.bet_type, fs.score DESC
        """, (week_start, weekend_end))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    if not rows:
        print("  No scored fixtures found for this weekend.")
        conn.close()
        return

    # Group by bet type, take top N
    by_bet = {}
    for row in rows:
        r = dict(zip(cols, row))
        bt = r["bet_type"]
        if bt not in by_bet:
            by_bet[bt] = []
        if len(by_bet[bt]) < TOP_N:
            by_bet[bt].append(r)

    inserted = 0
    with conn.cursor() as cur:
        for bet_type, picks in by_bet.items():
            for pick in picks:
                cur.execute("""
                    INSERT INTO weekend_picks
                        (week_start, fixture_id, bet_type, score, pick,
                         home_team, away_team, league_name, kickoff_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (week_start, fixture_id, bet_type) DO NOTHING
                """, (
                    week_start,
                    pick["id"],
                    bet_type,
                    pick["score"],
                    pick["pick"],
                    pick["home_team"],
                    pick["away_team"],
                    pick["league_name"],
                    pick["kickoff_time"],
                ))
                inserted += 1

    conn.commit()
    conn.close()
    print(f"  Recorded {inserted} picks across {len(by_bet)} bet types")
    for bt, picks in by_bet.items():
        print(f"    {bt}: {len(picks)} picks, top score {picks[0]['score'] if picks else 0}")
    print("Weekend pick recording complete")


if __name__ == "__main__":
    main()
