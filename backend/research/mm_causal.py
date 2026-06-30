"""Causal-cancellation emulator — EV vs reaction latency (Phase 0a-2).

THE decision experiment: how fast must we react to capture enough of the two-sided maker edge
to be net-profitable, per asset?

Model (causal, no foresight): a maker whose quotes reflect market state from `tau` ago — i.e.
reaction latency `tau` = quote staleness (the time to detect an adverse move and cancel/replace).
At time t my resting quotes sit at mid(t-tau) ± h. A trade fills a quote only if it trades THROUGH
it (worst-queue; a slow account behind the bots). Adverse selection is NOT assumed — it emerges:
in a falling market the *stale* (too-high) Up bid gets run over; faster reaction (smaller tau)
keeps the quote fresh and avoids it, while benign oscillations still fill both sides (spread
captured). Realistic constraints: one resting order per side (size S), repost takes `tau`, and a
net-inventory cap (stop adding to the heavy side).

Resolution caveat: tape timestamps are 1-second. Sub-second tau (<1s) uses deterministic
intra-second jitter and is LOWER-confidence + trade-density-dependent (thin assets like XRP can't
resolve sub-second — nothing trades in between). tau >= 1s is directly supported.

Pure offline research. No live trades, no executor.
"""
import json, os, sys
import statistics as st

SP = sys.argv[1] if len(sys.argv) > 1 else "."
ASSETS = ["btc", "eth", "sol", "xrp"]
S = 5.0                          # order size (shares)
REB = 0.20 * 0.07                # maker rebate coefficient (20% of crypto taker fee)
H = 0.005                        # half-spread (quote at the 1c touch)
INV_CAP = 10.0                   # max net inventory per side (shares)
MARKOUT = 5.0                    # seconds, for adverse-selection mark-out


def _jittered(prints, seed):
    """Assign deterministic intra-second offsets so sub-second tau is resolvable. prints are
    [sec, price, size, side]; returns sorted [(t_cont, price, size)]."""
    st_ = seed
    def rnd():
        nonlocal st_; st_ = (1103515245 * st_ + 12345) & 0x7FFFFFFF; return st_ / 0x7FFFFFFF
    out = [(p[0] + rnd(), p[1], p[2]) for p in prints]
    out.sort()
    return out


def _midfn(events):
    times = [e[0] for e in events]; prices = [e[1] for e in events]
    import bisect
    def mid(s):
        i = bisect.bisect_right(times, s) - 1
        return prices[max(0, i)]
    return mid


def sim_market(mkt, tau, *, h=H, theta=0.004, W=1.0, refresh=2.0, inv_cap=INV_CAP, seed=12345):
    """Fixed quote cadence (refresh); tau ONLY controls the momentum-cancellation reaction.
    We pull a side's quote when adverse mid-momentum is observable; a through-trade fills us
    only if that adverse signal was NOT yet visible tau seconds before the trade (we couldn't
    pull in time). Causal: momentum is computed from mid(t-tau) vs mid(t-tau-W) — past info only."""
    ev = _jittered(mkt["prints"], seed)
    if len(ev) < 3:
        return None
    mid = _midfn(ev)
    won = 1.0 if mkt["won_up"] else 0.0
    up = dn = cost = reb = 0.0
    fills = 0; thru = 0; avoided = 0
    markouts = []
    import bisect
    tlist = [e[0] for e in ev]
    t = 0.0
    while t < 305:
        M = mid(t); b_up = M - h; a_up = M + h
        lo = bisect.bisect_left(tlist, t); hi = bisect.bisect_left(tlist, t + refresh)
        up_done = dn_done = False
        for k in range(lo, hi):
            te, p, sz = ev[k]
            if (not up_done) and p < b_up and b_up > 0.02 and (up - dn) < inv_cap:
                thru += 1
                mom = mid(te - tau) - mid(te - tau - W)          # adverse for Up bid if falling
                if mom < -theta:
                    avoided += 1; up_done = True                  # pulled in time -> no fill
                else:
                    up += S; cost += S * b_up; reb += REB * S * b_up * (1 - b_up)
                    fills += 1; up_done = True; markouts.append(("up", te, b_up))
            if (not dn_done) and p > a_up and a_up < 0.98 and (dn - up) < inv_cap:
                thru += 1
                mom = mid(te - tau) - mid(te - tau - W)           # adverse for Down bid if rising
                if mom > theta:
                    avoided += 1; dn_done = True
                else:
                    dnp = 1 - a_up
                    dn += S; cost += S * dnp; reb += REB * S * dnp * (1 - dnp)
                    fills += 1; dn_done = True; markouts.append(("dn", te, a_up))
        t += refresh
    adv = []
    for side, tf, lvl in markouts:
        m2 = mid(tf + MARKOUT)
        adv.append((m2 - lvl) if side == "up" else (lvl - m2))
    payout = up * won + dn * (1 - won)
    net = payout - cost + reb
    return {"net": net, "cost": cost, "fills": fills, "thru": thru, "avoided": avoided,
            "both": up > 0 and dn > 0, "pairs": min(up, dn), "adv": st.mean(adv) if adv else None,
            "up": up, "dn": dn, "reb": reb, "trading": payout - cost}


