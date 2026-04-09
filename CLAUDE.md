# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Coupon King is a football data aggregation project. It pulls fixtures, teams, and results from three external APIs into a shared PostgreSQL database, normalizing them under a common schema. A GitHub Actions workflow runs the sync scripts nightly at 1am UTC.

## Running Scripts Locally

All scripts require environment variables set before running:

```bash
# Sportmonks (Scottish Premiership)
DB_CONNECTION_STRING=... SPORTMONKS_API_KEY=... python scripts/sync_sportmonks.py

# football-data.org (Premier League, Championship, Champions League)
DB_CONNECTION_STRING=... FOOTBALL_DATA_API_KEY=... python scripts/sync_football_data.py

# API-Sports (Scottish leagues, lower English leagues, cups, European)
DB_CONNECTION_STRING=... API_SPORTS_KEY=... python scripts/sync_api_sports.py
```

Install dependencies:
```bash
pip install -r requirements.txt
```

## Architecture

### Database Schema (PostgreSQL)

The central tables are:
- `leagues` — pre-seeded league records; scripts look up leagues by name, never insert them
- `teams` — inserted on first encounter; keyed by name + league
- `fixtures` — one row per match; fields: `home_team_id`, `away_team_id`, `league_id`, `season`, `kickoff_time`, `status`, `matchday`
- `results` — one row per fixture (unique on `fixture_id`); fields: `home_goals`, `away_goals`, plus optional `home_goals_ht`/`away_goals_ht`
- `api_id_map` — cross-reference table mapping external API IDs to local IDs; columns: `table_name`, `local_id`, `api_source`, `api_id`

### ID Mapping Pattern

Each script never stores raw external IDs in the main tables. Instead, all external IDs are stored in `api_id_map` with `api_source` set to `'sportmonks'`, `'football-data'`, or `'api-sports'`. Before inserting a team or fixture, the script checks `api_id_map` — if a mapping exists, it reuses the local ID; otherwise it inserts and records the new mapping.

### Per-Script Scope

| Script | Source | Leagues covered |
|---|---|---|
| `sync_sportmonks.py` | Sportmonks v3 | Scottish Premiership only (league ID 501) |
| `sync_football_data.py` | football-data.org v4 | Premier League (PL), Championship (ELC), Champions League (CL) |
| `sync_api_sports.py` | API-Sports v3 | Scottish Championship/League One/Two/Cup, lower English leagues (L1/L2/National), FA Cup, EFL Cup, Europa League, Conference League |

### Result Handling

- `sync_sportmonks.py`: saves results for scores with `description == "CURRENT"`
- `sync_football_data.py`: saves results when `status == "FINISHED"`, includes half-time scores
- `sync_api_sports.py`: saves results when fixture status short code is `"FT"`, includes half-time scores; adds `time.sleep(1)` between league/season calls to respect rate limits

### CI

`.github/workflows/nightly_sync.yml` runs all three scripts sequentially each night. Secrets required: `DB_CONNECTION_STRING`, `SPORTMONKS_API_KEY`, `FOOTBALL_DATA_API_KEY`, `API_SPORTS_KEY`.
