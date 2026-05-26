#!/usr/bin/env python3
"""
Filecoin L1 Health Dashboard — Auto-update script.

Fetches data from the Filecoin Data Portal (filecoindataportal.xyz),
computes decentralization + consensus-security metrics, and writes
a self-contained HTML dashboard.

Requirements:
    pip install duckdb

Usage:
    python update_dashboard.py                     # basic run
    python update_dashboard.py -o dashboard.html   # custom output path
    python update_dashboard.py --attack-cost-csv "Cost of 33% attack.csv"  # use your spreadsheet for attack cost

Data sources (all from https://data.filecoindataportal.xyz):
    - daily_network_metrics.parquet       (~530 KB)
    - daily_storage_providers_metrics.parquet (~94 MB — cached locally)
    - storage_providers.parquet           (~900 KB)
"""

import argparse, csv, json, os, sys, time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import duckdb
except ImportError:
    sys.exit("duckdb not installed. Run:  pip install duckdb")

# ── Config ──────────────────────────────────────────────────────────
BASE_URL   = "https://data.filecoindataportal.xyz"
NETWORK    = f"{BASE_URL}/daily_network_metrics.parquet"
SP_DAILY   = f"{BASE_URL}/daily_storage_providers_metrics.parquet"
SP_STATIC  = f"{BASE_URL}/storage_providers.parquet"
CACHE_DIR  = Path(__file__).parent / ".cache"

# ── Helpers ─────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def cache_parquet(url: str, name: str, max_age_hours: int = 12) -> str:
    """Download parquet to local cache if stale; return local path."""
    CACHE_DIR.mkdir(exist_ok=True)
    local = CACHE_DIR / name
    if local.exists():
        age_h = (time.time() - local.stat().st_mtime) / 3600
        if age_h < max_age_hours:
            log(f"  Using cached {name} ({age_h:.1f}h old)")
            return str(local)
    log(f"  Downloading {name} …")
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FilecoinDashboard/1.0"})
    with urllib.request.urlopen(req) as resp, open(local, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)  # 1 MB chunks
            if not chunk:
                break
            f.write(chunk)
    log(f"  Saved {local.stat().st_size / 1e6:.1f} MB → {local}")
    return str(local)

def arr(rows, col=0):
    """Extract column from DuckDB result as a Python list."""
    return [r[col] for r in rows]

def round_list(lst, n=2):
    return [round(x, n) if x is not None else None for x in lst]

def parse_attack_cost_csv(csv_path: str):
    """Parse Irene's attack-cost spreadsheet CSV.
    Returns (dates, cost_m, tvl_m, roi) — all weekly-sampled lists.
    Cost is in millions FIL, TVL in millions FIL.
    """
    log(f"  Reading attack cost CSV: {csv_path}")
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            date = r.get("stateTime", "").strip()
            cost_raw = r.get("Cost of the 33% attack (FIL)", "").strip()
            tvl_raw = r.get("Total FIL Locked (M FIL)", "").strip()
            roi_raw = r.get("33% attack ROI (TVL/Cost)", "").strip()
            if not date or not cost_raw or cost_raw == "#DIV/0!":
                continue
            try:
                cost_fil = float(cost_raw.replace(",", "")) / 1e6  # raw FIL → millions
                tvl_m = abs(float(tvl_raw.replace(",", "")))        # abs to handle negative sign
                roi = float(roi_raw.replace(",", "")) if roi_raw and roi_raw != "#DIV/0!" else None
                rows.append((date, cost_fil, tvl_m, roi))
            except (ValueError, TypeError):
                continue

    # Sample weekly (every 7th row, starting from first Thursday)
    # Find first Thursday
    from datetime import date as dt_date
    weekly = []
    last_date = None
    for date_str, cost, tvl, roi in rows:
        d = dt_date.fromisoformat(date_str)
        if d.weekday() != 3:  # Thursday
            continue
        weekly.append((date_str, cost, tvl, roi))

    log(f"  → {len(weekly)} weekly samples from CSV ({weekly[0][0]} – {weekly[-1][0]})")
    return (
        [w[0] for w in weekly],
        round_list([w[1] for w in weekly]),
        round_list([w[2] for w in weekly]),
        round_list([w[3] for w in weekly]),
    )

