"""Baseline strategy: the naive fee-aware taker used in P2A and P3A.

Buy whichever side the model favours when the after-fee edge at the best
available price exceeds `tau`, walking the book level by level, one entry per
market, with a contract cap per market and per event. Defaults match the
registered P2A/P3A rules (tau 0.10, 250 contracts per market, 1,000 per event).
"""

from __future__ import annotations

from pmw.backtest.taker import Entry, Exposure, Observation, walk


class NaiveTaker:
    name = "naive"

    def __init__(
        self, tau: float = 0.10, cap_market: float = 250.0, cap_event: float = 1000.0
    ) -> None:
        self.tau = float(tau)
        self.cap_market = float(cap_market)
        self.cap_event = float(cap_event)

    def probability(self, obs: Observation) -> float:
        """The YES probability the strategy acts on. Subclasses may adjust it."""
        return obs.p_yes

    def decide(self, obs: Observation, exposure: Exposure) -> Entry | None:
        if obs.ticker in exposure.entered:
            return None
        cap = min(
            self.cap_market, self.cap_event - exposure.event_contracts.get(obs.event_ticker, 0.0)
        )
        if cap <= 0:
            return None
        p = self.probability(obs)
        for side, q, opp_px, opp_qty in (("yes", p, obs.no_px, obs.no_qty),
                                         ("no", 1 - p, obs.yes_px, obs.yes_qty)):  # fmt: skip
            fills = walk(q, opp_px, opp_qty, self.tau, cap)
            if fills:
                return Entry(side, q, fills)
        return None
