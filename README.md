# ⚡ European Gas Intelligence Dashboard

A Python-powered dashboard that fetches live European gas data and generates 
a beautiful interactive HTML report you can view in your browser.

## Data Sources
- **GIE AGSI** — Gas storage levels (fill %, injection, withdrawal) for every EU country
- **GIE ALSI** — LNG terminal inventory, send-out rates, and capacity utilization
- **ENTSOG Transparency Platform** — Cross-border pipeline physical flows

## Quick Start (Windows)

### 1. Install the one dependency

Open **Command Prompt** (search "cmd" in Start menu) and run:

```
pip install requests
```

### 2. Run the dashboard

Navigate to the folder where you saved these files:

```
cd C:\path\to\gas-dashboard
python gas_dashboard.py
```

That's it. The script will:
1. Fetch live data from all three APIs (takes ~2-3 minutes)
2. Generate `gas_dashboard.html` 
3. Automatically open it in your default browser

### 3. Refresh anytime

Just run `python gas_dashboard.py` again to pull the latest data. The HTML file 
gets overwritten with fresh numbers each time.

## What's in the Dashboard

| Tab | What it shows |
|-----|---------------|
| ⛽ **Storage Overview** | EU aggregate fill level with gauge, full country breakdown table with fill bars, injection/withdrawal, trends |
| 📈 **Seasonality** | Current year vs 5 prior years fill trajectories (EU, Germany, Italy) with interactive Chart.js chart |
| 🚢 **LNG Terminals** | EU LNG inventory, send-out, capacity utilization by country |
| 🔀 **Pipeline Flows** | ENTSOG physical flow data across European interconnection points |

## Notes

- AGSI/ALSI data is updated daily at **19:30 CET** (second update at 23:00)
- ENTSOG data has a 60-second API timeout; large queries may be truncated
- The 90% storage target line references **EU Regulation 2022/1032**
- Your AGSI API key is embedded in the script — keep it private
- All data is fetched server-side by Python, so no CORS issues

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `ModuleNotFoundError: No module named 'requests'` | Run `pip install requests` |
| `python` not recognized | Try `python3 gas_dashboard.py` or `py gas_dashboard.py` |
| AGSI returns errors | Check your API key hasn't expired at https://agsi.gie.eu/account |
| ENTSOG timeout | Normal for large queries — the script retries automatically |

## Future Enhancements
- Add specific ENTSOG pointDirection queries for key corridors
- Country deep-dive tab with operator/facility breakdown
- Historical flow charts for major pipelines
- Automated daily scheduling via Windows Task Scheduler
