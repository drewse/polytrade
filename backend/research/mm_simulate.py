"""Phase 0a — two-sided maker simulator for BTC/ETH/SOL/XRP 5-minute markets.

Reads the harvested datasets (mm_dataset_<asset>.json: resolved markets + trade tapes) and
simulates a SMALL, NON-HFT two-sided maker:
  * Quote a BUY on Up at mid-h and a BUY on Down at mid+h (== Up ask), re-quoted every L s.
  * Maker-only: a quote fills only when the tape trades THROUGH it (worst-case queue — a slow
    account sits behind the HFT bots and only fills when price crosses past the level). 'mid'
    and 'best' queue modes are optimistic controls.
  * Adverse selection is NOT assumed — it emerges: an Up bid fills on a downtick (price heading
    toward Down winning), so unmatched Up inventory tends to lose at resolution.
  * Matched Up+Down shares -> merge into $1 (locked); unmatched held to resolution.
  * Economics: maker fee 0; rebate = 20% of the crypto taker fee (size*0.07*p*(1-p)) the
    counterparty pays on each of our fills; optional flat LP reward/market (default 0).
  * Inventory balancing: optional net-inventory cap (stop adding to the heavy side).

Pure offline research. No live trades, no executor.
"""
import json, sys, math
from collections import defaultdict

SP = sys.argv[1] if len(sys.argv) > 1 else "."
ASSETS = ["btc", "eth", "sol", "xrp"]
SHARES = 5.0                      # our order size (venue min; small account)
REBATE_RATE = 0.20               # maker rebate = 20% of crypto taker fee
TAKER_FEE = 0.07                 # crypto feeRate
CONTRA_TAKER_FRAC = 1.0          # fraction of our fills that face a fee-paying taker (worst=1.0)


def _mean(xs): return sum(xs) / len(xs) if xs else 0.0
def _std(xs):
    if len(xs) < 2: return 0.0
    m = _mean(xs); return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def mid_at(prints, t):
    """last trade price at or before t (price path = step function of prints)."""
    m = prints[0][1]
    for to, p, sz, sd in prints:
        if to <= t: m = p
        else: break
    return m


def crossed(prints, t0, t1, level, direction, mode, our_usd, cancel_h=30):
    """Did our resting quote at `level` fill in (t0,t1]? direction='down' (Up bid, fills when
    price trades <= level) or 'up' (Up ask, fills when price trades >= level).
    mode: best=touch, mid=cumulative size clears queue_ahead, worst=strictly through,
          cancel=latency-advantaged CEILING: keep a (worst-queue) fill ONLY if price reverts
                 back across the level within cancel_h s (benign); a sustained adverse move is
                 assumed cancelled before it fills us (perfect-cancellation upper bound)."""
    cum = 0.0
    qahead = 25.0                # ~$25 resting ahead of us (median book), for 'mid'
    for i, (to, p, sz, sd) in enumerate(prints):
        if to <= t0 or to > t1: continue
        hit = (p <= level) if direction == "down" else (p >= level)
        thru = (p < level) if direction == "down" else (p > level)
        if mode == "best" and hit:
            return True
        if mode == "worst" and thru:
            return True
        if mode == "cancel" and thru:
            revert = any((pp >= level if direction == "down" else pp <= level)
                         for tt, pp, _, _ in prints if to < tt <= to + cancel_h)
            if revert:
                return True            # benign fill we'd have kept; else assume cancelled
        if mode == "mid" and hit:
            cum += p * sz
            if cum >= qahead:
                return True
    return False