# ── Data fetching ───────────────────────────────────────────────────
def fetch_all(con, attack_cost_csv=None):
    """Return dict with all computed metric arrays."""
    log("Fetching network metrics …")
    net_path = NETWORK  # small file, query remote directly

    log("Caching large SP daily file …")
    sp_daily_path = cache_parquet(SP_DAILY, "daily_storage_providers_metrics.parquet")
    sp_static_path = cache_parquet(SP_STATIC, "storage_providers.parquet")

    data = {}

    # ─── 1. Decentralization: reward concentration (30d rolling, weekly) ───
    log("Computing reward concentration (30d rolling avg, weekly samples) …")
    rows = con.sql(f"""
        WITH daily_owner AS (
            SELECT d.date, s.owner_id, SUM(d.block_rewards_fil) AS rewards
            FROM '{sp_daily_path}' d
            JOIN '{sp_static_path}' s ON d.provider_id = s.provider_id
            GROUP BY d.date, s.owner_id
        ),
        daily_shares AS (
            SELECT date,
                SUM(CASE WHEN rnk =  1 THEN pct ELSE 0 END) AS top1,
                SUM(CASE WHEN rnk <= 5 THEN pct ELSE 0 END) AS top5,
                SUM(CASE WHEN rnk <=10 THEN pct ELSE 0 END) AS top10,
                COUNT(*) AS owner_count
            FROM (
                SELECT date, owner_id,
                    100.0 * rewards / SUM(rewards) OVER (PARTITION BY date) AS pct,
                    ROW_NUMBER() OVER (PARTITION BY date ORDER BY rewards DESC) AS rnk
                FROM daily_owner
            ) sub
            GROUP BY date
        ),
        rolling AS (
            SELECT date,
                AVG(top1)  OVER w AS top1_30d,
                AVG(top5)  OVER w AS top5_30d,
                AVG(top10) OVER w AS top10_30d,
                owner_count AS owners_daily,
                AVG(owner_count) OVER w AS owners_30d
            FROM daily_shares
            WINDOW w AS (ORDER BY date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
        )
        SELECT date, top1_30d, top5_30d, top10_30d,
               owners_daily, owners_30d
        FROM rolling
        WHERE EXTRACT(DOW FROM date) = 1   -- Mondays
          AND date >= '2020-08-24'
        ORDER BY date
    """).fetchall()

    data["decDates"]    = [str(r[0]) for r in rows]
    data["top1"]        = round_list(arr(rows, 1))
    data["top5"]        = round_list(arr(rows, 2))
    data["top10"]       = round_list(arr(rows, 3))
    data["ownersDaily"] = [int(r[4]) for r in rows]
    data["owners30d"]   = round_list(arr(rows, 5))
    log(f"  → {len(rows)} weekly samples ({data['decDates'][0]} – {data['decDates'][-1]})")

    # ─── 2. Nakamoto coefficient @33% (monthly) ─────────────────────────
    log("Computing Nakamoto coefficient (monthly) …")
    rows_nk = con.sql(f"""
        WITH monthly_owner AS (
            SELECT DATE_TRUNC('month', d.date) AS month,
                   s.owner_id,
                   SUM(d.block_rewards_fil) AS rewards
            FROM '{sp_daily_path}' d
            JOIN '{sp_static_path}' s ON d.provider_id = s.provider_id
            GROUP BY month, s.owner_id
        ),
        ranked AS (
            SELECT month, owner_id, rewards,
                ROW_NUMBER() OVER (PARTITION BY month ORDER BY rewards DESC) AS rnk,
                SUM(rewards) OVER (PARTITION BY month ORDER BY rewards DESC
                                   ROWS UNBOUNDED PRECEDING)
                    / SUM(rewards) OVER (PARTITION BY month) * 100 AS cum_pct,
                COUNT(*) OVER (PARTITION BY month) AS total_owners
            FROM monthly_owner
        )
        SELECT
            STRFTIME(month, '%Y-%m') AS ym,
            MIN(CASE WHEN cum_pct >= 33 THEN rnk END) AS nk33,
            MAX(total_owners) AS owners
        FROM ranked
        GROUP BY month, ym
        ORDER BY month
    """).fetchall()

    data["nkMonths"]  = [r[0] for r in rows_nk]
    data["nk33"]      = [int(r[1]) if r[1] else None for r in rows_nk]
    data["nkOwners"]  = [int(r[2]) for r in rows_nk]
    log(f"  → {len(rows_nk)} months ({data['nkMonths'][0]} – {data['nkMonths'][-1]})")

    # ─── 3. Consensus security ─────────────────────────────────────────
    if attack_cost_csv:
        log("Loading attack cost from CSV …")
        csv_dates, csv_cost, csv_tvl, csv_roi = parse_attack_cost_csv(attack_cost_csv)

        # Extend with data portal for dates beyond the CSV
        last_csv_date = csv_dates[-1]
        log(f"  CSV ends at {last_csv_date}, checking for newer data …")
        rows_ext = con.sql(f"""
            SELECT date,
                locked_fil / 1e6 AS tvl_m
            FROM '{net_path}'
            WHERE EXTRACT(DOW FROM date) = 4
              AND date > '{last_csv_date}'
            ORDER BY date
        """).fetchall()

        if rows_ext:
            # Extend TVL from data portal; extrapolate attack cost trend
            last_cost = csv_cost[-1]
            last_tvl = csv_tvl[-1]
            for r in rows_ext:
                new_tvl = round(r[1], 2)
                # Scale attack cost proportionally to TVL change
                ratio = new_tvl / last_tvl if last_tvl else 1
                new_cost = round(last_cost * ratio, 2)
                new_roi = round(new_tvl / new_cost, 2) if new_cost > 0 else None
                csv_dates.append(str(r[0]))
                csv_cost.append(new_cost)
                csv_tvl.append(new_tvl)
                csv_roi.append(new_roi)
            log(f"  Extended with {len(rows_ext)} weeks from data portal (→ {csv_dates[-1]})")

        data["atkDates"]        = csv_dates
        data["attack_cost_fil"] = csv_cost
        data["tvl_fil"]         = csv_tvl
        data["roi_fil"]         = csv_roi
        data["atk_method"]      = "csv"
    else:
        log("Computing consensus security from data portal (approximate) …")
        rows_net = con.sql(f"""
            SELECT date,
                locked_fil / 1e6                     AS tvl_m,
                pledge_collateral_fil * (33.0/67) / 1e6 AS attack_cost_m,
                locked_fil / NULLIF(pledge_collateral_fil * (33.0/67), 0) AS roi
            FROM '{net_path}'
            WHERE EXTRACT(DOW FROM date) = 4
              AND date >= '2020-10-01'
            ORDER BY date
        """).fetchall()

        data["atkDates"]        = [str(r[0]) for r in rows_net]
        data["tvl_fil"]         = round_list(arr(rows_net, 1))
        data["attack_cost_fil"] = round_list(arr(rows_net, 2))
        data["roi_fil"]         = round_list(arr(rows_net, 3))
        data["atk_method"]      = "approx"

    log(f"  → {len(data['atkDates'])} weekly samples ({data['atkDates'][0]} – {data['atkDates'][-1]})")

    # ─── 4. Stat-card values (latest) ───────────────────────────────────
    if data["top1"]:
        data["stat_nk"]       = data["nk33"][-1]
        data["stat_nk_peak"]  = max(x for x in data["nk33"] if x is not None)
        nk_peak_idx           = data["nk33"].index(data["stat_nk_peak"])
        data["stat_nk_peak_date"] = data["nkMonths"][nk_peak_idx]
        data["stat_top5"]     = data["top5"][-1]
        data["stat_top10"]    = data["top10"][-1]
        data["stat_owners"]   = data["ownersDaily"][-1]
        data["stat_owners_peak"] = max(data["ownersDaily"])
        owners_peak_idx = data["ownersDaily"].index(data["stat_owners_peak"])
        data["stat_owners_peak_date"] = data["decDates"][owners_peak_idx]
        nk_decline = round(100 * (1 - data["stat_nk"] / data["stat_nk_peak"]))
        data["stat_nk_decline"] = f"-{nk_decline}%"

    if data["attack_cost_fil"]:
        data["stat_atk_cost"]     = data["attack_cost_fil"][-1]
        data["stat_atk_cost_peak"]= max(data["attack_cost_fil"])
        atk_peak_idx = data["attack_cost_fil"].index(data["stat_atk_cost_peak"])
        data["stat_atk_cost_peak_date"] = data["atkDates"][atk_peak_idx]
        data["stat_tvl"]          = data["tvl_fil"][-1]
        data["stat_tvl_peak"]     = max(data["tvl_fil"])
        tvl_peak_idx = data["tvl_fil"].index(data["stat_tvl_peak"])
        data["stat_tvl_peak_date"] = data["atkDates"][tvl_peak_idx]
        data["stat_roi"]          = data["roi_fil"][-1]
        data["stat_roi_peak"]     = max(data["roi_fil"])
        roi_peak_idx = data["roi_fil"].index(data["stat_roi_peak"])
        data["stat_roi_peak_date"] = data["atkDates"][roi_peak_idx]

    data["generated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    data["dec_range"]    = f"{data['decDates'][0]} – {data['decDates'][-1]}"
    data["atk_range"]    = f"{data['atkDates'][0]} – {data['atkDates'][-1]}"

    return data


# ── HTML template ───────────────────────────────────────────────────
def build_html(d):
    """Return complete HTML string."""

    def fmt_m(v):
        """Format millions: 34.12 → '34.1M'"""
        return f"{v:.1f}M"

    def fmt_peak(val, date):
        return f"Peak: {fmt_m(val)} ({date[:7]})"

    # Date range for methodology note
    if d.get("atk_method") == "csv":
        atk_method = ("Attack cost = sectors_for_33% &times; expanding median of on-chain "
                      "Initial Pledge per 32 GiB QAP (from spreadsheet)")
    else:
        atk_method = ("Attack cost &asymp; pledge_collateral &times; (33/67). "
                      "Approximation &mdash; use --attack-cost-csv for precise methodology.")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Filecoin L1 Health Metrics</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-annotation/3.0.1/chartjs-plugin-annotation.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #ffffff; color: #1a1a1a; padding: 32px 24px;
  }}
  .wrapper {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 2px; }}
  h2 {{ font-size: 1.2rem; font-weight: 700; margin-bottom: 2px; }}
  .sub {{ font-size: 0.82rem; color: #666; margin-bottom: 28px; line-height: 1.5; }}
  .card {{
    background: #ffffff; border: 1px solid #e0e0e0; border-radius: 14px;
    padding: 24px 20px 16px; margin-bottom: 24px;
  }}
  .card-title {{ font-size: 0.92rem; font-weight: 600; color: #1a1a1a; margin-bottom: 4px; }}
  .card-sub {{ font-size: 0.75rem; color: #888; margin-bottom: 16px; }}
  .chart-wrap {{ position: relative; height: 340px; }}
  .chart-wrap-sm {{ position: relative; height: 220px; }}
  .legend {{
    display: flex; flex-wrap: wrap; gap: 6px 14px;
    margin-top: 14px; justify-content: center;
  }}
  .leg-item {{
    display: flex; align-items: center; gap: 6px;
    font-size: 0.78rem; color: #666; cursor: pointer;
    padding: 5px 10px; border-radius: 8px; transition: all 0.2s;
  }}
  .leg-item:hover {{ background: #f5f5f5; }}
  .leg-item.off {{ opacity: 0.3; }}
  .dot {{ width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; }}
  .stats {{
    display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap;
  }}
  .stat {{
    background: #ffffff; border: 1px solid #e8e8e8; border-radius: 10px; padding: 14px 18px;
    flex: 1; min-width: 130px;
  }}
  .stat-label {{ font-size: 0.7rem; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat-value {{ font-size: 1.4rem; font-weight: 700; color: #1a1a1a; margin-top: 4px; }}
  .stat-sub {{ font-size: 0.7rem; color: #f85149; margin-top: 2px; }}
  .stat-sub.green {{ color: #3fb950; }}
  .section-label {{
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 1px;
    color: #999; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 1px solid #eee;
  }}
  .section-divider {{
    margin: 56px 0 32px;
    padding-bottom: 10px;
    border-bottom: 2px solid #e0e0e0;
  }}
  .note {{ text-align: center; font-size: 0.72rem; color: #999; margin-top: 14px; }}
  .dl-row {{ text-align: center; margin-top: 16px; }}
  .dl-btn {{
    padding: 8px 18px; border-radius: 8px; border: 1px solid #e0e0e0;
    background: #fff; color: #333; cursor: pointer; font-size: 0.78rem;
    margin: 4px;
  }}
  .dl-btn:hover {{ background: #f5f5f5; }}
</style>
</head>
<body>
<div class="wrapper">

  <h1>Filecoin L1 Health Metrics</h1>
  <p class="sub">Companion charts for the L1 Health Metrics Framework report &middot; Source: <a href="https://filecoindataportal.xyz" style="color:#666;">Filecoin Data Portal</a> &middot; Generated: {d['generated_at']}</p>

  <!-- ============================================================ -->
  <!-- SECTION 1: DECENTRALIZATION -->
  <!-- ============================================================ -->
  <div class="section-divider">
    <h2>Decentralization</h2>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">NK @33% (latest)</div>
      <div class="stat-value">{d['stat_nk']}</div>
      <div class="stat-sub">Peak: {d['stat_nk_peak']} ({d['stat_nk_peak_date']})</div>
    </div>
    <div class="stat">
      <div class="stat-label">Top 5 share (30d)</div>
      <div class="stat-value">{d['stat_top5']:.1f}%</div>
      <div class="stat-sub {'green' if d['stat_top5'] < 33 else ''}">{('below' if d['stat_top5'] < 33 else 'above')} 33% threshold</div>
    </div>
    <div class="stat">
      <div class="stat-label">Top 10 share (30d)</div>
      <div class="stat-value">{d['stat_top10']:.1f}%</div>
      <div class="stat-sub{' green' if d['stat_top10'] < 33 else ''}">{('below' if d['stat_top10'] < 33 else 'at/above')} 33% threshold</div>
    </div>
    <div class="stat">
      <div class="stat-label">Active owner_ids</div>
      <div class="stat-value">{d['stat_owners']:,}</div>
      <div class="stat-sub">Peak: {max(d['ownersDaily']):,} ({d['stat_owners_peak_date'][:7]})</div>
    </div>
    <div class="stat">
      <div class="stat-label">NK decline</div>
      <div class="stat-value">{d['stat_nk_decline']}</div>
      <div class="stat-sub">from peak to latest</div>
    </div>
  </div>

  <!-- Reward Concentration -->
  <div class="section-label">Reward Concentration</div>
  <div class="card">
    <div class="card-title">30d average share of the reward pool captured by each day's top owner_ids</div>
    <div class="card-sub">Percentage of daily block rewards going to the top 1, 5, and 10 owner_ids</div>
    <div class="chart-wrap"><canvas id="chartShare"></canvas></div>
    <div class="legend" id="legendShare"></div>
  </div>

  <!-- Owner count -->
  <div class="card">
    <div class="card-title">Owner_ids with rewards</div>
    <div class="card-sub">Number of unique owner_ids earning block rewards each day</div>
    <div class="chart-wrap-sm"><canvas id="chartOwners"></canvas></div>
    <div class="legend" id="legendOwners"></div>
  </div>

  <!-- Nakamoto Coefficient -->
  <div class="section-label">Nakamoto Coefficient</div>
  <div class="card">
    <div class="card-title">Nakamoto Coefficient @33%</div>
    <div class="card-sub">Minimum number of independent owner_ids needed to control 33% of block rewards. Higher = more decentralized.</div>
    <div class="chart-wrap"><canvas id="chartNK"></canvas></div>
    <div class="legend" id="legendNK"></div>
  </div>

  <p class="note">Source: Filecoin Data Portal &middot; {d['dec_range']}</p>
  <div class="dl-row">
    <button class="dl-btn" onclick="dlPng('chartShare','reward_share.png')">Download Reward Share</button>
    <button class="dl-btn" onclick="dlPng('chartOwners','owner_ids.png')">Download Owner IDs</button>
    <button class="dl-btn" onclick="dlPng('chartNK','nakamoto_coefficient.png')">Download Nakamoto</button>
  </div>

  <!-- ============================================================ -->
  <!-- SECTION 2: CONSENSUS SECURITY -->
  <!-- ============================================================ -->
  <div class="section-divider">
    <h2>Consensus Security</h2>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">Attack cost (FIL)</div>
      <div class="stat-value">{fmt_m(d['stat_atk_cost'])}</div>
      <div class="stat-sub">{fmt_peak(d['stat_atk_cost_peak'], d['stat_atk_cost_peak_date'])}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Total locked (FIL)</div>
      <div class="stat-value">{fmt_m(d['stat_tvl'])}</div>
      <div class="stat-sub">{fmt_peak(d['stat_tvl_peak'], d['stat_tvl_peak_date'])}</div>
    </div>
    <div class="stat">
      <div class="stat-label">FoF ROI (TVL/Cost)</div>
      <div class="stat-value">{d['stat_roi']:.2f}</div>
      <div class="stat-sub">Peak: {d['stat_roi_peak']:.2f} ({d['stat_roi_peak_date'][:7]})</div>
    </div>
  </div>

  <!-- Attack Cost FIL -->
  <div class="section-label">FIL-Denominated</div>
  <div class="card">
    <div class="card-title">33% Attack Cost (FIL-denominated)</div>
    <div class="card-sub">Estimated cost to acquire 33% of network power via pledge, and total locked FIL</div>
    <div class="chart-wrap"><canvas id="chartFil"></canvas></div>
    <div class="legend" id="legendFil"></div>
  </div>

  <!-- ROI -->
  <div class="section-label">Attack ROI</div>
  <div class="card">
    <div class="card-title">33% Attack FoF ROI (TVL / Cost)</div>
    <div class="card-sub">A purely rational attacker weighs extractable value (locked FIL) against the pledge cost.</div>
    <div class="chart-wrap"><canvas id="chartRoi"></canvas></div>
    <div class="legend" id="legendRoi"></div>
  </div>

  <p class="note">Source: Filecoin Data Portal &middot; {d['atk_range']} &middot; Methodology: {atk_method}</p>
  <div class="dl-row">
    <button class="dl-btn" onclick="dlPng('chartFil','attack_cost_fil.png')">Download Attack Cost</button>
    <button class="dl-btn" onclick="dlPng('chartRoi','attack_roi.png')">Download ROI</button>
  </div>

  <br><br>
</div>

<script>
// ============================================================
// SHARED HELPERS
// ============================================================
function dlPng(id, name) {{
  const link = document.createElement('a');
  link.download = name;
  link.href = document.getElementById(id).toDataURL('image/png', 1.0);
  link.click();
}}

function makeLegend(el, series, chart) {{
  Object.entries(series).forEach(([name, s], i) => {{
    const d = document.createElement('div');
    d.className = 'leg-item';
    d.innerHTML = '<span class="dot" style="background:' + s.color + '"></span>' + name;
    d.onclick = () => {{
      const meta = chart.getDatasetMeta(i);
      meta.hidden = !meta.hidden;
      d.classList.toggle('off');
      chart.update();
    }};
    el.appendChild(d);
  }});
}}

const sharedScaleX = {{
  grid: {{ color: '#f0f0f0' }},
  ticks: {{ color: '#6e7681', font: {{ size: 11 }}, maxTicksLimit: 10 }},
  border: {{ color: '#e0e0e0' }}
}};

// ============================================================
// DATA (auto-generated — do not edit by hand)
// ============================================================
const decDates    = {json.dumps(d['decDates'])};
const top1        = {json.dumps(d['top1'])};
const top5        = {json.dumps(d['top5'])};
const top10       = {json.dumps(d['top10'])};
const ownersDaily = {json.dumps(d['ownersDaily'])};
const owners30d   = {json.dumps(d['owners30d'])};

const nkMonths  = {json.dumps(d['nkMonths'])};
const nk33      = {json.dumps(d['nk33'])};
const nkOwners  = {json.dumps(d['nkOwners'])};

const atkDates        = {json.dumps(d['atkDates'])};
const attack_cost_fil = {json.dumps(d['attack_cost_fil'])};
const tvl_fil         = {json.dumps(d['tvl_fil'])};
const roi_fil         = {json.dumps(d['roi_fil'])};

// ============================================================
// CHART 1: Reward share
// ============================================================
const shareSeries = {{
  "Top 1 owner_id": {{ data: top1, color: "#b0b0b0", width: 1.2 }},
  "Top 5 owner_ids": {{ data: top5, color: "#1a1a1a", width: 2.5 }},
  "Top 10 owner_ids": {{ data: top10, color: "#6366f1", width: 1.8 }},
}};

const shareDatasets = Object.entries(shareSeries).map(([name, s]) => ({{
  label: name, data: s.data, borderColor: s.color, backgroundColor: s.color + '15',
  borderWidth: s.width, pointRadius: 0, pointHoverRadius: 5, tension: 0.3, fill: false,
}}));

const chartShare = new Chart(document.getElementById('chartShare').getContext('2d'), {{
  type: 'line',
  data: {{ labels: decDates, datasets: shareDatasets }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#ffffffee', borderColor: '#e0e0e0', borderWidth: 1,
        titleColor: '#1a1a1a', bodyColor: '#333', padding: 14,
        callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + '%' }}
      }},
      annotation: {{
        annotations: {{
          threshold: {{
            type: 'line', yMin: 33, yMax: 33,
            borderColor: '#ef4444', borderWidth: 1.5, borderDash: [8, 4],
            label: {{ display: false }}
          }}
        }}
      }}
    }},
    scales: {{
      x: sharedScaleX,
      y: {{
        title: {{ display: true, text: 'Share of reward pool (%)', color: '#6e7681' }},
        grid: {{ color: '#f0f0f0' }},
        ticks: {{ color: '#6e7681', callback: v => v + '%' }},
        border: {{ color: '#e0e0e0' }}, min: 0, max: 60
      }}
    }}
  }}
}});

const legendShareEl = document.getElementById('legendShare');
makeLegend(legendShareEl, shareSeries, chartShare);
const thEl = document.createElement('div');
thEl.className = 'leg-item';
thEl.innerHTML = '<span class="dot" style="background:#ef4444;width:20px;height:2px;border-radius:0;"></span>33% threshold';
legendShareEl.appendChild(thEl);

// ============================================================
// CHART 2: Owner count
// ============================================================
const ownerSeries = {{
  "Daily owner_ids": {{ data: ownersDaily, color: "#c4b5fd", width: 1 }},
  "30d avg": {{ data: owners30d, color: "#7c3aed", width: 2 }},
}};

const ownerDatasets = Object.entries(ownerSeries).map(([name, s]) => ({{
  label: name, data: s.data, borderColor: s.color,
  backgroundColor: name === "Daily owner_ids" ? s.color + '30' : 'transparent',
  borderWidth: s.width, pointRadius: 0, pointHoverRadius: 4, tension: 0.3,
  fill: name === "Daily owner_ids",
}}));

const chartOwners = new Chart(document.getElementById('chartOwners').getContext('2d'), {{
  type: 'line',
  data: {{ labels: decDates, datasets: ownerDatasets }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#ffffffee', borderColor: '#e0e0e0', borderWidth: 1,
        titleColor: '#1a1a1a', bodyColor: '#333', padding: 14,
      }}
    }},
    scales: {{
      x: sharedScaleX,
      y: {{
        title: {{ display: true, text: 'Owner IDs', color: '#6e7681' }},
        grid: {{ color: '#f0f0f0' }}, ticks: {{ color: '#6e7681' }},
        border: {{ color: '#e0e0e0' }}, beginAtZero: true
      }}
    }}
  }}
}});

makeLegend(document.getElementById('legendOwners'), ownerSeries, chartOwners);

// ============================================================
// CHART 3: Nakamoto coefficient
// ============================================================
const nkSeries = {{
  "Nakamoto @33%": {{ data: nk33, color: "#f97316", width: 2.5 }},
  "Active owner_ids": {{ data: nkOwners, color: "#999999", width: 1.2 }},
}};

const nkDatasets = [
  {{
    label: "Nakamoto @33%", data: nk33, borderColor: "#f97316",
    backgroundColor: "#f9731620", borderWidth: 2.5, pointRadius: 0,
    pointHoverRadius: 5, tension: 0.3, fill: {{ target: 'origin', alpha: 0.08 }}, yAxisID: 'y',
  }},
  {{
    label: "Active owner_ids", data: nkOwners, borderColor: "#999999",
    backgroundColor: "#99999920", borderWidth: 1.2, borderDash: [6, 3],
    pointRadius: 0, pointHoverRadius: 5, tension: 0.3, fill: false, yAxisID: 'y1',
  }}
];

const chartNK = new Chart(document.getElementById('chartNK').getContext('2d'), {{
  type: 'line',
  data: {{ labels: nkMonths, datasets: nkDatasets }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#ffffffee', borderColor: '#e0e0e0', borderWidth: 1,
        titleColor: '#1a1a1a', bodyColor: '#333', padding: 14,
      }}
    }},
    scales: {{
      x: {{
        grid: {{ color: '#f0f0f0' }},
        ticks: {{ color: '#6e7681', font: {{ size: 11 }}, maxTicksLimit: 12 }},
        border: {{ color: '#e0e0e0' }}
      }},
      y: {{
        position: 'left',
        title: {{ display: true, text: 'Nakamoto Coefficient', color: '#6e7681' }},
        grid: {{ color: '#f0f0f0' }}, ticks: {{ color: '#6e7681' }},
        border: {{ color: '#e0e0e0' }}, beginAtZero: true
      }},
      y1: {{
        position: 'right',
        title: {{ display: true, text: 'Active Owner IDs', color: '#6e7681' }},
        grid: {{ display: false }}, ticks: {{ color: '#6e7681' }},
        border: {{ color: '#e0e0e0' }}, beginAtZero: true
      }}
    }}
  }}
}});

makeLegend(document.getElementById('legendNK'), nkSeries, chartNK);

// ============================================================
// CHART 4: Attack cost (FIL-denominated)
// ============================================================
const filSeries = {{
  "Attack cost (M FIL)": {{ data: attack_cost_fil, color: "#1a1a1a", width: 2.5 }},
  "Total locked FIL (M)": {{ data: tvl_fil, color: "#6366f1", width: 1.8 }},
}};

const filDatasets = Object.entries(filSeries).map(([name, s]) => ({{
  label: name, data: s.data, borderColor: s.color, backgroundColor: s.color + '15',
  borderWidth: s.width, pointRadius: 0, pointHoverRadius: 5, tension: 0.3, fill: false,
}}));

const chartFil = new Chart(document.getElementById('chartFil').getContext('2d'), {{
  type: 'line',
  data: {{ labels: atkDates, datasets: filDatasets }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#ffffffee', borderColor: '#e0e0e0', borderWidth: 1,
        titleColor: '#1a1a1a', bodyColor: '#333', padding: 14,
        callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + 'M' }}
      }}
    }},
    scales: {{
      x: sharedScaleX,
      y: {{
        title: {{ display: true, text: 'Million FIL', color: '#6e7681' }},
        grid: {{ color: '#f0f0f0' }},
        ticks: {{ color: '#6e7681' }},
        border: {{ color: '#e0e0e0' }}, min: 0, max: 200
      }}
    }}
  }}
}});

