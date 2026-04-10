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

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM team_form WHERE team_id IN (%s, %s)",
            (fixture["home_team_id"], fixture["away_team_id"]),
        )
        forms = {r["team_id"]: r for r in cur.fetchall()}

    hf = forms.get(fixture["home_team_id"])
    af = forms.get(fixture["away_team_id"])
    if not hf or not af:
        return {}

    out = {}
    for bt in ["BTTS", "OVER25", "WIN", "BTTS_WIN", "BTTS_NODRAW", "BTTS_OVER25"]:
        score, reasoning, pick = _score_fixture(bt, dict(hf), dict(af))
        out[bt] = {"score": score, "pick": pick, "reasoning": reasoning}
    return out


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
            cur.execute(
                "SELECT * FROM team_form WHERE team_id IN (%s, %s)",
                (home_id, away_id),
            )
            forms = {r["team_id"]: r for r in cur.fetchall()}

        hf = forms.get(home_id)  # home form
        af = forms.get(away_id)  # away form
        market = sel.market.upper()
        flags = []

        if market == "1":
            if hf and hf["wins"] <= 1:
                flags.append(f"{fixture['home_team']} have only {hf['wins']} win(s) in their last {hf['played']} matches (form: {hf['form_string']})")
            if hf and hf["losses"] >= 3:
                flags.append(f"{fixture['home_team']} have lost {hf['losses']} of their last {hf['played']} matches")
            if af and af["wins"] >= 3:
                flags.append(f"{fixture['away_team']} are in good form — {af['wins']}W in last {af['played']}")

        elif market == "2":
            if af and af["wins"] <= 1:
                flags.append(f"{fixture['away_team']} have only {af['wins']} win(s) in their last {af['played']} matches (form: {af['form_string']})")
            if af and af["losses"] >= 3:
                flags.append(f"{fixture['away_team']} have lost {af['losses']} of their last {af['played']} matches")
            if hf and hf["wins"] >= 3:
                flags.append(f"{fixture['home_team']} are in good form — {hf['wins']}W in last {hf['played']}")

        elif market == "X":
            if hf and hf["draws"] <= 1:
                flags.append(f"{fixture['home_team']} have only {hf['draws']} draw(s) in their last {hf['played']} matches")
            if af and af["draws"] <= 1:
                flags.append(f"{fixture['away_team']} have only {af['draws']} draw(s) in their last {af['played']} matches")

        elif market == "BTTS_YES":
            if hf and hf["goals_scored"] <= 2:
                flags.append(f"{fixture['home_team']} have scored only {hf['goals_scored']} goal(s) in their last {hf['played']} matches")
            if af and af["goals_scored"] <= 2:
                flags.append(f"{fixture['away_team']} have scored only {af['goals_scored']} goal(s) in their last {af['played']} matches")
            if hf and hf["clean_sheets"] >= 3:
                flags.append(f"{fixture['home_team']} have kept {hf['clean_sheets']} clean sheets in last {hf['played']}")
            if af and af["clean_sheets"] >= 3:
                flags.append(f"{fixture['away_team']} have kept {af['clean_sheets']} clean sheets in last {af['played']}")

        elif market == "BTTS_NO":
            if hf and hf["goals_scored"] >= 6:
                flags.append(f"{fixture['home_team']} are high scorers — {hf['goals_scored']} goals in last {hf['played']}")
            if af and af["goals_scored"] >= 6:
                flags.append(f"{fixture['away_team']} are high scorers — {af['goals_scored']} goals in last {af['played']}")

        elif market in ("OVER25", "UNDER25"):
            if hf and af:
                total = (hf["goals_scored"] + hf["goals_conceded"]
                         + af["goals_scored"] + af["goals_conceded"])
                played = hf["played"] + af["played"]
                if played > 0:
                    avg = total / played
                    if market == "OVER25" and avg < 2.0:
                        flags.append(f"Combined average of {avg:.1f} goals/match — low-scoring sides")
                    elif market == "UNDER25" and avg > 3.0:
                        flags.append(f"Combined average of {avg:.1f} goals/match — high-scoring sides")

        risk = "high" if len(flags) >= 2 else "medium" if len(flags) == 1 else "low"
        output.append({
            "fixture_id":  sel.fixture_id,
            "market":      sel.market,
            "home_team":   fixture["home_team"],
            "away_team":   fixture["away_team"],
            "home_form":   hf["form_string"] if hf else None,
            "away_form":   af["form_string"] if af else None,
            "flags":       flags,
            "risk":        risk,
        })

    return output


# ── Smart Picks ───────────────────────────────────────────────────────────────

def _rate(num: int, den: int) -> float:
    return num / den if den > 0 else 0.0


def _score_fixture(bet_type: str, hf: dict, af: dict) -> Tuple[int, list, Optional[str]]:
    """Return (score 0-100, reasoning lines, pick 'home'|'away'|None)."""
    hp = hf["played"] or 1
    ap = af["played"] or 1

    h_win   = _rate(hf["wins"],         hp)
    h_draw  = _rate(hf["draws"],        hp)
    h_loss  = _rate(hf["losses"],       hp)
    h_att   = _rate(hf["goals_scored"], hp)
    h_def   = _rate(hf["goals_conceded"], hp)
    h_cs    = _rate(hf["clean_sheets"], hp)

    a_win   = _rate(af["wins"],         ap)
    a_draw  = _rate(af["draws"],        ap)
    a_loss  = _rate(af["losses"],       ap)
    a_att   = _rate(af["goals_scored"], ap)
    a_def   = _rate(af["goals_conceded"], ap)
    a_cs    = _rate(af["clean_sheets"], ap)

    hn = hf["home_team"] if "home_team" in hf else "Home"
    an = af["away_team"] if "away_team" in af else "Away"

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
            f"Likely winner: {'home' if pick=='home' else 'away'} ({hf['wins']}W vs {af['wins']}W in last 5)",
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

    results = []
    for f in fixtures:
        hf = forms.get(f["home_team_id"])
        af = forms.get(f["away_team_id"])
        if not hf or not af:
            continue

        hf = dict(hf); hf["home_team"] = f["home_team"]
        af = dict(af); af["away_team"] = f["away_team"]

        score, reasoning, pick = _score_fixture(bt, hf, af)

        results.append({
            **dict(f),
            "score":     score,
            "reasoning": reasoning,
            "pick":      pick,
            "bet_type":  bt,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results
