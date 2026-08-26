"""Build EconomyFlows/seed.json.

Backbone: Filecoin Data Portal daily_network_metrics.parquet (supply, issuance,
burn, all cumulative). Exception: burn by mechanism comes from the Starboard
Network Health API, because the portal's daily metrics do not itemize burn by
mechanism reliably (recent rows zero the breakdown columns and the penalty
column has corrupted values; checked 2026-08-26).

Run daily by .github/workflows/update-economy-flows.yml. Local run:
    pip install duckdb && python EconomyFlows/build_seed.py
"""
import datetime as dt
import json
import os
import time
import urllib.request

import duckdb

FDP = "https://data.filecoindataportal.xyz/daily_network_metrics.parquet"
SB = "https://networkhealth.starboard.ventures/v2/v2"
OUT = os.path.join(os.path.dirname(__file__), "seed.json")


def fdp_rows():
    con = duckdb.connect()
    return con.execute(
        f"""SELECT date, circulating_fil, mined_fil, vested_fil, locked_fil, burnt_fil
            FROM '{FDP}' WHERE circulating_fil IS NOT NULL ORDER BY date"""
    ).fetchall()


def sb_gas_rows():
    # ponytail: full re-pull every run (~25 requests); immutable past re-fetches
    # identically, so the script stays stateless and self-healing.
    rows, d, today = [], dt.date(2020, 10, 1), dt.date.today()
    while d <= today:
        e = min(d + dt.timedelta(days=88), today)  # API caps windows at 90 days
        url = f"{SB}/gas/daily_network_fee_breakdown?start_date={d}&end_date={e}"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    rows += json.load(r).get("data") or []
                break
            except Exception:
                time.sleep(1 + attempt)
        d = e + dt.timedelta(days=1)
        time.sleep(0.15)
    return rows


def main():
    sup = fdp_rows()
    gas = sb_gas_rows()
    assert sup and gas, "empty source data"

    fdp_max = str(sup[-1][0])[:10]
    sb_max = str(gas[-1]["stat_date"])[:10]

    # monthly supply: last row per month, complete months only.
    # vested slot holds vested + reserve disbursed, recovered from the identity
    # circulating = mined + vested + disbursed - locked - burnt (holds per row).
    by_month = {}
    for r in sup:
        by_month[str(r[0])[:7]] = r
    supply = [
        [m, round(r[1]), round(r[2]), round(r[1] - r[2] + r[4] + r[5]), round(r[4]), round(r[5])]
        for m, r in sorted(by_month.items())
        if m < fdp_max[:7]
    ]

    # monthly burn-by-mechanism sums (Starboard), complete months only
    gm = {}
    for r in gas:
        a = gm.setdefault(str(r["stat_date"])[:7], [0.0] * 5)
        a[0] += r["base_fee_burn"]
        a[1] += r["overestimation_burn"]
        a[2] += r["precommit_batch_fee_burn"] + r["provecommit_batch_fee_burn"]
        a[3] += r["penalty_fee_burn"]
        a[4] += r["miner_tip"]
    gas_monthly = [[m] + [round(v, 1) for v in a] for m, a in sorted(gm.items()) if m < sb_max[:7]]

    def sup_obj(r, extra=None):
        o = {
            "stat_date": str(r[0])[:10],
            "circulating_fil": r[1],
            "mined_fil": r[2],
            "burnt_fil": r[5],
        }
        if extra:
            o.update(extra)
        return o

    last, prev = sup[-1], sup[-2]
    latest = sup_obj(
        last,
        {
            "vested_fil": last[3],
            "reserve_disbursed_fil": last[1] - (last[2] + last[3] - last[4] - last[5]),
            "locked_fil": last[4],
            "circulating_fil_increase": last[1] - prev[1],
            "mined_fil_increase": last[2] - prev[2],
            "burnt_fil_increase": last[5] - prev[5],
        },
    )
    target = sup[-1][0] - dt.timedelta(days=365)
    year_ago = sup_obj(max((r for r in sup if r[0] <= target), key=lambda r: r[0]))

    seed = {
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "fdp_through": fdp_max,
        "sb_gas_through": sb_max,
        "supply": supply,
        "gas": gas_monthly,
        "latest": latest,
        "yearAgo": year_ago,
        "tail": [sup_obj(r) for r in sup[-100:]],
        "gasTail": [
            {k: r[k] for k in ("stat_date", "base_fee_burn", "overestimation_burn",
                               "precommit_batch_fee_burn", "provecommit_batch_fee_burn",
                               "penalty_fee_burn")}
            for r in gas[-40:]
        ],
    }
    with open(OUT, "w") as f:
        json.dump(seed, f, separators=(",", ":"))
    print(f"seed.json: {os.path.getsize(OUT)} bytes, FDP through {fdp_max}, "
          f"Starboard gas through {sb_max}, {len(supply)} supply months, "
          f"{len(gas_monthly)} gas months")
    # smallest check that fails if the accounting logic breaks
    assert abs(latest["reserve_disbursed_fil"] - 17.07e6) < 1e6, latest["reserve_disbursed_fil"]
    assert year_ago["stat_date"] < fdp_max


if __name__ == "__main__":
    main()
