"""
Dixon-Coles team rating estimation.

For each team estimates:
  attack (α) — attacking strength relative to league average
  defence (β) — defensive strength relative to league average

Plus a global home advantage (γ).

Expected goals:
  λ_home = α_home × β_away × γ
  λ_away = α_away × β_home

Low-score correction (ρ) adjusts probabilities for 0-0, 1-0, 0-1, 1-1
as these are systematically mis-predicted by pure Poisson.

Matches are weighted by time decay: weight = exp(-decay × days_ago)
so recent matches matter more without a hard cutoff.
"""
import math
import os
from datetime import datetime, timezone

import numpy as np
import psycopg2
from scipy.optimize import minimize

DB = os.environ["DB_CONNECTION_STRING"]
DECAY = 0.003          # ~half-weight at 230 days
MIN_MATCHES = 5        # teams with fewer matches get average ratings
MAX_ITER = 2000


def get_conn():
    return psycopg2.connect(DB)


def _dc_correction(lam_h, lam_a, i, j, rho):
    """Dixon-Coles low-score correction factor τ."""
    if i == 0 and j == 0:
        return 1 - lam_h * lam_a * rho
    elif i == 1 and j == 0:
        return 1 + lam_a * rho
    elif i == 0 and j == 1:
        return 1 + lam_h * rho
    elif i == 1 and j == 1:
        return 1 - rho
    return 1.0


def _poisson_pmf(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _neg_log_likelihood(params, matches, team_index):
    """Negative log likelihood for Dixon-Coles model."""
    n = len(team_index)
    # params layout: [alpha_0..n-1, beta_0..n-1, gamma, rho]
    alphas = params[:n]
    betas = params[n:2*n]
    gamma = params[2*n]
    rho = params[2*n + 1]

    # Constrain rho to valid range
    if rho > 0.2 or rho < -0.2:
        return 1e9

    total = 0.0
    for home_idx, away_idx, hg, ag, weight in matches:
        lam_h = alphas[home_idx] * betas[away_idx] * gamma
        lam_a = alphas[away_idx] * betas[home_idx]

        if lam_h <= 0 or lam_a <= 0:
            return 1e9

        tau = _dc_correction(lam_h, lam_a, hg, ag, rho)
        if tau <= 0:
            return 1e9

        ll = (math.log(tau)
              + math.log(_poisson_pmf(lam_h, hg))
              + math.log(_poisson_pmf(lam_a, ag))
              + math.log(weight))
        total -= ll

    return total


def main():
    conn = get_conn()
    now = datetime.now(timezone.utc)
    print(f"Rating estimation started at {now.isoformat()}")

    # Load all results with days ago
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.home_team_id, f.away_team_id,
                   r.home_goals, r.away_goals,
                   f.kickoff_time
            FROM fixtures f
            JOIN results r ON r.fixture_id = f.id
            WHERE r.home_goals IS NOT NULL AND r.away_goals IS NOT NULL
            ORDER BY f.kickoff_time DESC
        """)
        rows = cur.fetchall()

    print(f"  Loaded {len(rows)} results")

    # Build team index
    team_ids = sorted(set(r[0] for r in rows) | set(r[1] for r in rows))
    team_index = {tid: i for i, tid in enumerate(team_ids)}
    n = len(team_ids)
    print(f"  {n} teams")

    # Build match list with time-decay weights
    matches = []
    for home_id, away_id, hg, ag, kickoff in rows:
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        days_ago = (now - kickoff).days
        weight = math.exp(-DECAY * days_ago)
        matches.append((
            team_index[home_id],
            team_index[away_id],
            int(hg), int(ag),
            weight,
        ))

    # Filter teams with enough matches
    match_counts = {}
    for hi, ai, _, _, _ in matches:
        match_counts[hi] = match_counts.get(hi, 0) + 1
        match_counts[ai] = match_counts.get(ai, 0) + 1

    # Initial params: all attack=1, defence=1, gamma=1.3 (typical home advantage), rho=-0.1
    x0 = np.ones(2 * n + 2)
    x0[2*n] = 1.3    # home advantage
    x0[2*n + 1] = -0.1  # rho

    # Bounds: attack/defence > 0.1, gamma > 1, rho in (-0.2, 0.2)
    bounds = (
        [(0.1, 5.0)] * n +      # alphas
        [(0.1, 5.0)] * n +      # betas
        [(1.0, 2.0)] +          # gamma
        [(-0.2, 0.2)]           # rho
    )

    # Identification constraint: fix average attack = 1 by fixing first team's alpha
    # (scipy doesn't support equality constraints easily with L-BFGS-B, so we
    #  normalise after optimisation instead)
    print("  Optimising... (this may take 30-60 seconds)")
    result = minimize(
        _neg_log_likelihood,
        x0,
        args=(matches, team_index),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": MAX_ITER, "ftol": 1e-9},
    )

    if not result.success:
        print(f"  Warning: optimiser did not fully converge: {result.message}")

    params = result.x
    alphas = params[:n]
    betas = params[n:2*n]
    gamma = params[2*n]
    rho = params[2*n + 1]

    # Normalise so mean attack = 1
    mean_alpha = np.mean(alphas)
    alphas = alphas / mean_alpha
    gamma = gamma * mean_alpha  # absorb scale into gamma
    mean_beta = np.mean(betas)
    betas = betas / mean_beta

    print(f"  Home advantage γ = {gamma:.3f}")
    print(f"  Low-score correction ρ = {rho:.4f}")

    # Save ratings
    with conn.cursor() as cur:
        for tid, idx in team_index.items():
            if match_counts.get(idx, 0) < MIN_MATCHES:
                # Use average ratings for teams with too few matches
                atk, dfn = 1.0, 1.0
            else:
                atk = float(alphas[idx])
                dfn = float(betas[idx])
            cur.execute("""
                INSERT INTO team_ratings (team_id, attack, defence, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (team_id) DO UPDATE SET
                    attack = EXCLUDED.attack,
                    defence = EXCLUDED.defence,
                    updated_at = NOW()
            """, (tid, atk, dfn))

        cur.execute("""
            INSERT INTO model_params (key, value, updated_at)
            VALUES ('gamma', %s, NOW()), ('rho', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (float(gamma), float(rho)))

    conn.commit()
    conn.close()

    # Print top 10 attack / best defence for sanity check
    top_atk = sorted(zip(alphas, team_ids), reverse=True)[:10]
    top_dfn = sorted(zip(betas, team_ids))[:10]  # lower beta = harder to score against
    print("  Top 10 attack ratings:", [(f"{a:.2f}", tid) for a, tid in top_atk])
    print("  Top 10 defence ratings (lower=better):", [(f"{b:.2f}", tid) for b, tid in top_dfn])
    print("Rating estimation complete")


if __name__ == "__main__":
    main()
