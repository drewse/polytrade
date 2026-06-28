"""Top-20 production wallet AUDIT — read-only visibility.

Exposes, for each wallet the live executor is currently allowed to copy, the
internal ranking stats, OUR rolling-window performance, the PUBLIC Polymarket
lifetime/rolling stats, a ranking-score breakdown, and warning flags (e.g. deeply
negative public all-time P/L, low internal coverage, internal-vs-public conflict,
likely market-maker/whale).

STRICTLY READ-ONLY: it imports live_ranking and only READS it (rank_wallets,
eligible_addresses, production_score, _cfg). It changes NO ranking, eligibility,
execution, sizing, bankroll, discovery, or trading state. Public stats are NEVER
used to alter ranking here — visibility only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import live_ranking, positions as positions_mod, public_profile, ranking_hardening
from .models import DiscoverySource, Market, Trade, Wallet, WalletStat

WHALE_VOLUME = 1_000_000.0          # public lifetime volume above this => likely MM/whale
DEEP_LOSS = -1_000.0                # public all-time P/L below this => "lifetime loser"
PROFILE_BASE = "https://polymarket.com/profile/"


def _profile_url(address: str) -> str:
    return f"{PROFILE_BASE}{address}"


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _settled_positions(db: Session, wallet: Wallet):
    trades = db.scalars(select(Trade).where(Trade.wallet_id == wallet.id)).all()
    mids = {t.market_id for t in trades}
    markets = {m.id: m for m in db.scalars(select(Market).where(Market.id.in_(mids))).all()} if mids else {}
    return trades, positions_mod.settled_positions(trades, markets)


def _window_stats(positions, now: datetime, days: int) -> dict:
    cut = now - timedelta(days=days)
    sub = [p for p in positions if p.timestamp and p.timestamp >= cut]
    n = len(sub)
    if not n:
        return {"trades": 0, "pnl": 0.0, "roi": 0.0, "pf": 0.0}
    pnls = [p.realized_pnl for p in sub]
    invested = sum(abs(p.size) for p in sub) or 1.0
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    return {"trades": n, "pnl": round(sum(pnls), 2), "roi": round(sum(pnls) / invested, 4),
            "pf": round(gw / gl, 3) if gl > 0 else round(gw, 3)}


def _rolling(positions, now: datetime) -> dict:
    return {f"{d}d": _window_stats(positions, now, d) for d in (1, 7, 30, 90)}


def _internal_stats(db: Session, wallet: Wallet, stat: WalletStat | None, trades) -> dict:
    times = [t.timestamp for t in trades if t.timestamp]
    volume = round(sum(t.size for t in trades), 2)
    sources = db.scalars(select(DiscoverySource).where(
        DiscoverySource.wallet_address == wallet.address)).all() if wallet else []
    src_names = sorted({s.discovery_source for s in sources})
    return {
        "roi": round(stat.realized_roi or 0, 4) if stat else None,
        "profit_factor": round(stat.profit_factor or 0, 4) if stat else None,
        "win_rate": round(stat.win_rate or 0, 4) if stat else None,
        "num_settled": int(stat.num_settled or 0) if stat else 0,
        "num_trades": int(stat.num_trades or 0) if stat else len(trades),
        "realized_pnl": round(stat.realized_pnl or 0, 2) if stat else None,
        "volume": volume,
        "avg_trade_size": round(stat.avg_trade_size or 0, 2) if stat else None,
        "max_drawdown": round(stat.max_drawdown or 0, 4) if stat else None,
        "last_active": wallet.last_active.isoformat() if (wallet and wallet.last_active) else None,
        "first_trade_seen": min(times).isoformat() if times else None,
        "last_trade_seen": max(times).isoformat() if times else None,
        "partial_history": bool(stat.partial_history) if stat else None,
        "classification": stat.classification if stat else None,
        "data_source_count": len(src_names),
        "discovery_sources": src_names,
    }


def _coverage(internal: dict, public: dict | None) -> dict:
    """Estimate how much of the wallet's real activity our internal data captured."""
    cov = {"volume_ratio": None, "predictions_ratio": None, "level": "unknown"}
    if public:
        pv = public.get("volume_all")
        if pv and pv > 0 and internal.get("volume") is not None:
            cov["volume_ratio"] = round(internal["volume"] / pv, 4)
        pp = public.get("predictions")
        if pp and pp > 0:
            cov["predictions_ratio"] = round((internal.get("num_settled") or 0) / pp, 4)
    ratios = [r for r in (cov["volume_ratio"], cov["predictions_ratio"]) if r is not None]
    best = max(ratios) if ratios else None
    if internal.get("partial_history") or (best is not None and best < 0.10):
        cov["level"] = "low"
    elif best is not None and best >= 0.5:
        cov["level"] = "high"
    elif best is not None:
        cov["level"] = "medium"
    return cov


