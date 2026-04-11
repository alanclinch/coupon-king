import math
import os
from datetime import date, timedelta
from typing import List, Optional, Tuple

import psycopg2
import psycopg2.extras
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_URL = os.environ["DB_CONNECTION_STRING"]

app = FastAPI(title="Coupon King API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_db():
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


_FIXTURE_COLS = """
    f.id,
    f.kickoff_time,
    f.status,
    f.matchday,
    f.season,
    f.league_id,
    l.name  AS league_name,
    f.home_team_id,
    ht.name AS home_team,
    f.away_team_id,
    at.name AS away_team,
    r.home_goals,
    r.away_goals
"""

_FIXTURE_JOINS = """
    FROM fixtures f
    JOIN leagues l  ON l.id  = f.league_id
    JOIN teams   ht ON ht.id = f.home_team_id
    JOIN teams   at ON at.id = f.away_team_id
    LEFT JOIN results r ON r.fixture_id = f.id
"""


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok"}


# ── Leagues ───────────────────────────────────────────────────────────────────

@app.get("/leagues")
def get_leagues(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM leagues ORDER BY name")
        return cur.fetchall()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@app.get("/fixtures/saturday3pm")
def get_saturday_3pm(conn=Depends(get_db)):
    """Return upcoming fixtures on the next Saturday between 13:00–15:00 UTC
    (covers both GMT 15:00 and BST 14:00 kick-offs)."""
    today = date.today()
    days_until_sat = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_sat)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_FIXTURE_COLS}
            {_FIXTURE_JOINS}
            WHERE f.kickoff_time::date = %s
              AND EXTRACT(hour FROM f.kickoff_time AT TIME ZONE 'UTC') IN (13, 14, 15)
              AND r.fixture_id IS NULL
            ORDER BY f.kickoff_time, ht.name
            """,
            (saturday,),
        )
        return cur.fetchall()


@app.get("/fixtures")
def get_fixtures(
    league_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    conn=Depends(get_db),
):
    today = date.today()
    df = date_from or today.isoformat()
    dt = date_to or (today + timedelta(days=7)).isoformat()

    where = "WHERE f.kickoff_time::date >= %s AND f.kickoff_time::date <= %s"
    params: list = [df, dt]

    if league_id:
        where += " AND f.league_id = %s"
        params.append(league_id)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_FIXTURE_COLS} {_FIXTURE_JOINS} {where} ORDER BY f.kickoff_time, ht.name",
            params,
        )
        return cur.fetchall()


# ── Team form ─────────────────────────────────────────────────────────────────

@app.get("/teams/{team_id}/form")
def get_team_form(team_id: int, conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tf.*, t.name AS team_name
            FROM team_form tf
            JOIN teams t ON t.id = tf.team_id
            WHERE tf.team_id = %s
            """,
            (team_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Form data not found for this team")
    return row


# ── Head-to-head ──────────────────────────────────────────────────────────────

@app.get("/fixtures/{fixture_id}/h2h")
def get_h2h(fixture_id: int, conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT home_team_id, away_team_id FROM fixtures WHERE id = %s",
            (fixture_id,),
        )
        fixture = cur.fetchone()
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    home_id = fixture["home_team_id"]
    away_id = fixture["away_team_id"]
    t1, t2 = min(home_id, away_id), max(home_id, away_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.*, t1.name AS team1_name, t2.name AS team2_name
            FROM head_to_head h
            JOIN teams t1 ON t1.id = h.team1_id
            JOIN teams t2 ON t2.id = h.team2_id
            WHERE h.team1_id = %s AND h.team2_id = %s
            """,
            (t1, t2),
        )
        h2h = cur.fetchone()

    if not h2h:
        return {"home_team_id": home_id, "away_team_id": away_id, "meetings": 0}

    result = dict(h2h)
    # Re-orient to home/away perspective for the caller
    if home_id == t1:
        result["home_team"]  = h2h["team1_name"]
        result["away_team"]  = h2h["team2_name"]
        result["home_wins"]  = h2h["team1_wins"]
        result["away_wins"]  = h2h["team2_wins"]
        result["home_goals"] = h2h["team1_goals"]
        result["away_goals"] = h2h["team2_goals"]
    else:
        result["home_team"]  = h2h["team2_name"]
        result["away_team"]  = h2h["team1_name"]
        result["home_wins"]  = h2h["team2_wins"]
        result["away_wins"]  = h2h["team1_wins"]
        result["home_goals"] = h2h["team2_goals"]
        result["away_goals"] = h2h["team1_goals"]
    return result


# ── Fixture scores (all bet types) ────────────────────────────────────────────

@app.get("/fixtures/{fixture_id}/scores")
def get_fixture_scores(fixture_id: int, conn=Depends(get_db)):
    import json as _json
    # Try pre-computed scores first
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bet_type, score, pick, reasoning FROM fixture_scores WHERE fixture_id = %s",
                (fixture_id,),
            )
            rows = cur.fetchall()
        if rows:
            out = {}
            for r in rows:
                reasoning = r["reasoning"]
                if isinstance(reasoning, str):
                    reasoning = _json.loads(reasoning)
                out[r["bet_type"]] = {"score": r["score"], "pick": r["pick"], "reasoning": reasoning}
            return out
    except Exception:
        conn.rollback()

    # Fall back: compute live
    with conn.cursor() as cur:
        cur.execute("SELECT home_team_id, away_team_id FROM fixtures WHERE id = %s", (fixture_id,))
        fixture = cur.fetchone()
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")

    home_id = fixture["home_team_id"]
    away_id = fixture["away_team_id"]

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM team_form WHERE team_id IN (%s, %s)", (home_id, away_id))
        forms = {r["team_id"]: r for r in cur.fetchall()}
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, attack, defence FROM team_ratings WHERE team_id IN (%s, %s)", (home_id, away_id))
        ratings = {r["team_id"]: r for r in cur.fetchall()}
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM model_params")
        mparams = {r["key"]: r["value"] for r in cur.fetchall()}

    hf = forms.get(home_id)
    af = forms.get(away_id)
    if not hf or not af:
        return {}

    gamma = mparams.get("gamma", 1.3)
    rho = mparams.get("rho", -0.1)

    out = {}
    for bt in ["BTTS", "OVER25", "WIN", "BTTS_WIN", "BTTS_NODRAW", "BTTS_OVER25"]:
        score, reasoning, pick = _score_fixture(
            bt, dict(hf), dict(af),
            ratings.get(home_id), ratings.get(away_id),
            gamma, rho,
        )
        out[bt] = {"score": score, "pick": pick, "reasoning": reasoning}
    return out


# ── Poisson probability model ─────────────────────────────────────────────────

_MAX_GOALS = 9


def _poisson(lam: float, k: int) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _expected_goals(hf: dict, af: dict,
                    home_rating: Optional[dict] = None,
                    away_rating: Optional[dict] = None,
                    gamma: float = 1.3) -> Tuple[float, float]:
    if home_rating and away_rating:
        # Dixon-Coles: α_home × β_away × γ
        lam_h = home_rating["attack"] * away_rating["defence"] * gamma
        lam_a = away_rating["attack"] * home_rating["defence"]
    else:
        # Fallback: raw goal averages from home/away split
        h_ph = hf["played_home"] or 1
        a_pa = af["played_away"] or 1
        h_att = hf["goals_scored_home"] / h_ph
        a_def = af["goals_conceded_away"] / a_pa
        a_att = af["goals_scored_away"] / a_pa
        h_def = hf["goals_conceded_home"] / h_ph
        lam_h = (h_att + a_def) / 2
        lam_a = (a_att + h_def) / 2

    lam_h = max(0.3, min(4.5, lam_h))
    lam_a = max(0.3, min(4.5, lam_a))
    return lam_h, lam_a


def _dc_correction(lam_h: float, lam_a: float, i: int, j: int, rho: float) -> float:
    if i == 0 and j == 0:   return 1 - lam_h * lam_a * rho
    elif i == 1 and j == 0: return 1 + lam_a * rho
    elif i == 0 and j == 1: return 1 + lam_h * rho
    elif i == 1 and j == 1: return 1 - rho
    return 1.0


def _match_probs(lam_h: float, lam_a: float, rho: float = -0.1) -> dict:
    p_hw = p_draw = p_aw = p_btts = p_over25 = 0.0
    p_btts_hw = p_btts_aw = p_btts_over25 = 0.0

    for i in range(_MAX_GOALS + 1):
        ph = _poisson(lam_h, i)
        for j in range(_MAX_GOALS + 1):
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
            if i + j > 2: p_over25 += p
            if i > 0 and j > 0 and i + j > 2: p_btts_over25 += p

    return {
        "p_hw": p_hw, "p_draw": p_draw, "p_aw": p_aw,
        "p_btts": p_btts, "p_over25": p_over25,
        "p_btts_hw": p_btts_hw, "p_btts_aw": p_btts_aw,
        "p_btts_nodraw": p_btts_hw + p_btts_aw,
        "p_btts_over25": p_btts_over25,
    }


def _score_fixture(bet_type: str, hf: dict, af: dict,
                   home_rating: Optional[dict] = None,
                   away_rating: Optional[dict] = None,
                   gamma: float = 1.3,
                   rho: float = -0.1) -> Tuple[int, list, Optional[str]]:
    """Return (score 0-100, reasoning lines, pick 'home'|'away'|None)."""
    h_ph = hf["played_home"] or 1
    a_pa = af["played_away"] or 1
    hist_btts = (hf["btts_home"] / h_ph + af["btts_away"] / (af["played_away"] or 1)) / 2
    hist_over25 = (hf["over25_home"] / h_ph + af["over25_away"] / (af["played_away"] or 1)) / 2

    lam_h, lam_a = _expected_goals(hf, af, home_rating, away_rating, gamma)
    p = _match_probs(lam_h, lam_a, rho)

    if bet_type == "BTTS":
        prob = 0.7 * p["p_btts"] + 0.3 * hist_btts
        return round(prob * 100), [
            f"Home scores {hf['goals_scored_home']/h_ph:.1f}/game at home, concedes {hf['goals_conceded_home']/h_ph:.1f}",
            f"Away scores {af['goals_scored_away']/a_pa:.1f}/game away, concedes {af['goals_conceded_away']/a_pa:.1f}",
            f"Expected goals: {lam_h:.2f} – {lam_a:.2f}",
            f"Historical BTTS: home {round(hf['btts_home']/h_ph*100)}%, away {round(af['btts_away']/a_pa*100)}%",
            f"Model probability: {round(prob*100)}%",
        ], None

    elif bet_type == "OVER25":
        prob = 0.7 * p["p_over25"] + 0.3 * hist_over25
        return round(prob * 100), [
            f"Expected goals: {lam_h:.2f} + {lam_a:.2f} = {lam_h+lam_a:.2f}",
            f"Historical over 2.5: home {round(hf['over25_home']/h_ph*100)}%, away {round(af['over25_away']/a_pa*100)}%",
            f"Model probability: {round(prob*100)}%",
        ], None

    elif bet_type == "WIN":
        if p["p_hw"] >= p["p_aw"]:
            return round(p["p_hw"] * 100), [
                f"Home win: {round(p['p_hw']*100)}% | Draw: {round(p['p_draw']*100)}% | Away win: {round(p['p_aw']*100)}%",
                f"Home at home: {hf['wins_home']}W {hf['draws_home']}D {hf['losses_home']}L ({h_ph} games)",
                f"Away away: {af['wins_away']}W {af['draws_away']}D {af['losses_away']}L ({a_pa} games)",
            ], "home"
        else:
            return round(p["p_aw"] * 100), [
                f"Away win: {round(p['p_aw']*100)}% | Draw: {round(p['p_draw']*100)}% | Home win: {round(p['p_hw']*100)}%",
                f"Away away: {af['wins_away']}W {af['draws_away']}D {af['losses_away']}L ({a_pa} games)",
                f"Home at home: {hf['wins_home']}W {hf['draws_home']}D {hf['losses_home']}L ({h_ph} games)",
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


# ── Coupon check ──────────────────────────────────────────────────────────────

class CouponSelection(BaseModel):
    fixture_id: int
    market: str  # "1" | "X" | "2" | "BTTS_YES" | "BTTS_NO" | "OVER25" | "UNDER25"


@app.post("/coupon/check")
def check_coupon(selections: List[CouponSelection], conn=Depends(get_db)):
    output = []

    for sel in selections:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.home_team_id, f.away_team_id,
                       ht.name AS home_team, at.name AS away_team
                FROM fixtures f
                JOIN teams ht ON ht.id = f.home_team_id
                JOIN teams at ON at.id = f.away_team_id
                WHERE f.id = %s
                """,
                (sel.fixture_id,),
            )
            fixture = cur.fetchone()
        if not fixture:
            continue

        home_id = fixture["home_team_id"]
        away_id = fixture["away_team_id"]

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM team_form WHERE team_id IN (%s, %s)", (home_id, away_id))
            forms = {r["team_id"]: r for r in cur.fetchall()}
        with conn.cursor() as cur:
            cur.execute("SELECT team_id, attack, defence FROM team_ratings WHERE team_id IN (%s, %s)", (home_id, away_id))
            ratings = {r["team_id"]: r for r in cur.fetchall()}
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM model_params")
            mparams = {r["key"]: r["value"] for r in cur.fetchall()}

        hf = forms.get(home_id)
        af = forms.get(away_id)
        market = sel.market.upper()
        flags = []

        if hf and af:
            gamma = mparams.get("gamma", 1.3)
            rho = mparams.get("rho", -0.1)
            lam_h, lam_a = _expected_goals(dict(hf), dict(af), ratings.get(home_id), ratings.get(away_id), gamma)
            p = _match_probs(lam_h, lam_a, rho)
            h_ph = hf["played_home"] or 1
            a_pa = af["played_away"] or 1
            hist_btts = (hf["btts_home"] / h_ph + af["btts_away"] / a_pa) / 2
            hist_over25 = (hf["over25_home"] / h_ph + af["over25_away"] / a_pa) / 2
            p_btts = 0.7 * p["p_btts"] + 0.3 * hist_btts
            p_over25 = 0.7 * p["p_over25"] + 0.3 * hist_over25

            if market == "1":
                if p["p_hw"] < 0.40:
                    flags.append(f"Home win probability only {round(p['p_hw']*100)}% — not a strong favourite")
                if p["p_aw"] > 0.40:
                    flags.append(f"Away win probability {round(p['p_aw']*100)}% — {fixture['away_team']} are a real threat")
                if hf["recent_played"] >= 5 and hf["recent_wins"] / hf["recent_played"] < 0.25:
                    flags.append(f"{fixture['home_team']} poor recent form: {hf['recent_wins']}W in last {hf['recent_played']}")

            elif market == "2":
                if p["p_aw"] < 0.33:
                    flags.append(f"Away win probability only {round(p['p_aw']*100)}% — difficult ask")
                if p["p_hw"] > 0.45:
                    flags.append(f"Home win probability {round(p['p_hw']*100)}% — {fixture['home_team']} are strong favourites")
                if af["recent_played"] >= 5 and af["recent_wins"] / af["recent_played"] < 0.20:
                    flags.append(f"{fixture['away_team']} poor recent form: {af['recent_wins']}W in last {af['recent_played']}")

            elif market == "X":
                if p["p_draw"] < 0.22:
                    flags.append(f"Draw probability only {round(p['p_draw']*100)}% — sides are mismatched")
                if abs(p["p_hw"] - p["p_aw"]) > 0.30:
                    stronger = fixture["home_team"] if p["p_hw"] > p["p_aw"] else fixture["away_team"]
                    flags.append(f"{stronger} heavily favoured — draw unlikely")

            elif market == "BTTS_YES":
                if p_btts < 0.45:
                    flags.append(f"BTTS probability only {round(p_btts*100)}% — one side may be shut out")
                if hf["played_home"] >= 5 and hf["clean_sheets_home"] / h_ph >= 0.40:
                    flags.append(f"{fixture['home_team']} keep clean sheets in {round(hf['clean_sheets_home']/h_ph*100)}% of home games")
                if af["played_away"] >= 5 and af["clean_sheets_away"] / a_pa >= 0.40:
                    flags.append(f"{fixture['away_team']} keep clean sheets in {round(af['clean_sheets_away']/a_pa*100)}% of away games")

            elif market == "BTTS_NO":
                if p_btts > 0.60:
                    flags.append(f"BTTS probability {round(p_btts*100)}% — both teams likely to score")
                if hf["played_home"] >= 5 and hf["goals_scored_home"] / h_ph >= 1.8:
                    flags.append(f"{fixture['home_team']} score {hf['goals_scored_home']/h_ph:.1f} goals/home game")
                if af["played_away"] >= 5 and af["goals_scored_away"] / a_pa >= 1.4:
                    flags.append(f"{fixture['away_team']} score {af['goals_scored_away']/a_pa:.1f} goals/away game")

            elif market == "OVER25":
                if p_over25 < 0.45:
                    flags.append(f"Over 2.5 probability only {round(p_over25*100)}% — low-scoring fixture expected")
                if lam_h + lam_a < 2.2:
                    flags.append(f"Expected goals only {lam_h+lam_a:.1f} — under 2.5 more likely")

            elif market == "UNDER25":
                if p_over25 > 0.60:
                    flags.append(f"Over 2.5 probability {round(p_over25*100)}% — high-scoring fixture expected")
                if lam_h + lam_a > 3.0:
                    flags.append(f"Expected goals {lam_h+lam_a:.1f} — over 2.5 more likely")

        risk = "high" if len(flags) >= 2 else "medium" if len(flags) == 1 else "low"
        output.append({
            "fixture_id": sel.fixture_id,
            "market":     sel.market,
            "home_team":  fixture["home_team"],
            "away_team":  fixture["away_team"],
            "home_form":  hf["form_string"] if hf else None,
            "away_form":  af["form_string"] if af else None,
            "flags":      flags,
            "risk":       risk,
        })

    return output


@app.get("/picks")
def get_picks(
    bet_type: str = "BTTS",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    saturday3pm: bool = False,
    conn=Depends(get_db),
):
    today = date.today()
    bt = bet_type.upper()

    if saturday3pm:
        days_until_sat = (5 - today.weekday()) % 7
        saturday = today + timedelta(days=days_until_sat)
        where  = """WHERE f.kickoff_time::date = %s
          AND EXTRACT(hour FROM f.kickoff_time AT TIME ZONE 'UTC') IN (13, 14, 15)
          AND r.fixture_id IS NULL"""
        params: list = [saturday]
    else:
        df = date_from or today.isoformat()
        dt = date_to or (today + timedelta(days=7)).isoformat()
        where  = "WHERE f.kickoff_time::date >= %s AND f.kickoff_time::date <= %s AND r.fixture_id IS NULL"
        params = [df, dt]

    # Try pre-computed scores first (populated nightly by build_fixture_scores.py)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_FIXTURE_COLS},
                       fs.score, fs.pick, fs.reasoning
                {_FIXTURE_JOINS}
                JOIN fixture_scores fs ON fs.fixture_id = f.id AND fs.bet_type = %s
                {where}
                ORDER BY fs.score DESC
                """,
                [bt] + params,
            )
            rows = cur.fetchall()

        if rows:
            import json as _json
            results = []
            for r in rows:
                row = dict(r)
                if isinstance(row.get("reasoning"), str):
                    row["reasoning"] = _json.loads(row["reasoning"])
                row["bet_type"] = bt
                results.append(row)
            return results
    except Exception:
        conn.rollback()  # fixture_scores may not exist yet — reset tx and fall through

    # Fall back: compute live from team_form (used until first nightly score run)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_FIXTURE_COLS} {_FIXTURE_JOINS} {where} ORDER BY f.kickoff_time",
            params,
        )
        fixtures = cur.fetchall()

    if not fixtures:
        return []

    team_ids = list({f["home_team_id"] for f in fixtures} | {f["away_team_id"] for f in fixtures})
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM team_form WHERE team_id = ANY(%s)", (team_ids,))
        forms = {r["team_id"]: r for r in cur.fetchall()}
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, attack, defence FROM team_ratings WHERE team_id = ANY(%s)", (team_ids,))
        ratings = {r["team_id"]: r for r in cur.fetchall()}
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM model_params")
        mparams = {r["key"]: r["value"] for r in cur.fetchall()}

    gamma = mparams.get("gamma", 1.3)
    rho = mparams.get("rho", -0.1)

    results = []
    for f in fixtures:
        hf = forms.get(f["home_team_id"])
        af = forms.get(f["away_team_id"])
        if not hf or not af:
            continue

        hf = dict(hf)
        af = dict(af)

        score, reasoning, pick = _score_fixture(
            bt, hf, af,
            ratings.get(f["home_team_id"]), ratings.get(f["away_team_id"]),
            gamma, rho,
        )

        results.append({
            **dict(f),
            "score":     score,
            "reasoning": reasoning,
            "pick":      pick,
            "bet_type":  bt,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results
