"""
Pre-compute bet scores for all upcoming fixtures and store in fixture_scores table.
Run nightly after build_form_cache.py so form data is fresh.
"""
import os
import json
from datetime import datetime, timezone, timedelta

import psycopg2

DB = os.environ["DB_CONNECTION_STRING"]

BET_TYPES = ["BTTS", "OVER25", "WIN", "BTTS_WIN", "BTTS_NODRAW", "BTTS_OVER25"]

# Look-ahead: score fixtures up to this many days in advance
DAYS_AHEAD = 14


def get_conn():
    return psycopg2.connect(DB)


def _rate(num, den):
    return num / den if den > 0 else 0.0


def score_fixture(bet_type, hf, af):
    """Return (score 0-100, reasoning list, pick 'home'|'away'|None)."""
    hp = hf["played"] or 1
    ap = af["played"] or 1

    h_win  = _rate(hf["wins"],          hp)
    h_draw = _rate(hf["draws"],         hp)
    h_loss = _rate(hf["losses"],        hp)
    h_att  = _rate(hf["goals_scored"],  hp)
    h_def  = _rate(hf["goals_conceded"],hp)
    h_cs   = _rate(hf["clean_sheets"],  hp)

    a_win  = _rate(af["wins"],          ap)
    a_draw = _rate(af["draws"],         ap)
    a_loss = _rate(af["losses"],        ap)
    a_att  = _rate(af["goals_scored"],  ap)
    a_def  = _rate(af["goals_conceded"],ap)
    a_cs   = _rate(af["clean_sheets"],  ap)

    if bet_type == "BTTS":
        raw = (a_att * (1 - h_cs) + h_att * (1 - a_cs)) / 2
        score = min(100, round(raw * 50))
        reasons = [
            f"Home scores {h_att:.1f} goals/game, concedes {h_def:.1f}",
            f"Away scores {a_att:.1f} goals/game, concedes {a_def:.1f}",
        ]
        if h_cs >= 0.6: reasons.append(f"Caution: home kept {hf['clean_sheets']} clean sheets in last {hp}")
        if a_cs >= 0.6: reasons.append(f"Caution: away kept {af['clean_sheets']} clean sheets in last {ap}")
        return score, reasons, None

    elif bet_type == "OVER25":
        avg = ((h_att + h_def) + (a_att + a_def)) / 2
        score = max(0, min(100, round((avg - 1.5) / 2.5 * 100)))
        reasons = [
            f"Home avg {h_att + h_def:.1f} goals/game (scored + conceded)",
            f"Away avg {a_att + a_def:.1f} goals/game (scored + conceded)",
            f"Combined average: {avg:.1f} goals/game",
        ]
        return score, reasons, None

    elif bet_type == "WIN":
        h_strength = (h_win + a_loss) / 2
        a_strength = (a_win + h_loss) / 2
        if h_strength >= a_strength:
            score = min(100, round(h_strength * 100))
            reasons = [f"Home: {hf['wins']}W {hf['draws']}D {hf['losses']}L in last {hp}",
                       f"Away: {af['wins']}W {af['draws']}D {af['losses']}L in last {ap}"]
            return score, reasons, "home"
        else:
            score = min(100, round(a_strength * 100))
            reasons = [f"Away: {af['wins']}W {af['draws']}D {af['losses']}L in last {ap}",
                       f"Home: {hf['wins']}W {hf['draws']}D {hf['losses']}L in last {hp}"]
            return score, reasons, "away"

    elif bet_type == "BTTS_WIN":
        btts = (a_att * (1 - h_cs) + h_att * (1 - a_cs)) / 2
        h_strength = (h_win + a_loss) / 2
        a_strength = (a_win + h_loss) / 2
        pick = "home" if h_strength >= a_strength else "away"
        win_s = h_strength if pick == "home" else a_strength
        score = min(100, round((btts + win_s) / 2 * 80))
        reasons = [
            f"BTTS: home scores {h_att:.1f}/game, away scores {a_att:.1f}/game",
            f"Likely winner: {pick} ({hf['wins']}W vs {af['wins']}W in last 5)",
        ]
        return score, reasons, pick

    elif bet_type == "BTTS_NODRAW":
        btts = (a_att * (1 - h_cs) + h_att * (1 - a_cs)) / 2
        no_draw = 1 - (h_draw + a_draw) / 2
        score = min(100, round(btts * no_draw * 80))
        reasons = [
            f"Home scores {h_att:.1f}/game, draw rate {round(h_draw*100)}%",
            f"Away scores {a_att:.1f}/game, draw rate {round(a_draw*100)}%",
        ]
        return score, reasons, None

    elif bet_type == "BTTS_OVER25":
        btts = (a_att * (1 - h_cs) + h_att * (1 - a_cs)) / 2
        avg = ((h_att + h_def) + (a_att + a_def)) / 2
        over25 = max(0.0, min(1.0, (avg - 1.5) / 2.5))
        score = min(100, round((btts + over25) / 2 * 80))
        reasons = [
            f"Home scores {h_att:.1f}/game, away scores {a_att:.1f}/game",
            f"Combined avg {avg:.1f} goals/game",
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

    # Load upcoming fixtures (no result yet)
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

    print(f"  Found {len(fixtures)} upcoming fixtures")

    if not fixtures:
        conn.close()
        print("No fixtures to score.")
        return

    # Load form for all relevant teams
    team_ids = list({row[1] for row in fixtures} | {row[2] for row in fixtures})
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM team_form WHERE team_id = ANY(%s)", (team_ids,))
        rows = cur.fetchall()
        col_names = [desc[0] for desc in cur.description]
        forms = {row[col_names.index("team_id")]: dict(zip(col_names, row)) for row in rows}

    scored = 0
    skipped = 0

    with conn.cursor() as cur:
        for fixture in fixtures:
            fid, home_id, away_id, home_team, away_team, league_name, kickoff = fixture
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
                """, (fid, bet_type, score, pick, json.dumps(reasoning)))
            scored += 1

    conn.commit()
    conn.close()
    print(f"  Scored {scored} fixtures ({skipped} skipped — no form data)")
    print("Fixture scoring complete")


if __name__ == "__main__":
    main()
