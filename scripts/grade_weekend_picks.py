"""
Grade last weekend's picks against actual results.
Runs Monday morning after results are in the database.

Grading logic per bet type:
  BTTS       — both teams scored
  OVER25     — total goals > 2
  WIN        — pick ('home'/'away') won
  BTTS_WIN   — BTTS AND pick won
  BTTS_NODRAW — BTTS AND not a draw
  BTTS_OVER25 — BTTS AND total goals > 2
"""
import os
from datetime import datetime, timezone, timedelta

import psycopg2

DB = os.environ["DB_CONNECTION_STRING"]


def get_conn():
    return psycopg2.connect(DB)


def grade(bet_type, pick, home_goals, away_goals):
    hg, ag = home_goals, away_goals
    btts = hg > 0 and ag > 0
    over25 = hg + ag > 2

    if bet_type == "BTTS":
        return btts
    elif bet_type == "OVER25":
        return over25
    elif bet_type == "WIN":
        if pick == "home":
            return hg > ag
        elif pick == "away":
            return ag > hg
        return False
    elif bet_type == "BTTS_WIN":
        if pick == "home":
            return btts and hg > ag
        elif pick == "away":
            return btts and ag > hg
        return False
    elif bet_type == "BTTS_NODRAW":
        return btts and hg != ag
    elif bet_type == "BTTS_OVER25":
        return btts and over25
    return False


def main():
    conn = get_conn()
    now = datetime.now(timezone.utc)
    today = now.date()

    # Grade the most recent ungraded weekend (picks from last Saturday)
    # Look back up to 7 days for ungraded picks
    cutoff = today - timedelta(days=7)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT wp.id, wp.week_start, wp.fixture_id, wp.bet_type, wp.pick,
                   wp.home_team, wp.away_team, wp.score,
                   r.home_goals, r.away_goals
            FROM weekend_picks wp
            JOIN results r ON r.fixture_id = wp.fixture_id
            WHERE wp.graded = false
              AND wp.week_start >= %s
              AND r.home_goals IS NOT NULL
        """, (cutoff,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    if not rows:
        print("No ungraded picks with results found.")
        conn.close()
        return

    print(f"Grading {len(rows)} picks...")

    results_by_bet = {}
    with conn.cursor() as cur:
        for row in rows:
            r = dict(zip(cols, row))
            correct = grade(r["bet_type"], r["pick"], r["home_goals"], r["away_goals"])
            cur.execute("""
                UPDATE weekend_picks SET
                    graded = true,
                    correct = %s,
                    home_goals = %s,
                    away_goals = %s
                WHERE id = %s
            """, (correct, r["home_goals"], r["away_goals"], r["id"]))

            bt = r["bet_type"]
            if bt not in results_by_bet:
                results_by_bet[bt] = {"correct": 0, "total": 0, "picks": []}
            results_by_bet[bt]["total"] += 1
            if correct:
                results_by_bet[bt]["correct"] += 1
            results_by_bet[bt]["picks"].append({
                "match": f"{r['home_team']} vs {r['away_team']}",
                "score": r["score"],
                "correct": correct,
                "result": f"{r['home_goals']}-{r['away_goals']}",
            })

    conn.commit()

    # Print accuracy report
    print(f"\n=== Weekend Picks Accuracy Report ({today}) ===\n")
    overall_correct = overall_total = 0
    for bt, data in sorted(results_by_bet.items()):
        pct = round(data["correct"] / data["total"] * 100) if data["total"] else 0
        print(f"{bt}: {data['correct']}/{data['total']} correct ({pct}%)")
        overall_correct += data["correct"]
        overall_total += data["total"]
        # Show wrong picks for analysis
        wrong = [p for p in data["picks"] if not p["correct"]]
        for w in wrong[:3]:
            print(f"  MISS  {w['match']} — result {w['result']} (model score {w['score']})")

    if overall_total:
        overall_pct = round(overall_correct / overall_total * 100)
        print(f"\nOverall: {overall_correct}/{overall_total} ({overall_pct}%)")

    conn.close()
    print("\nGrading complete")


if __name__ == "__main__":
    main()
