"""
Pre-compute bet scores for all upcoming fixtures using a Poisson probability model.
Run nightly after build_form_cache.py.

Model:
  Expected home goals (λ_h) = average of:
    - home team's goals scored per home game
    - away team's goals conceded per away game
  Expected away goals (λ_a) = average of:
    - away team's goals scored per away game
    - home team's goals conceded per home game

  Recent form (last 10) adjusts λ up/down by up to 15%.

  Poisson distribution gives P(home scores i, away scores j) for each scoreline.
  Summing over scorelines gives P(home win), P(draw), P(away win), P(BTTS), P(over 2.5).

  Scores (0-100) are probabilities × 100.
"""
import json
import math
import os
from datetime import datetime, timezone, timedelta

import psycopg2

DB = os.environ["DB_CONNECTION_STRING"]
BET_TYPES = ["BTTS", "OVER25", "WIN", "BTTS_WIN", "BTTS_NODRAW", "BTTS_OVER25"]
DAYS_AHEAD = 14
MAX_GOALS = 9  # sum scorelines up to 9-9


def get_conn():
    return psycopg2.connect(DB)


def _poisson(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _expected_goals(hf, af):
    """Return (lambda_home, lambda_away) using home/away split stats."""
    h_ph = hf["played_home"] or 1
    a_pa = af["played_away"] or 1

    # Home team: how many they score at home, how many the away team concedes away
    h_att = hf["goals_scored_home"] / h_ph
    a_def = af["goals_conceded_away"] / a_pa

    # Away team: how many they score away, how many the home team concedes at home
    a_att = af["goals_scored_away"] / a_pa
    h_def = hf["goals_conceded_home"] / h_ph

    # Expected goals = average of scorer's rate and opponent's conceding rate
    lam_h = (h_att + a_def) / 2
    lam_a = (a_att + h_def) / 2

    # Clamp to realistic range
    lam_h = max(0.3, min(4.0, lam_h))
    lam_a = max(0.3, min(4.0, lam_a))

    # Recent form modifier: if team is winning more/less than their long-run rate, adjust
    h_total = hf["played"] or 1
    a_total = af["played"] or 1

    if hf["recent_played"] >= 5:
        hist_wr = hf["wins"] / h_total
        rec_wr = hf["recent_wins"] / hf["recent_played"]
        modifier = 1.0 + (rec_wr - hist_wr) * 0.5
        lam_h *= max(0.85, min(1.15, modifier))

    if af["recent_played"] >= 5:
        hist_wr = af["wins"] / a_total
        rec_wr = af["recent_wins"] / af["recent_played"]
        modifier = 1.0 + (rec_wr - hist_wr) * 0.5
        lam_a *= max(0.85, min(1.15, modifier))

    return lam_h, lam_a


def _match_probs(lam_h, lam_a):
    """Compute outcome probabilities from expected goals using Poisson."""
    p_hw = p_draw = p_aw = p_btts = p_over25 = 0.0
    p_btts_hw = p_btts_aw = p_btts_nodraw = p_btts_over25 = 0.0

    for i in range(MAX_GOALS + 1):
        ph = _poisson(lam_h, i)
        for j in range(MAX_GOALS + 1):
            pa = _poisson(lam_a, j)
            p = ph * pa

            if i > j:
                p_hw += p
            elif i == j:
                p_draw += p
            else:
                p_aw += p

            if i > 0 and j > 0:
                p_btts += p
                if i > j:
                    p_btts_hw += p
                elif i < j:
                    p_btts_aw += p
                else:
                    pass  # btts draw — not counted in btts_nodraw

            if i + j > 2:
                p_over25 += p

            if i > 0 and j > 0 and i + j > 2:
                p_btts_over25 += p

    p_btts_nodraw = p_btts - (p_btts - p_btts_hw - p_btts_aw)
    # Simpler: btts_nodraw = btts_hw + btts_aw (excludes btts draws)
    p_btts_nodraw = p_btts_hw + p_btts_aw

    return {
        "p_hw": p_hw,
        "p_draw": p_draw,
        "p_aw": p_aw,
        "p_btts": p_btts,
        "p_over25": p_over25,
        "p_btts_hw": p_btts_hw,
        "p_btts_aw": p_btts_aw,
        "p_btts_nodraw": p_btts_nodraw,
        "p_btts_over25": p_btts_over25,
    }


def score_fixture(bet_type, hf, af):
    """Return (score 0-100, reasoning list, pick 'home'|'away'|None)."""
    h_ph = hf["played_home"] or 1
    a_pa = af["played_away"] or 1

    lam_h, lam_a = _expected_goals(hf, af)
    p = _match_probs(lam_h, lam_a)

    # Historical rates for blending
    hist_btts = (hf["btts_home"] / h_ph + af["btts_away"] / a_pa) / 2
    hist_over25 = (hf["over25_home"] / h_ph + af["over25_away"] / a_pa) / 2

    if bet_type == "BTTS":
        # Blend Poisson (70%) with historical rate (30%)
        prob = 0.7 * p["p_btts"] + 0.3 * hist_btts
        score = round(prob * 100)
        reasons = [
            f"Home scores {hf['goals_scored_home']/h_ph:.1f}/game at home, concedes {hf['goals_conceded_home']/h_ph:.1f}",
            f"Away scores {af['goals_scored_away']/a_pa:.1f}/game away, concedes {af['goals_conceded_away']/a_pa:.1f}",
            f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
            f"Historical BTTS rate: home {round(hf['btts_home']/h_ph*100)}%, away {round(af['btts_away']/a_pa*100)}%",
            f"Model probability: {round(prob*100)}%",
        ]
        return score, reasons, None

    elif bet_type == "OVER25":
        prob = 0.7 * p["p_over25"] + 0.3 * hist_over25
        score = round(prob * 100)
        reasons = [
            f"Expected goals: {lam_h:.2f} home + {lam_a:.2f} away = {lam_h+lam_a:.2f} total",
            f"Historical over 2.5 rate: home {round(hf['over25_home']/h_ph*100)}%, away {round(af['over25_away']/a_pa*100)}%",
            f"Model probability: {round(prob*100)}%",
        ]
        return score, reasons, None

    elif bet_type == "WIN":
        if p["p_hw"] >= p["p_aw"]:
            score = round(p["p_hw"] * 100)
            reasons = [
                f"Home win probability: {round(p['p_hw']*100)}%",
                f"Draw probability: {round(p['p_draw']*100)}%",
                f"Away win probability: {round(p['p_aw']*100)}%",
                f"Home record at home: {hf['wins_home']}W {hf['draws_home']}D {hf['losses_home']}L ({h_ph} games)",
                f"Away record away: {af['wins_away']}W {af['draws_away']}D {af['losses_away']}L ({a_pa} games)",
            ]
            return score, reasons, "home"
        else:
            score = round(p["p_aw"] * 100)
            reasons = [
                f"Away win probability: {round(p['p_aw']*100)}%",
                f"Draw probability: {round(p['p_draw']*100)}%",
                f"Home win probability: {round(p['p_hw']*100)}%",
                f"Away record away: {af['wins_away']}W {af['draws_away']}D {af['losses_away']}L ({a_pa} games)",
                f"Home record at home: {hf['wins_home']}W {hf['draws_home']}D {hf['losses_home']}L ({h_ph} games)",
            ]
            return score, reasons, "away"

    elif bet_type == "BTTS_WIN":
        if p["p_btts_hw"] >= p["p_btts_aw"]:
            prob = p["p_btts_hw"]
            pick = "home"
        else:
            prob = p["p_btts_aw"]
            pick = "away"
        score = round(prob * 100)
        reasons = [
            f"P(BTTS & home win): {round(p['p_btts_hw']*100)}%",
            f"P(BTTS & away win): {round(p['p_btts_aw']*100)}%",
            f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
        ]
        return score, reasons, pick

    elif bet_type == "BTTS_NODRAW":
        prob = p["p_btts_nodraw"]
        score = round(prob * 100)
        reasons = [
            f"P(BTTS & decisive result): {round(prob*100)}%",
            f"Draw probability: {round(p['p_draw']*100)}%",
            f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
        ]
        return score, reasons, None

    elif bet_type == "BTTS_OVER25":
        prob = p["p_btts_over25"]
        score = round(prob * 100)
        reasons = [
            f"P(BTTS & over 2.5 goals): {round(prob*100)}%",
            f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
            f"Historical BTTS rate: {round(hist_btts*100)}%, over 2.5 rate: {round(hist_over25*100)}%",
        ]
        return score, reasons, None

    return 0, [], None


def main():
    conn = get_conn()
    now = datetime.now(timezone.utc)
    date_from = now.date()
    date_to = date_from + timedelta(days=DAYS_AHEAD)
    print(f"Fixture scoring started at {now.isoformat()}")
    print(f"  Scoring fixtures from {date_from} to {date_to}")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.id, f.home_team_id, f.away_team_id,
                   ht.name AS home_team, at.name AS away_team,
                   l.name AS league_name, f.kickoff_time
            FROM fixtures f
            JOIN teams ht ON ht.id = f.home_team_id
            JOIN teams at ON at.id = f.away_team_id
            JOIN leagues l ON l.id = f.league_id
            LEFT JOIN results r ON r.fixture_id = f.id
            WHERE f.kickoff_time::date >= %s
              AND f.kickoff_time::date <= %s
              AND r.fixture_id IS NULL
            ORDER BY f.kickoff_time
        """, (date_from, date_to))
        fixtures = cur.fetchall()
        col_names = [desc[0] for desc in cur.description]

    print(f"  Found {len(fixtures)} upcoming fixtures")

    if not fixtures:
        conn.close()
        print("No fixtures to score.")
        return

    team_ids = list({row[col_names.index("home_team_id")] for row in fixtures} |
                    {row[col_names.index("away_team_id")] for row in fixtures})

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM team_form WHERE team_id = ANY(%s)", (team_ids,))
        rows = cur.fetchall()
        form_cols = [desc[0] for desc in cur.description]
        forms = {row[form_cols.index("team_id")]: dict(zip(form_cols, row)) for row in rows}

    scored = skipped = 0

    with conn.cursor() as cur:
        for fixture in fixtures:
            frow = dict(zip(col_names, fixture))
            home_id = frow["home_team_id"]
            away_id = frow["away_team_id"]
            hf = forms.get(home_id)
            af = forms.get(away_id)

            if not hf or not af:
                skipped += 1
                continue

            for bet_type in BET_TYPES:
                score, reasoning, pick = score_fixture(bet_type, hf, af)
                cur.execute("""
                    INSERT INTO fixture_scores
                        (fixture_id, bet_type, score, pick, reasoning, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (fixture_id, bet_type) DO UPDATE SET
                        score      = EXCLUDED.score,
                        pick       = EXCLUDED.pick,
                        reasoning  = EXCLUDED.reasoning,
                        updated_at = NOW()
                """, (frow["id"], bet_type, score, pick, json.dumps(reasoning)))
            scored += 1

    conn.commit()
    conn.close()
    print(f"  Scored {scored} fixtures ({skipped} skipped — no form data)")
    print("Fixture scoring complete")


if __name__ == "__main__":
    main()