def boot(xs, iters=3000, seed=5):
    n = len(xs)
    if n < 8: return None
    s = seed
    def r():
        nonlocal s; s = (1103515245 * s + 12345) & 0x7FFFFFFF; return s / 0x7FFFFFFF
    ms = sorted(sum(xs[int(r() * n) % n] for _ in range(n)) / n for _ in range(iters))
    return [round(ms[int(.025 * iters)], 4), round(ms[int(.975 * iters)], 4)]


def run_asset(mkts, tau, *, theta=0.004, refresh=2.0):
    rs = [sim_market(m, tau, theta=theta, refresh=refresh, seed=12345 + i) for i, m in enumerate(mkts)]
    rs = [r for r in rs if r]
    n = len(rs)
    if not n: return None
    nets = [r["net"] for r in rs]
    fills = sum(r["fills"] for r in rs); thru = sum(r["thru"] for r in rs)
    avoided = sum(r["avoided"] for r in rs)
    both = sum(1 for r in rs if r["both"])
    advs = [r["adv"] for r in rs if r["adv"] is not None]
    cost = sum(r["cost"] for r in rs)
    traded = [r["cost"] for r in rs if r["fills"] > 0]
    return {
        "tau": tau, "n": n,
        "fills_per_mkt": round(fills / n, 2),
        "cancel_rate": round(avoided / thru, 3) if thru else 0,     # share of through-events we dodged
        "matched_pair_rate": round(both / n, 3),
        "adverse_sel": round(st.mean(advs), 4) if advs else None,    # avg 5s mark-out (yes terms; <0 adverse)
        "merge_spread_est": round(sum(r["pairs"] for r in rs) * 2 * H, 2),  # captured spread on matched pairs (est)
        "rebate_per_mkt": round(sum(r["reb"] for r in rs) / n, 4),
        "trading_per_mkt": round(sum(r["trading"] for r in rs) / n, 4),
        "net_ev_per_mkt": round(st.mean(nets), 4),
        "ci95": boot(nets),
        "cap_eff": round(sum(nets) / cost, 4) if cost else 0,        # net EV per $ deployed
        "avg_cap": round(st.mean(traded), 2) if traded else 0,
    }


def main():
    data = {a: json.load(open(os.path.join(SP, f"mm_dataset_{a}.json"))) for a in ASSETS}
    taus = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]
    print("dataset:", {a: len(data[a]) for a in ASSETS})
    print("(per (asset,tau): best of theta in {0.002,0.004,0.008} and refresh in {1,2}s — best-case for the strategy)")
    print("\n================  EV vs REACTION LATENCY  (net EV $/market; CI95)  ================")
    print(f"{'tau(s)':>7} | " + " | ".join(f"{a.upper():^22}" for a in ASSETS))
    table = {a: {} for a in ASSETS}
    GRID = [(th, rf) for th in (0.002, 0.004, 0.008) for rf in (1.0, 2.0)]
    for tau in taus:
        cells = []
        for a in ASSETS:
            best = max((run_asset(data[a], tau, theta=th, refresh=rf) for th, rf in GRID),
                       key=lambda r: r["net_ev_per_mkt"])
            table[a][tau] = best
            cells.append(f"{best['net_ev_per_mkt']:+.3f} {str(best['ci95']):>16}")
        print(f"{tau:>7} | " + " | ".join(cells))
    # zero-crossing per asset
    print("\n================  ZERO-CROSSING (max tau with net EV>0 AND CI95 low>0)  ================")
    for a in ASSETS:
        prof = [tau for tau in taus if (table[a][tau]["net_ev_per_mkt"] > 0)]
        sig = [tau for tau in taus if table[a][tau]["ci95"] and table[a][tau]["ci95"][0] > 0]
        print(f"  {a.upper()}: net>0 at tau<= {max(prof) if prof else 'NONE'} s ; "
              f"SIGNIFICANT (CI>0) at tau<= {max(sig) if sig else 'NONE'} s")
    # detailed metrics at a few key taus
    print("\n================  DETAIL @ tau in {0.25, 1.0, 2.0}  ================")
    for tau in [0.25, 1.0, 2.0]:
        print(f"\n-- tau={tau}s --")
        for a in ASSETS:
            r = table[a][tau]
            print(f"  {a.upper()}: net=${r['net_ev_per_mkt']:+.3f}/mkt ci={r['ci95']} fills/mkt={r['fills_per_mkt']} "
                  f"cancel_rate={r['cancel_rate']} matched={r['matched_pair_rate']} adv5s={r['adverse_sel']} "
                  f"reb/mkt=${r['rebate_per_mkt']} capEff={r['cap_eff']} avgCap=${r['avg_cap']}")
    json.dump(table, open(os.path.join(SP, "mm_causal_report.json"), "w"), indent=1)
    print("\nsaved mm_causal_report.json")


if __name__ == "__main__":
    main()
