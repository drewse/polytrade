"""
Daily research report (Phase 19).

Pure Markdown generator over a prepared context dict (the engine assembles the
context from the DB). PAPER ONLY — a research summary, not a trade instruction.
"""
from __future__ import annotations


def _usd(x):
    return f"${x:,.2f}" if x is not None else "—"


def _line(items, fmt):
    return "\n".join(fmt(i) for i in items) if items else "_none_"


def generate(ctx: dict) -> str:
    best = ctx.get("best_strategy") or {}
    risk = ctx.get("open_risk") or {}
    md = []
    md.append(f"# TOP 20 Research Report — {ctx.get('date', '')}")
    md.append("\n> 📝 PAPER TRADING ONLY — simulated results, no real orders.\n")

    md.append("## Best strategy today")
    md.append(f"- **{best.get('name', '—')}** — score {best.get('score', 0)}/100, "
              f"Sharpe {best.get('sharpe', 0)}, return {best.get('total_return', 0)*100:.1f}%, "
              f"{best.get('closed_positions', 0)} closed trades")
    if ctx.get("best_reason"):
        md.append(f"- _{ctx['best_reason']}_")

    md.append("\n## Movers")
    imp = ctx.get("biggest_improvement")
    reg = ctx.get("largest_regression")
    md.append(f"- 📈 Biggest improvement: **{imp['name']}** ({imp['delta']:+.2f} equity)" if imp else "- 📈 Biggest improvement: _n/a_")
    md.append(f"- 📉 Largest regression: **{reg['name']}** ({reg['delta']:+.2f} equity)" if reg else "- 📉 Largest regression: _n/a_")
    ld = ctx.get("largest_drawdown")
    md.append(f"- 🌊 Largest drawdown: **{ld['name']}** ({ld['dd']*100:.1f}%)" if ld else "- 🌊 Largest drawdown: _n/a_")

    md.append("\n## Wallets")
    md.append("New / top copy wallets:")
    md.append(_line(ctx.get("new_top_wallets", []),
                    lambda w: f"- `{w['address'][:14]}…` copyability {w.get('copyability', 0)}, {w.get('classification', '')}"))

    md.append("\n## Category performance")
    md.append(_line(ctx.get("category_performance", []),
                    lambda c: f"- **{c['category']}**: {c['win_rate']*100:.0f}% win, "
                              f"avg edge {c['avg_edge']*100:.1f}%, efficiency {c['market_efficiency']:.2f}"))

    md.append("\n## Signals")
    mp = ctx.get("most_profitable_signal")
    ws = ctx.get("worst_signal")
    md.append(f"- 🥇 Most profitable: {mp['market'][:50]} ({_usd(mp['pnl'])})" if mp else "- 🥇 Most profitable: _n/a_")
    md.append(f"- 🥶 Worst: {ws['market'][:50]} ({_usd(ws['pnl'])})" if ws else "- 🥶 Worst: _n/a_")

    md.append("\n## Recent exits")
    md.append(_line(ctx.get("recent_exits", []),
                    lambda e: f"- {e['market'][:40]} — {e['reason']} ({_usd(e['pnl'])})"))

    md.append("\n## Parameter / lifecycle changes")
    md.append(_line(ctx.get("parameter_changes", []), lambda p: f"- {p}"))

    md.append("\n## Open risk")
    md.append(f"- Open exposure: {_usd(risk.get('open_exposure'))} "
              f"({risk.get('capital_utilization', 0)*100:.1f}% of capital), "
              f"{risk.get('open_positions', 0)} open positions")

    return "\n".join(md) + "\n"