def simulate_market(mkt, *, h=0.005, L=30, mode="worst", phase="full", inv_cap=None):
    prints = mkt["prints"]
    won_up = 1.0 if mkt["won_up"] else 0.0
    lo, hi = {"full": (0, 300), "early": (0, 180), "late": (180, 300), "avoid_last60": (0, 240)}[phase]
    up_sh = dn_sh = 0.0
    cost = 0.0
    rebate = 0.0
    quotes = 0
    fills = 0
    t = lo
    while t < hi:
        m = mid_at(prints, t)
        if m <= 0.02 or m >= 0.98:
            t += L; continue
        net = up_sh - dn_sh
        do_up = not (inv_cap is not None and net >= inv_cap)        # skip side if too long it
        do_dn = not (inv_cap is not None and -net >= inv_cap)
        b_up = round(m - h, 4)
        a_up = round(m + h, 4)                                       # Down bid == Up ask
        if do_up:
            quotes += 1
            if b_up > 0.01 and crossed(prints, t, t + L, b_up, "down", mode, SHARES * b_up):
                up_sh += SHARES; cost += SHARES * b_up; fills += 1
                rebate += REBATE_RATE * SHARES * TAKER_FEE * b_up * (1 - b_up) * CONTRA_TAKER_FRAC
        if do_dn:
            quotes += 1
            dn_price = round(1 - a_up, 4)
            if a_up < 0.99 and crossed(prints, t, t + L, a_up, "up", mode, SHARES * dn_price):
                dn_sh += SHARES; cost += SHARES * dn_price; fills += 1
                rebate += REBATE_RATE * SHARES * TAKER_FEE * dn_price * (1 - dn_price) * CONTRA_TAKER_FRAC
        t += L
    payout = up_sh * won_up + dn_sh * (1 - won_up)
    pairs = min(up_sh, dn_sh)
    return {"up_sh": up_sh, "dn_sh": dn_sh, "cost": cost, "payout": payout,
            "trading_pnl": payout - cost, "rebate": rebate, "pairs": pairs,
            "quotes": quotes, "fills": fills,
            "both_filled": up_sh > 0 and dn_sh > 0, "any_fill": (up_sh + dn_sh) > 0,
            "peak_capital": cost, "won_up": won_up}


def run_asset(markets, **kw):
    if not markets:
        return {"n_markets": 0}
    res = [simulate_market(m, **kw) for m in markets]
    res = [r for r in res if r["quotes"] > 0]
    n = len(res)
    pnls = [r["trading_pnl"] + r["rebate"] for r in res]            # net per market
    quotes = sum(r["quotes"] for r in res)
    fills = sum(r["fills"] for r in res)
    traded = [r for r in res if r["any_fill"]]
    both = [r for r in res if r["both_filled"]]
    total_cost = sum(r["cost"] for r in res)
    merge_pairs = sum(r["pairs"] for r in res)
    trading = sum(r["trading_pnl"] for r in res)
    rebate = sum(r["rebate"] for r in res)
    net = trading + rebate
    # drawdown over the market sequence (already most-recent-first; reverse to chrono)
    cum = 0.0; peak = 0.0; dd = 0.0
    for r in reversed(res):
        cum += r["trading_pnl"] + r["rebate"]; peak = max(peak, cum); dd = min(dd, cum - peak)
    avg_cap = _mean([r["cost"] for r in traded]) if traded else 0.0
    # market-level bootstrap on net per-market pnl
    ci = boot(pnls)
    return {
        "n_markets": n, "quotes": quotes, "fills": fills,
        "fill_rate": round(fills / quotes, 4) if quotes else 0,
        "markets_with_any_fill": len(traded), "matched_pair_rate": round(len(both) / n, 4) if n else 0,
        "merged_pairs": merge_pairs,
        "trading_pnl": round(trading, 2), "rebate": round(rebate, 2), "net_pnl": round(net, 2),
        "net_ev_per_market": round(net / n, 5) if n else 0,
        "net_ev_per_market_ci95": ci,
        "trading_ev_per_market": round(trading / n, 5) if n else 0,
        "rebate_per_market": round(rebate / n, 5) if n else 0,
        "total_cost_deployed": round(total_cost, 1),
        "roi_on_cost": round(net / total_cost, 4) if total_cost else 0,
        "avg_capital_per_active_market": round(avg_cap, 2),
        "drawdown": round(dd, 2),
        "fills_per_market": round(fills / n, 2) if n else 0,
    }


