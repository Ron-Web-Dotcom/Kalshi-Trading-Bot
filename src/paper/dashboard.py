"""Paper trading HTML dashboard generator."""

import html
from datetime import datetime
from typing import Dict, List


def generate_html(signals: List[Dict], stats: Dict) -> str:
    total_pnl = stats.get("total_pnl", 0)
    win_rate = stats.get("win_rate", 0)
    total = stats.get("total", 0)
    pnl_color = "#00cc66" if total_pnl >= 0 else "#ff4444"

    rows = ""
    for s in signals[:100]:
        outcome = s.get("outcome")
        outcome_str = f"${outcome:+.2f}" if outcome is not None else "Pending"
        outcome_color = "#00cc66" if (outcome or 0) > 0 else "#ff4444" if (outcome or 0) < 0 else "#888"
        rows += f"""
        <tr>
          <td>{html.escape(s.get('ticker',''))}</td>
          <td>{html.escape(s.get('action',''))} {html.escape(s.get('side',''))}</td>
          <td>{s.get('price',0):.0f}¢</td>
          <td>{s.get('ai_confidence',0):.0f}%</td>
          <td style="color:{outcome_color}">{outcome_str}</td>
          <td>{html.escape(s.get('created_at','')[:19])}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Kalshi Paper Trading Dashboard</title>
<style>
  body {{ font-family: monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }}
  h1 {{ color: #58a6ff; }}
  .stats {{ display: flex; gap: 24px; margin-bottom: 24px; }}
  .stat {{ background: #161b22; border: 1px solid #30363d; padding: 16px; border-radius: 8px; min-width: 120px; }}
  .stat-value {{ font-size: 1.8em; font-weight: bold; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #161b22; padding: 8px; text-align: left; }}
  td {{ padding: 8px; border-bottom: 1px solid #21262d; }}
  tr:hover {{ background: #161b22; }}
</style></head><body>
<h1>📊 Kalshi Paper Trading Dashboard</h1>
<p style="color:#888">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="stats">
  <div class="stat"><div>Total PnL</div><div class="stat-value" style="color:{pnl_color}">${total_pnl:+.2f}</div></div>
  <div class="stat"><div>Win Rate</div><div class="stat-value">{win_rate:.1f}%</div></div>
  <div class="stat"><div>Signals</div><div class="stat-value">{total}</div></div>
</div>
<table>
<tr><th>Ticker</th><th>Action</th><th>Price</th><th>AI Conf</th><th>Outcome</th><th>Time</th></tr>
{rows}
</table></body></html>"""
