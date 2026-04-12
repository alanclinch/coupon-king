"""Print min/max/avg/median for each bet type from fixture_scores."""
import os
import psycopg2

DB = os.environ["DB_CONNECTION_STRING"]

with psycopg2.connect(DB) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT bet_type,
                   COUNT(*)                         AS count,
                   ROUND(MIN(score)::numeric, 1)    AS min,
                   ROUND(MAX(score)::numeric, 1)    AS max,
                   ROUND(AVG(score)::numeric, 1)    AS avg,
                   ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score)::numeric, 1) AS median,
                   ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY score)::numeric, 1) AS p25,
                   ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY score)::numeric, 1) AS p75
            FROM fixture_scores
            GROUP BY bet_type
            ORDER BY bet_type
        """)
        rows = cur.fetchall()

print(f"{'Bet Type':<16} {'Count':>6} {'Min':>6} {'Max':>6} {'Avg':>6} {'Median':>8} {'P25':>6} {'P75':>6}")
print("-" * 68)
for row in rows:
    print(f"{row[0]:<16} {row[1]:>6} {row[2]:>6} {row[3]:>6} {row[4]:>6} {row[5]:>8} {row[6]:>6} {row[7]:>6}")