def boot(pnls, iters=3000, seed=11):
    n = len(pnls)
    if n < 8: return None
    st = seed
    def rnd():
        nonlocal st; st = (1103515245 * st + 12345) & 0x7FFFFFFF; return st / 0x7FFFFFFF
    ms = []
    for _ in range(iters):
        s = sum(pnls[int(rnd() * n) % n] for _ in range(n)); ms.append(s / n)
    ms.sort()
    return [round(ms[int(0.025 * iters)], 4), round(ms[int(0.975 * iters)], 4)]


def main():
    data = {}
    for a in ASSETS:
        try:
            data[a] = json.load(open(f"{SP}/mm_dataset_{a}.json"))
        except Exception:
            data[a] = []
    print("dataset sizes:", {a: len(data[a]) for a in ASSETS})
    base = dict(h=0.005, L=30, mode="worst", phase="full", inv_cap=None)

    print("\n===== BASELINE (h=0.005 join-touch, L=30s, WORST queue, full window, no inv cap) =====")
    rep = {}
    for a in ASSETS:
        r = run_asset(data[a], **base); rep[a] = r
        if r["n_markets"]:
            print(f"\n[{a.upper()}] markets={r['n_markets']} quotes={r['quotes']} fillRate={r['fill_rate']} "
                  f"fills/mkt={r['fills_per_market']} matchedPairRate={r['matched_pair_rate']}")
            print(f"   trading_pnl=${r['trading_pnl']} rebate=${r['rebate']} NET=${r['net_pnl']} "
                  f"| net EV/mkt={r['net_ev_per_market']} CI95={r['net_ev_per_market_ci95']}")
            print(f"   ROI on cost={r['roi_on_cost']} avgCap/mkt=${r['avg_capital_per_active_market']} "
                  f"drawdown=${r['drawdown']} merged_pairs={r['merged_pairs']}")
        else:
            print(f"\n[{a.upper()}] no data")

    print("\n===== SENSITIVITY: queue mode (net EV/market) =====")
    for a in ASSETS:
        if not data[a]: continue
        row = {mode: run_asset(data[a], **{**base, "mode": mode})["net_ev_per_market"] for mode in ["worst", "mid", "best"]}
        print(f"  {a}: {row}")
    print("\n===== SENSITIVITY: quote lifetime L (worst queue) =====")
    for a in ASSETS:
        if not data[a]: continue
        row = {f"{L}s": run_asset(data[a], **{**base, "L": L})["net_ev_per_market"] for L in [15, 30, 60]}
        print(f"  {a}: {row}")
    print("\n===== SENSITIVITY: quote half-spread h (worst queue) =====")
    for a in ASSETS:
        if not data[a]: continue
        row = {f"{h}": run_asset(data[a], **{**base, "h": h})["net_ev_per_market"] for h in [0.005, 0.01, 0.015]}
        print(f"  {a}: {row}")
    print("\n===== SENSITIVITY: window phase (worst queue) =====")
    for a in ASSETS:
        if not data[a]: continue
        row = {ph: run_asset(data[a], **{**base, "phase": ph})["net_ev_per_market"] for ph in ["full", "early", "late", "avoid_last60"]}
        print(f"  {a}: {row}")
    print("\n===== SENSITIVITY: inventory cap (worst queue) =====")
    for a in ASSETS:
        if not data[a]: continue
        row = {str(c): run_asset(data[a], **{**base, "inv_cap": c})["net_ev_per_market"] for c in [None, 5, 10, 20]}
        print(f"  {a}: {row}")

    json.dump(rep, open(f"{SP}/mm_sim_report.json", "w"), indent=1)
    print("\nsaved mm_sim_report.json")


if __name__ == "__main__":
    main()