def _warnings(row: dict, internal: dict, rolling: dict, public: dict | None, coverage: dict) -> list[dict]:
    w = []

    def add(code, severity, msg):
        w.append({"code": code, "severity": severity, "message": msg})

    pnl_all = public.get("pnl_all") if public else None
    if pnl_all is not None and pnl_all < DEEP_LOSS:
        add("public_lifetime_loss", "high", f"Public all-time P/L is {pnl_all:,.0f} (deeply negative).")
    if coverage["level"] == "low":
        add("low_coverage", "high", "Internal data captures only a small slice of this wallet's real activity.")
    if internal.get("partial_history"):
        add("partial_history", "medium", "Internal stats are flagged partial_history (recent-window only).")
    # internal positive but public lifetime negative
    if pnl_all is not None and pnl_all < 0 and (internal.get("roi") or 0) > 0 and (internal.get("profit_factor") or 0) > 1:
        add("internal_public_conflict", "high",
            f"Internal ROI {internal['roi']*100:.0f}% / PF {internal['profit_factor']:.2f} is profitable, "
            f"but public all-time P/L is {pnl_all:,.0f}.")
    # recent good (our 30d) but lifetime bad
    if pnl_all is not None and pnl_all < 0 and (rolling.get("30d", {}).get("pnl") or 0) > 0:
        add("recent_good_lifetime_bad", "medium", "Recent (30d) internal P/L positive but public lifetime is negative.")
    # whale / market maker
    vol = public.get("volume_all") if public else None
    posval = public.get("position_value") if public else None
    bigpos = public.get("largest_position_size") if public else None
    if (vol and vol > WHALE_VOLUME) or (posval and posval > 100_000) or (bigpos and bigpos > 50_000):
        add("likely_market_maker_whale", "high",
            f"Whale/MM signature: public volume {vol:,.0f}" if vol else "Whale/MM signature: very large positions.")
    cfg = live_ranking._cfg()
    if (internal.get("num_settled") or 0) < cfg["min_settled"] * 2:
        add("small_sample", "medium", f"Backfilled settled sample is small ({internal.get('num_settled')}).")
    if (internal.get("max_drawdown") or 0) > 0.4:
        add("high_drawdown", "medium", f"High internal max drawdown ({internal['max_drawdown']*100:.0f}%).")
    if internal.get("last_active"):
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(internal["last_active"])).days
            if age > 14:
                add("stale", "low", f"Last active {age}d ago.")
        except (ValueError, TypeError):
            pass
    # single-market concentration (public): top position dominates portfolio value
    if public and posval and bigpos and posval > 0 and bigpos / posval > 0.5:
        add("single_market_concentration", "medium", "One position dominates the public portfolio value.")
    return w


