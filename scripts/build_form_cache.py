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

    # Fetch ALL completed results, most recent first
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.home_team_id, f.away_team_id, r.home_goals, r.away_goals, f.kickoff_time
            FROM fixtures f
            JOIN results r ON r.fixture_id = f.id
            ORDER BY f.kickoff_time DESC
        """)
        all_results = cur.fetchall()

    print(f"  Found {len(all_results)} completed matches")

    # ── Accumulate home and away stats separately across all history ─────────
    home_stats = defaultdict(lambda: {
        "played": 0, "wins": 0, "draws": 0, "losses": 0,
        "goals_scored": 0, "goals_conceded": 0, "clean_sheets": 0,
        "btts": 0, "over25": 0,
    })
    away_stats = defaultdict(lambda: {
        "played": 0, "wins": 0, "draws": 0, "losses": 0,
        "goals_scored": 0, "goals_conceded": 0, "clean_sheets": 0,
        "btts": 0, "over25": 0,
    })
    # Last 10 results per team (results already ordered desc, so first = most recent)
    recent_matches = defaultdict(list)

    for home_id, away_id, hg, ag, _ in all_results:
        btts = 1 if hg > 0 and ag > 0 else 0
        over25 = 1 if hg + ag > 2 else 0

        h = home_stats[home_id]
        h["played"] += 1
        h["goals_scored"] += hg
        h["goals_conceded"] += ag
        h["clean_sheets"] += 1 if ag == 0 else 0
        h["btts"] += btts
        h["over25"] += over25
        if hg > ag:   h["wins"] += 1
        elif hg == ag: h["draws"] += 1
        else:          h["losses"] += 1

        a = away_stats[away_id]
        a["played"] += 1
        a["goals_scored"] += ag
        a["goals_conceded"] += hg
        a["clean_sheets"] += 1 if hg == 0 else 0
        a["btts"] += btts
        a["over25"] += over25
        if ag > hg:   a["wins"] += 1
        elif ag == hg: a["draws"] += 1
        else:          a["losses"] += 1

        if len(recent_matches[home_id]) < 10:
            c = "W" if hg > ag else "D" if hg == ag else "L"
            recent_matches[home_id].append((hg, ag, c))
        if len(recent_matches[away_id]) < 10:
            c = "W" if ag > hg else "D" if ag == hg else "L"
            recent_matches[away_id].append((ag, hg, c))

    all_team_ids = set(home_stats) | set(away_stats)
    form_rows = []

    for team_id in all_team_ids:
        h = home_stats[team_id]
        a = away_stats[team_id]
        rec = recent_matches[team_id]

        # Overall combined
        total_played = h["played"] + a["played"]
        total_wins = h["wins"] + a["wins"]
        total_draws = h["draws"] + a["draws"]
        total_losses = h["losses"] + a["losses"]
        total_scored = h["goals_scored"] + a["goals_scored"]
        total_conceded = h["goals_conceded"] + a["goals_conceded"]
        total_cs = h["clean_sheets"] + a["clean_sheets"]

        form_string = "".join(c for _, _, c in rec)
        rec_played = len(rec)
        rec_wins = sum(1 for _, _, c in rec if c == "W")
        rec_draws = sum(1 for _, _, c in rec if c == "D")
        rec_losses = sum(1 for _, _, c in rec if c == "L")
        rec_scored = sum(s for s, _, _ in rec)
        rec_conceded = sum(cn for _, cn, _ in rec)

        form_rows.append((
            team_id,
            # overall
            total_played, total_wins, total_draws, total_losses,
            total_scored, total_conceded, total_cs,
            form_string,
            # home
            h["played"], h["wins"], h["draws"], h["losses"],
            h["goals_scored"], h["goals_conceded"], h["clean_sheets"],
            h["btts"], h["over25"],
            # away
            a["played"], a["wins"], a["draws"], a["losses"],
            a["goals_scored"], a["goals_conceded"], a["clean_sheets"],
            a["btts"], a["over25"],
            # recent
            rec_played, rec_wins, rec_draws, rec_losses,
            rec_scored, rec_conceded,
        ))

    with conn.cursor() as cur:
        for row in form_rows:
            cur.execute("""
                INSERT INTO team_form
                    (team_id,
                     played, wins, draws, losses,
                     goals_scored, goals_conceded, clean_sheets, form_string,
                     played_home, wins_home, draws_home, losses_home,
                     goals_scored_home, goals_conceded_home, clean_sheets_home,
                     btts_home, over25_home,
                     played_away, wins_away, draws_away, losses_away,
                     goals_scored_away, goals_conceded_away, clean_sheets_away,
                     btts_away, over25_away,
                     recent_played, recent_wins, recent_draws, recent_losses,
                     recent_goals_scored, recent_goals_conceded,
                     updated_at)
                VALUES (
                    %s,
                    %s,%s,%s,%s, %s,%s,%s,%s,
                    %s,%s,%s,%s, %s,%s,%s,%s,%s,
                    %s,%s,%s,%s, %s,%s,%s,%s,%s,
                    %s,%s,%s,%s, %s,%s,
                    NOW()
                )
                ON CONFLICT (team_id) DO UPDATE SET
                    played=EXCLUDED.played, wins=EXCLUDED.wins,
                    draws=EXCLUDED.draws, losses=EXCLUDED.losses,
                    goals_scored=EXCLUDED.goals_scored,
                    goals_conceded=EXCLUDED.goals_conceded,
                    clean_sheets=EXCLUDED.clean_sheets,
                    form_string=EXCLUDED.form_string,
                    played_home=EXCLUDED.played_home, wins_home=EXCLUDED.wins_home,
                    draws_home=EXCLUDED.draws_home, losses_home=EXCLUDED.losses_home,
                    goals_scored_home=EXCLUDED.goals_scored_home,
                    goals_conceded_home=EXCLUDED.goals_conceded_home,
                    clean_sheets_home=EXCLUDED.clean_sheets_home,
                    btts_home=EXCLUDED.btts_home, over25_home=EXCLUDED.over25_home,
                    played_away=EXCLUDED.played_away, wins_away=EXCLUDED.wins_away,
                    draws_away=EXCLUDED.draws_away, losses_away=EXCLUDED.losses_away,
                    goals_scored_away=EXCLUDED.goals_scored_away,
                    goals_conceded_away=EXCLUDED.goals_conceded_away,
                    clean_sheets_away=EXCLUDED.clean_sheets_away,
                    btts_away=EXCLUDED.btts_away, over25_away=EXCLUDED.over25_away,
                    recent_played=EXCLUDED.recent_played,
                    recent_wins=EXCLUDED.recent_wins,
                    recent_draws=EXCLUDED.recent_draws,
                    recent_losses=EXCLUDED.recent_losses,
                    recent_goals_scored=EXCLUDED.recent_goals_scored,
                    recent_goals_conceded=EXCLUDED.recent_goals_conceded,
                    updated_at=NOW()
            """, row)
    conn.commit()
    print(f"  Updated form for {len(form_rows)} teams")

    # ── Head to head ─────────────────────────────────────────────────────────
    h2h = defaultdict(lambda: {
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
