"""
Exit policies (Phase 5 + Phase 10: isolated exit logic).

Entry logic lives in strategies.py; this module decides whether an OPEN paper
position should close early (before market resolution). The engine always closes
on resolution; these policies add optional earlier exits. Each strategy picks
one policy (Top20Strategy.exit_policy). PAPER ONLY — closes are simulated at the
current mark, never a real order.

Policies:
  hold            — never exit early (default); ride to resolution.
  tp_sl           — take-profit / stop-loss on unrealized return.
  time_stop       — exit after a max holding time.
  mirror          — exit when the copied wallet itself exits the position
                    (engine passes wallet_exited).
  kelly_rebalance — currently behaves as hold (placeholder for size rebalancing;
                    documented so it can be implemented without engine changes).
"""
from __future__ import annotations

from dataclasses import dataclass

# Default thresholds per policy (kept simple + transparent).
TP_RETURN = 0.50    # +50% of stake
SL_RETURN = -0.40   # -40% of stake
TIME_STOP_MIN = 1440  # 24h


@dataclass
class ExitDecision:
    close: bool
    reason: str | None = None


def decide(policy: str, *, unrealized_return: float, holding_minutes: float,
           wallet_exited: bool = False) -> ExitDecision:
    """Return whether to close an open position early under `policy`.

    `unrealized_return` = unrealized_pnl / stake. All inputs are paper values.
    """
    if policy == "tp_sl":
        if unrealized_return >= TP_RETURN:
            return ExitDecision(True, f"take-profit (+{unrealized_return*100:.0f}%)")
        if unrealized_return <= SL_RETURN:
            return ExitDecision(True, f"stop-loss ({unrealized_return*100:.0f}%)")
        return ExitDecision(False)
    if policy == "time_stop":
        if holding_minutes >= TIME_STOP_MIN:
            return ExitDecision(True, f"time-stop ({holding_minutes/60:.0f}h)")
        return ExitDecision(False)
    if policy == "mirror":
        if wallet_exited:
            return ExitDecision(True, "mirror: copied wallet exited")
        return ExitDecision(False)
    # hold / kelly_rebalance (placeholder) -> ride to resolution
    return ExitDecision(False)


def needs_wallet_tracking(policy: str) -> bool:
    return policy == "mirror"