def _score_breakdown(row: dict) -> dict:
    """Deterministic reconstruction of the production_rank_score components
    (40% reputation + 30% PF + 20% ROI + 10% recency)."""
    rep = row.get("reputation_score") or 0.0
    pf = row.get("profit_factor") or 0.0
    roi = row.get("roi") or 0.0
    rec = row.get("recency") or 0.0
    rep_n = live_ranking._clip((rep) / 100.0)
    pf_n = live_ranking._clip((pf - 1.0) / 2.0)
    roi_n = live_ranking._clip(roi / 0.5)
    rec_n = live_ranking._clip(rec)
    wts = live_ranking.WEIGHTS
    comps = {
        "reputation": {"weight": wts["reputation"], "normalized": round(rep_n, 4), "points": round(100 * wts["reputation"] * rep_n, 2)},
        "profit_factor": {"weight": wts["profit_factor"], "normalized": round(pf_n, 4), "points": round(100 * wts["profit_factor"] * pf_n, 2)},
        "roi": {"weight": wts["roi"], "normalized": round(roi_n, 4), "points": round(100 * wts["roi"] * roi_n, 2)},
        "recency": {"weight": wts["recency"], "normalized": round(rec_n, 4), "points": round(100 * wts["recency"] * rec_n, 2)},
    }
    return {"components": comps, "total": round(sum(c["points"] for c in comps.values()), 2)}


def _strengths_weaknesses(internal: dict, rolling: dict, public: dict | None) -> tuple[list, list]:
    strengths, weaknesses = [], []
    if (internal.get("roi") or 0) > 0.1:
        strengths.append(f"Internal ROI {internal['roi']*100:.0f}%")
    if (internal.get("profit_factor") or 0) > 1.5:
        strengths.append(f"Internal PF {internal['profit_factor']:.2f}")
    if (rolling.get("30d", {}).get("pnl") or 0) > 0:
        strengths.append("Positive recent (30d) internal P/L")
    if public and public.get("pnl_all") is not None and public["pnl_all"] < 0:
        weaknesses.append(f"Public all-time P/L {public['pnl_all']:,.0f}")
    if internal.get("partial_history"):
        weaknesses.append("Partial internal history")
    if (internal.get("max_drawdown") or 0) > 0.4:
        weaknesses.append(f"Drawdown {internal['max_drawdown']*100:.0f}%")
    return strengths, weaknesses


