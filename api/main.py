import os
from datetime import date, timedelta
from typing import List, Optional

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
            if hf and hf["wins"] == 0:
                flags.append(f"{fixture['home_team']} have not won in their last {hf['played']} matches (form: {hf['form_string']})")
            if hf and hf["losses"] >= 4:
                flags.append(f"{fixture['home_team']} have lost {hf['losses']} of their last {hf['played']} matches")
            if af and af["wins"] >= 4:
                flags.append(f"{fixture['away_team']} are in excellent form — {af['wins']}W in last {af['played']}")

        elif market == "2":
            if af and af["wins"] == 0:
                flags.append(f"{fixture['away_team']} have not won in their last {af['played']} matches (form: {af['form_string']})")
            if af and af["losses"] >= 4:
                flags.append(f"{fixture['away_team']} have lost {af['losses']} of their last {af['played']} matches")
            if hf and hf["wins"] >= 4:
                flags.append(f"{fixture['home_team']} are in excellent form — {hf['wins']}W in last {hf['played']}")

        elif market == "X":
            if hf and hf["draws"] == 0:
                flags.append(f"{fixture['home_team']} have not drawn in their last {hf['played']} matches")
            if af and af["draws"] == 0:
                flags.append(f"{fixture['away_team']} have not drawn in their last {af['played']} matches")

        elif market == "BTTS_YES":
            if hf and hf["goals_scored"] == 0:
                flags.append(f"{fixture['home_team']} have not scored in their last {hf['played']} matches")
            if af and af["goals_scored"] == 0:
                flags.append(f"{fixture['away_team']} have not scored in their last {af['played']} matches")
            if hf and hf["clean_sheets"] >= 4:
                flags.append(f"{fixture['home_team']} have kept {hf['clean_sheets']} clean sheets in last {hf['played']}")
            if af and af["clean_sheets"] >= 4:
                flags.append(f"{fixture['away_team']} have kept {af['clean_sheets']} clean sheets in last {af['played']}")

        elif market == "BTTS_NO":
            if hf and hf["goals_scored"] >= 8:
                flags.append(f"{fixture['home_team']} are high scorers — {hf['goals_scored']} goals in last {hf['played']}")
            if af and af["goals_scored"] >= 8:
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
                    elif market == "UNDER25" and avg > 3.5:
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
