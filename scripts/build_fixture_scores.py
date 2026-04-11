"""
Pre-compute bet scores for all upcoming fixtures using Dixon-Coles ratings.

Expected goals:
  λ_home = α_home × β_away × γ
  λ_away = α_away × β_home

Scoreline probabilities use Dixon-Coles low-score correction (ρ).
All bet type probabilities are derived by summing over scorelines.
Scores (0-100) = probability × 100.
"""
import json
import math
import os
from datetime import datetime, timezone, timedelta

import psycopg2

DB = os.environ["DB_CONNECTION_STRING"]
BET_TYPES = ["BTTS", "OVER25", "WIN", "BTTS_WIN", "BTTS_NODRAW", "BTTS_OVER25"]
DAYS_AHEAD = 14
MAX_GOALS = 9


def get_conn():
    return psycopg2.connect(DB)


def _poisson(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _dc_correction(lam_h, lam_a, i, j, rho):
    if i == 0 and j == 0:
        return 1 - lam_h * lam_a * rho
    elif i == 1 and j == 0:
        return 1 + lam_a * rho
    elif i == 0 and j == 1:
        return 1 + lam_h * rho
    elif i == 1 and j == 1:
        return 1 - rho
    return 1.0


def _match_probs(lam_h, lam_a, rho):
    """Compute all outcome probabilities from Dixon-Coles model."""
    p_hw = p_draw = p_aw = 0.0
    p_btts = p_over25 = 0.0
    p_btts_hw = p_btts_aw = p_btts_over25 = 0.0

    for i in range(MAX_GOALS + 1):
        ph = _poisson(lam_h, i)
        for j in range(MAX_GOALS + 1):
            pa = _poisson(lam_a, j)
            tau = _dc_correction(lam_h, lam_a, i, j, rho)
            p = max(0.0, ph * pa * tau)

            if i > j:    p_hw += p
            elif i == j: p_draw += p
            else:        p_aw += p

            if i > 0 and j > 0:
                p_btts += p
                if i > j:  p_btts_hw += p
                elif i < j: p_btts_aw += p

            if i + j > 2:
                p_over25 += p

            if i > 0 and j > 0 and i + j > 2:
                p_btts_over25 += p

    return {
        "p_hw": p_hw, "p_draw": p_draw, "p_aw": p_aw,
        "p_btts": p_btts, "p_over25": p_over25,
        "p_btts_hw": p_btts_hw, "p_btts_aw": p_btts_aw,
        "p_btts_nodraw": p_btts_hw + p_btts_aw,
        "p_btts_over25": p_btts_over25,
    }


def score_fixture(bet_type, lam_h, lam_a, rho, hf, af, home_rating, away_rating):
    """Return (score 0-100, reasoning list, pick 'home'|'away'|None)."""
    p = _match_probs(lam_h, lam_a, rho)
    h_ph = hf["played_home"] or 1
    a_pa = hf["played_away"] or 1
    hist_btts = (hf["btts_home"] / h_ph + af["btts_away"] / (af["played_away"] or 1)) / 2
    hist_over25 = (hf["over25_home"] / h_ph + af["over25_away"] / (af["played_away"] or 1)) / 2

    # Blend model (80%) with historical rate (20%) for BTTS/Over
    if bet_type == "BTTS":
        prob = 0.8 * p["p_btts"] + 0.2 * hist_btts
        return round(prob * 100), [
            f"Expected goals: {lam_h:.2f} (home) – {lam_a:.2f} (away)",
            f"Home attack rating: {home_rating['attack']:.2f}, Away defence rating: {away_rating['defence']:.2f}",
            f"Away attack rating: {away_rating['attack']:.2f}, Home defence rating: {home_rating['defence']:.2f}",
            f"Historical BTTS: home {round(hf['btts_home']/h_ph*100)}% at home, away {round(af['btts_away']/(af['played_away'] or 1)*100)}% away",
            f"Model probability: {round(prob*100)}%",
        ], None

    elif bet_type == "OVER25":
        prob = 0.8 * p["p_over25"] + 0.2 * hist_over25
        return round(prob * 100), [
            f"Expected goals: {lam_h:.2f} + {lam_a:.2f} = {lam_h+lam_a:.2f} total",
            f"Historical over 2.5: home {round(hf['over25_home']/h_ph*100)}% at home, away {round(af['over25_away']/(af['played_away'] or 1)*100)}% away",
            f"Model probability: {round(prob*100)}%",
        ], None

    elif bet_type == "WIN":
        if p["p_hw"] >= p["p_aw"]:
            return round(p["p_hw"] * 100), [
                f"Home win: {round(p['p_hw']*100)}% | Draw: {round(p['p_draw']*100)}% | Away win: {round(p['p_aw']*100)}%",
                f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
                f"Home attack {home_rating['attack']:.2f} vs Away defence {away_rating['defence']:.2f}",
            ], "home"
        else:
            return round(p["p_aw"] * 100), [
                f"Away win: {round(p['p_aw']*100)}% | Draw: {round(p['p_draw']*100)}% | Home win: {round(p['p_hw']*100)}%",
                f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
                f"Away attack {away_rating['attack']:.2f} vs Home defence {home_rating['defence']:.2f}",
            ], "away"

    elif bet_type == "BTTS_WIN":
        if p["p_btts_hw"] >= p["p_btts_aw"]:
            return round(p["p_btts_hw"] * 100), [
                f"P(BTTS & home win): {round(p['p_btts_hw']*100)}%",
                f"P(BTTS & away win): {round(p['p_btts_aw']*100)}%",
                f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
            ], "home"
        else:
            return round(p["p_btts_aw"] * 100), [
                f"P(BTTS & away win): {round(p['p_btts_aw']*100)}%",
                f"P(BTTS & home win): {round(p['p_btts_hw']*100)}%",
                f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
            ], "away"

    elif bet_type == "BTTS_NODRAW":
        prob = p["p_btts_nodraw"]
        return round(prob * 100), [
            f"P(BTTS & decisive result): {round(prob*100)}%",
            f"Draw probability: {round(p['p_draw']*100)}%",
            f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
        ], None

    elif bet_type == "BTTS_OVER25":
        prob = p["p_btts_over25"]
        return round(prob * 100), [
            f"P(BTTS & over 2.5 goals): {round(prob*100)}%",
            f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
            f"Historical BTTS: {round(hist_btts*100)}%, over 2.5: {round(hist_over25*100)}%",
        ], None

    return 0, [], None


def main():
    conn = get_conn()
    now = datetime.now(timezone.utc)
    date_from = now.date()
    date_to = date_from + timedelta(days=DAYS_AHEAD)
    print(f"Fixture scoring started at {now.isoformat()}")

    # Load model params
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM model_params")
        params = {r[0]: r[1] for r in cur.fetchall()}

    gamma = params.get("gamma", 1.3)
    rho = params.get("rho", -0.1)
    print(f"  Using γ={gamma:.3f}, ρ={rho:.4f}")

    # Load upcoming fixtures
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.id, f.home_team_id, f.away_team_id,
                   ht.name, at.name, l.name, f.kickoff_time
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

    print(f"  Found {len(fixtures)} upcoming fixtures")
    if not fixtures:
        conn.close()
        return

    team_ids = list({f[1] for f in fixtures} | {f[2] for f in fixtures})

    # Load ratings
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, attack, defence FROM team_ratings WHERE team_id = ANY(%s)", (team_ids,))
        ratings = {r[0]: {"attack": r[1], "defence": r[2]} for r in cur.fetchall()}

    # Load form stats
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM team_form WHERE team_id = ANY(%s)", (team_ids,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        forms = {r[cols.index("team_id")]: dict(zip(cols, r)) for r in rows}

    default_rating = {"attack": 1.0, "defence": 1.0}
    scored = skipped = 0

    with conn.cursor() as cur:
        for fid, home_id, away_id, home_team, away_team, league, kickoff in fixtures:
            hr = ratings.get(home_id, default_rating)
            ar = ratings.get(away_id, default_rating)
            hf = forms.get(home_id)
            af = forms.get(away_id)

            if not hf or not af:
                skipped += 1
                continue

            # Dixon-Coles expected goals
            lam_h = hr["attack"] * ar["defence"] * gamma
            lam_a = ar["attack"] * hr["defence"]

            # Clamp to realistic range
            lam_h = max(0.3, min(4.5, lam_h))
            lam_a = max(0.3, min(4.5, lam_a))

            for bet_type in BET_TYPES:
                score, reasoning, pick = score_fixture(
                    bet_type, lam_h, lam_a, rho, hf, af, hr, ar
                )
                cur.execute("""
                    INSERT INTO fixture_scores
                        (fixture_id, bet_type, score, pick, reasoning, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (fixture_id, bet_type) DO UPDATE SET
                        score = EXCLUDED.score,
                        pick = EXCLUDED.pick,
                        reasoning = EXCLUDED.reasoning,
                        updated_at = NOW()
                """, (fid, bet_type, score, pick, json.dumps(reasoning)))
            scored += 1

    conn.commit()
    conn.close()
    print(f"  Scored {scored} fixtures ({skipped} skipped — no form data)")
    print("Fixture scoring complete")


if __name__ == "__main__":
    main()