def top_wallets_audit(db: Session, *, refresh_public: bool = False, force_refresh: bool = False) -> dict:
    """Audit the current production Top-N (the wallets the executor may copy).
    READ-ONLY — never changes ranking/eligibility/execution."""
    cfg = live_ranking._cfg()
    hcfg = ranking_hardening.config()
    ranked = live_ranking.rank_wallets(db, include_failed=True)       # READ ONLY
    eligible_set = live_ranking.eligible_addresses(db)               # READ ONLY (current copied top-N)
    # audit the BASE-eligible top-N (the candidate pool) in BOTH modes so the
    # dashboard always shows the full current set + what hardening would remove.
    eligible_rows = [r for r in ranked if r.get("base_eligible")][: cfg["top_n"]]
    addrs = [r["address"] for r in eligible_rows]
    refresh_info = None
    if refresh_public:
        refresh_info = public_profile.refresh_profiles(db, addrs, force=force_refresh)

    now = datetime.utcnow()
    wallets = {w.address.lower(): w for w in db.scalars(select(Wallet)).all()}
    stats = {s.wallet_id: s for s in db.scalars(select(WalletStat)).all()}
    out = []
    for i, r in enumerate(eligible_rows):
        w = wallets.get(r["address"].lower())
        stat = stats.get(w.id) if w else None
        trades, positions = _settled_positions(db, w) if w else ([], [])
        internal = _internal_stats(db, w, stat, trades)
        rolling = _rolling(positions, now)
        pub = public_profile.as_dict(public_profile.get_cached(db, r["address"]))
        coverage = _coverage(internal, pub)
        internal["backfill_coverage"] = coverage
        warnings = _warnings(r, internal, rolling, pub, coverage)
        strengths, weaknesses = _strengths_weaknesses(internal, rolling, pub)
        # hardened verdict computed with the FRESH public stats just fetched
        hv = ranking_hardening.evaluate(
            partial_history=internal.get("partial_history"), public=pub,
            internal_volume=internal.get("volume"), internal_settled=internal.get("num_settled"), cfg=hcfg)
        out.append({
            "rank": i + 1,
            "address": r["address"],
            "profile_url": _profile_url(r["address"]),
            "display_name": (pub or {}).get("display_name") or (pub or {}).get("pseudonym"),
            "production_rank_score": r["production_rank_score"],
            "eligible": r["eligible"],
            "filter_reason": r["filter_reason"],
            "copied": r["address"] in eligible_set,
            "reputation_score": r.get("reputation_score"),
            "classification": r.get("classification"),
            "internal": internal,
            "rolling": rolling,
            "public": pub,
            "score_breakdown": _score_breakdown(r),
            "strengths": strengths,
            "weaknesses": weaknesses,
            "warnings": warnings,
            "warning_count": len(warnings),
            # hardened-rules verdict (display; enforcement only when audit_only=false)
            "hardened_pass": hv["pass"],
            "would_be_excluded": (not hv["pass"]),
            "hardened_exclusions": hv["exclusions"],
            "hardened_unknowns": hv["unknowns"],
        })
    # --- hardening summary (what the hardened gates WOULD remove) ---
    def _excluded_by(code):
        return [w["address"] for w in out if any(e["code"] == code for e in w["hardened_exclusions"])]

    would_pass = [w for w in out if w["hardened_pass"]]
    removed = [w["address"] for w in out if w["copied"] and not w["hardened_pass"]]
    hardening = {
        "audit_only": hcfg["audit_only"],
        "mode": "AUDIT-ONLY (no eligibility change)" if hcfg["audit_only"] else "ENFORCED",
        "thresholds": {
            "min_public_all_time_pnl": hcfg["min_public_pnl"],
            "require_public_stats": hcfg["require_public_stats"],
            "allow_partial_history": hcfg["allow_partial_history"],
            "min_coverage_ratio": hcfg["min_coverage_ratio"],
            "max_public_volume": hcfg["max_public_volume"],
            "max_position_size": hcfg["max_position_size"],
        },
        "current_eligible_count": len(out),
        "would_pass_hardened_count": len(would_pass),
        "would_pass_addresses": [w["address"] for w in would_pass],
        "excluded_by_public_pnl": _excluded_by("public_pnl_below_min"),
        "excluded_by_partial_history": _excluded_by("partial_history"),
        "excluded_by_coverage": _excluded_by("low_coverage"),
        "excluded_by_whale": _excluded_by("whale_volume"),
        "excluded_by_missing_public": _excluded_by("missing_public_stats"),
        "excluded_by_large_position": _excluded_by("large_position"),
        "currently_copied_would_be_removed": removed,
    }
    return {
        "generated_at": now.isoformat(),
        "top_n": cfg["top_n"],
        "eligible_count": len([r for r in ranked if r["eligible"]]),
        "audited": len(out),
        "filters": {"min_roi": cfg["min_roi"], "min_pf": cfg["min_pf"],
                    "min_settled": cfg["min_settled"], "active_days": cfg["active_days"]},
        "weights": live_ranking.WEIGHTS,
        "public_refresh": refresh_info,
        "hardening": hardening,
        "wallets": out,
        "safety": "READ-ONLY audit — public stats are never used to alter ranking/eligibility/execution",
    }


