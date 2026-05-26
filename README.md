# Filecoin Metrics

A collection of Filecoin metrics dashboards. All dashboards are self-contained HTML files viewable in any browser.

## Dashboards

### [L1 Health Metrics](https://irenegia.github.io/filecoin-metrics/filecoin_l1_health_dashboard.html)

Companion charts for the [L1 Health Metrics Framework](https://docs.google.com/document/d/1zuFRFTgjRFMagiH8MhMJwmL2FZBMhKP7lAv0ooUb6Jc) report.

- **Decentralization** — reward concentration (top 1/5/10 owner share), active owner count, Nakamoto Coefficient @33%
- **Consensus Security** — estimated 33% attack cost (FIL), total value locked, attack ROI

Data sourced from [Filecoin Data Portal](https://filecoindataportal.xyz) parquet datasets. A GitHub Actions workflow runs `update_dashboard.py` daily at 06:00 UTC to keep charts current. To trigger manually: Actions > Update L1 Health Dashboard > Run workflow.

To run locally: `pip install duckdb && python update_dashboard.py`