makeLegend(document.getElementById('legendFil'), filSeries, chartFil);

// ============================================================
// CHART 5: ROI
// ============================================================
const roiSeries = {{
  "FoF ROI (TVL/Cost)": {{ data: roi_fil, color: "#f97316", width: 2.5 }},
}};

const roiDatasets = Object.entries(roiSeries).map(([name, s]) => ({{
  label: name, data: s.data, borderColor: s.color, backgroundColor: s.color + '15',
  borderWidth: s.width, pointRadius: 0, pointHoverRadius: 5, tension: 0.3, fill: false,
}}));

const chartRoi = new Chart(document.getElementById('chartRoi').getContext('2d'), {{
  type: 'line',
  data: {{ labels: atkDates, datasets: roiDatasets }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#ffffffee', borderColor: '#e0e0e0', borderWidth: 1,
        titleColor: '#1a1a1a', bodyColor: '#333', padding: 14,
        callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + 'x' }}
      }}
    }},
    scales: {{
      x: sharedScaleX,
      y: {{
        title: {{ display: true, text: 'ROI (TVL / Attack Cost)', color: '#6e7681' }},
        grid: {{ color: '#f0f0f0' }},
        ticks: {{ color: '#6e7681', callback: v => v.toFixed(1) + 'x' }},
        border: {{ color: '#e0e0e0' }}, min: 0, max: 14
      }}
    }}
  }}
}});

makeLegend(document.getElementById('legendRoi'), roiSeries, chartRoi);
</script>
</body>
</html>"""


# ── Main ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Update Filecoin L1 Health dashboard")
    parser.add_argument("-o", "--output", default=None,
                        help="Output HTML path (default: filecoin_l1_health.html in same dir)")
    parser.add_argument("--attack-cost-csv", default=None,
                        help="Path to the 'Cost of 33%% attack' spreadsheet CSV for precise attack cost data")
    args = parser.parse_args()

    out_path = args.output or str(Path(__file__).parent / "filecoin_l1_health_dashboard.html")

    log("Starting dashboard update …")
    con = duckdb.connect()
    con.sql("SET threads = 4")

    data = fetch_all(con, attack_cost_csv=args.attack_cost_csv)
    con.close()

    log("Generating HTML …")
    html = build_html(data)
    Path(out_path).write_text(html, encoding="utf-8")
    log(f"Done → {out_path}  ({len(html)/1024:.0f} KB)")

if __name__ == "__main__":
    main()