def wallet_audit_detail(db: Session, address: str) -> dict | None:
    cfg = live_ranking._cfg()
    ranked = live_ranking.rank_wallets(db, include_failed=True)
    row = next((r for r in ranked if r["address"].lower() == address.lower()), None)
    if row is None:
        return None
    w = db.scalar(select(Wallet).where(func.lower(Wallet.address) == address.lower()))
    stat = db.scalar(select(WalletStat).where(WalletStat.wallet_id == w.id)) if w else None
    trades, positions = _settled_positions(db, w) if w else ([], [])
    now = datetime.utcnow()
    internal = _internal_stats(db, w, stat, trades)
    rolling = _rolling(positions, now)
    pub = public_profile.as_dict(public_profile.get_cached(db, row["address"]))
    coverage = _coverage(internal, pub)
    internal["backfill_coverage"] = coverage
    # largest internal wins / losses (reconstructed settled positions)
    by_pnl = sorted(positions, key=lambda p: p.realized_pnl)
    largest_losses = [{"market": p.market, "pnl": round(p.realized_pnl, 2), "size": round(p.size, 2)} for p in by_pnl[:5]]
    largest_wins = [{"market": p.market, "pnl": round(p.realized_pnl, 2), "size": round(p.size, 2)} for p in by_pnl[-5:][::-1]]
    # internal market concentration
    by_market: dict[str, float] = {}
    for t in trades:
        by_market[t.market_id] = by_market.get(t.market_id, 0.0) + t.size
    top_markets = sorted(by_market.items(), key=lambda kv: -kv[1])[:5]
    total_vol = sum(by_market.values()) or 1.0
    concentration = round(top_markets[0][1] / total_vol, 3) if top_markets else 0.0
    # eligibility rule pass/fail breakdown (recomputed, read-only)
    rules = _eligibility_rules(stat, w, now, cfg)
    strengths, weaknesses = _strengths_weaknesses(internal, rolling, pub)
    return {
        "address": row["address"], "profile_url": _profile_url(row["address"]),
        "display_name": (pub or {}).get("display_name") or (pub or {}).get("pseudonym"),
        "eligible": row["eligible"], "filter_reason": row["filter_reason"],
        "production_rank_score": row["production_rank_score"], "reputation_score": row.get("reputation_score"),
        "internal": internal, "rolling": rolling, "public": pub,
        "score_breakdown": _score_breakdown(row),
        "eligibility_rules": rules,
        "largest_wins": largest_wins, "largest_losses": largest_losses,
        "market_concentration": {"top_market_share": concentration,
                                 "top_markets": [{"market_id": m, "volume": round(v, 2)} for m, v in top_markets]},
        "warnings": _warnings(row, internal, rolling, pub, coverage),
        "strengths": strengths, "weaknesses": weaknesses,
        "copy_rationale": (f"Selected: production score {row['production_rank_score']} "
                           f"(rep {row.get('reputation_score')}, PF {internal.get('profit_factor')}, "
                           f"ROI {internal.get('roi')}). " + (row["filter_reason"])),
    }


def _eligibility_rules(stat, wallet, now, cfg) -> list[dict]:
    if stat is None:
        return [{"rule": "has_stats", "pass": False, "detail": "no internal stats"}]
    last = wallet.last_active if wallet else None
    age = (now - last).days if last else None
    return [
        {"rule": f"ROI > {cfg['min_roi']*100:.0f}%", "pass": (stat.realized_roi or 0) > cfg["min_roi"],
         "detail": f"{(stat.realized_roi or 0)*100:.1f}%"},
        {"rule": f"Profit factor > {cfg['min_pf']:.2f}", "pass": (stat.profit_factor or 0) > cfg["min_pf"],
         "detail": f"{stat.profit_factor or 0:.2f}"},
        {"rule": f"Settled >= {cfg['min_settled']}", "pass": (stat.num_settled or 0) >= cfg["min_settled"],
         "detail": f"{stat.num_settled or 0}"},
        {"rule": f"Active within {cfg['active_days']}d", "pass": (age is None or age <= cfg["active_days"]),
         "detail": (f"{age}d ago" if age is not None else "unknown")},
        {"rule": "Full history (optional)", "pass": (not cfg["require_full_history"]) or (not stat.partial_history),
         "detail": ("partial" if stat.partial_history else "full")},
    ]
