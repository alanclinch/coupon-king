import os
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2

DB = os.environ["DB_CONNECTION_STRING"]


def get_conn():
    return psycopg2.connect(DB)


def main():
    conn = get_conn()
    print(f"Form cache build started at {datetime.now(timezone.utc).isoformat()}")

    # Fetch all completed results in one query, ordered most-recent-first
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.home_team_id, f.away_team_id, r.home_goals, r.away_goals, f.kickoff_time
            FROM fixtures f
            JOIN results r ON r.fixture_id = f.id
            ORDER BY f.kickoff_time DESC
        """)
        all_results = cur.fetchall()

    print(f"  Found {len(all_results)} completed matches")

    # ── TEAM FORM ────────────────────────────────────────────────────────────
    # Collect last 5 matches per team (already ordered desc by kickoff)
    team_matches: dict[int, list] = defaultdict(list)
    for home_id, away_id, home_goals, away_goals, _ in all_results:
        for team_id in (home_id, away_id):
            if len(team_matches[team_id]) < 5:
                team_matches[team_id].append((home_id, away_id, home_goals, away_goals))

    form_rows = []
    for team_id, matches in team_matches.items():
        wins = draws = losses = goals_scored = goals_conceded = clean_sheets = 0
        form_chars = []
        for home_id, away_id, hg, ag in matches:
            if home_id == team_id:
                scored, conceded = hg, ag
            else:
                scored, conceded = ag, hg
            goals_scored += scored
            goals_conceded += conceded
            if conceded == 0:
                clean_sheets += 1
            if scored > conceded:
                wins += 1
                form_chars.append("W")
            elif scored == conceded:
                draws += 1
                form_chars.append("D")
            else:
                losses += 1
                form_chars.append("L")
        form_rows.append((
            team_id, len(matches), wins, draws, losses,
            goals_scored, goals_conceded, clean_sheets, "".join(form_chars),
        ))

    with conn.cursor() as cur:
        for row in form_rows:
            cur.execute("""
                INSERT INTO team_form
                    (team_id, played, wins, draws, losses,
                     goals_scored, goals_conceded, clean_sheets, form_string, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (team_id) DO UPDATE SET
                    played        = EXCLUDED.played,
                    wins          = EXCLUDED.wins,
                    draws         = EXCLUDED.draws,
                    losses        = EXCLUDED.losses,
                    goals_scored  = EXCLUDED.goals_scored,
                    goals_conceded= EXCLUDED.goals_conceded,
                    clean_sheets  = EXCLUDED.clean_sheets,
                    form_string   = EXCLUDED.form_string,
                    updated_at    = NOW()
            """, row)
    conn.commit()
    print(f"  Updated form for {len(form_rows)} teams")

    # ── HEAD TO HEAD ─────────────────────────────────────────────────────────
    h2h: dict = defaultdict(lambda: {
        "meetings": 0, "team1_wins": 0, "team2_wins": 0,
        "draws": 0, "team1_goals": 0, "team2_goals": 0,
    })

    for home_id, away_id, home_goals, away_goals, _ in all_results:
        t1, t2 = min(home_id, away_id), max(home_id, away_id)
        rec = h2h[(t1, t2)]
        rec["meetings"] += 1
        if home_id == t1:
            rec["team1_goals"] += home_goals
            rec["team2_goals"] += away_goals
        else:
            rec["team1_goals"] += away_goals
            rec["team2_goals"] += home_goals
        if home_goals > away_goals:
            winner = home_id
        elif away_goals > home_goals:
            winner = away_id
        else:
            winner = None
        if winner is None:
            rec["draws"] += 1
        elif winner == t1:
            rec["team1_wins"] += 1
        else:
            rec["team2_wins"] += 1

    with conn.cursor() as cur:
        for (t1, t2), rec in h2h.items():
            cur.execute("""
                INSERT INTO head_to_head
                    (team1_id, team2_id, meetings,
                     team1_wins, team2_wins, draws,
                     team1_goals, team2_goals, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (team1_id, team2_id) DO UPDATE SET
                    meetings   = EXCLUDED.meetings,
                    team1_wins = EXCLUDED.team1_wins,
                    team2_wins = EXCLUDED.team2_wins,
                    draws      = EXCLUDED.draws,
                    team1_goals= EXCLUDED.team1_goals,
                    team2_goals= EXCLUDED.team2_goals,
                    updated_at = NOW()
            """, (t1, t2, rec["meetings"], rec["team1_wins"], rec["team2_wins"],
                  rec["draws"], rec["team1_goals"], rec["team2_goals"]))
    conn.commit()
    print(f"  Updated H2H for {len(h2h)} team pairs")

    conn.close()
    print("Form cache build complete")


if __name__ == "__main__":
    main()
