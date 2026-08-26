# Filecoin Metrics

A collection of Filecoin metrics dashboards. All dashboards are self-contained HTML files viewable in any browser.

## Dashboards

### [Filecoin Economy Flows](https://irenegia.github.io/filecoin-metrics/EconomyFlows/)

**Live dashboard:** <https://irenegia.github.io/filecoin-metrics/EconomyFlows/>

An ultrasound.money-style live view of the FIL token economy, supply lens only:

- **Supply** — circulating FIL since mainnet launch, plus the live accounting line (mined + vested − locked − burned = circulating)
- **Issuance and burn** — FIL minted and destroyed per day
- **Issuance offset** — burn as a share of issuance (1.0x = supply-neutral), trailing 365d/30d and monthly
- **Burn leaderboard** — burn ranked by protocol mechanism (7d/30d/all time), plus the one app with its own attribution (Filecoin Onchain Cloud)

Data is sourced from the [Filecoin Data Portal](https://filecoindataportal.xyz) `daily_network_metrics.parquet` dataset. A GitHub Actions workflow runs `EconomyFlows/build_seed.py` daily at 07:00 UTC and commits the refreshed `EconomyFlows/seed.json`; the page reads only that file plus a live CoinGecko price. One exception: the burn-by-mechanism split comes from the [Starboard Network Health API](https://networkhealth.starboard.ventures), because the portal's daily metrics do not itemize burn by mechanism reliably. The FOC row comes from the FOC contract index (FilecoinPay + PDPVerifier events), dated on the page. To trigger manually: Actions > Update Economy Flows Data > Run workflow. To run locally: `pip install duckdb && python EconomyFlows/build_seed.py`

### [L1 Health Metrics](https://irenegia.github.io/filecoin-metrics/filecoin_l1_health_dashboard.html)

**Live dashboard:** <https://irenegia.github.io/filecoin-metrics/filecoin_l1_health_dashboard.html>

Companion charts for the [L1 Health Metrics Framework](https://docs.google.com/document/d/1zuFRFTgjRFMagiH8MhMJwmL2FZBMhKP7lAv0ooUb6Jc) report.

- **Decentralization** — reward concentration (top 1/5/10 owner share), active owner count, Nakamoto Coefficient @33%
- **Consensus Security** — estimated 33% attack cost (FIL), total value locked, attack ROI

Data sourced from [Filecoin Data Portal](https://filecoindataportal.xyz) parquet datasets. A GitHub Actions workflow runs `update_dashboard.py` daily at 06:00 UTC to keep charts current. To trigger manually: Actions > Update L1 Health Dashboard > Run workflow.

To run locally: `pip install duckdb && python update_dashboard.py`
