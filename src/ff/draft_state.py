"""Live auction state tracking.

During the draft the only questions that matter are: what can I still afford,
what is left at each position, and is the market running hot or cold relative
to my valuations. This keeps that state in a JSON file so it survives a crashed
terminal mid-draft.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

from . import config

STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "draft-state.json"

# Endgame money dump: once the priced pool is gone there is no surplus left
# to divide the room's cash against, so an unclamped ratio explodes toward
# infinity on the last few picks -- clamp it well above any real mid-draft
# read instead.
FORWARD_RATE_MAX = 3.0
# Dollars of modeled value a position has to have actually sold before its
# backward-looking read is trusted at full strength -- see
# forward_inflation_by_position.
TILT_EVIDENCE_DOLLARS = 25

# In --ws mode, the background printer thread records sales (record_pick ->
# save()) alongside the main thread's own record()/undo() calls. Without this,
# two threads writing the same fixed tmp path can interleave, corrupting the
# write or making one thread's os.replace lose a FileNotFoundError race.
_save_lock = threading.Lock()


def normalize_team(name: str) -> str:
    """Fold team labels to a single canonical form.

    Typing "rival1" and "RIVAL1" during a live draft would otherwise create two
    teams, each with its own phantom budget, and quietly corrupt every number
    the console reports. Normalising on the way in is the cheap fix.
    """
    return name.strip().upper()


@dataclass
class Purchase:
    player: str
    position: str
    price: int
    team: str
    espn_pick_id: int | None = None  # ESPN's permanent id for the real player,
                                      # shared across every league and draft that
                                      # player appears in -- not scoped to one draft


@dataclass
class DraftState:
    purchases: list[Purchase] = field(default_factory=list)
    my_team: str = "ME"
    # Pick ids removed by 'undo' this session, so a still-running poller doesn't
    # immediately re-import something just taken back out. Not persisted --
    # ESPN's own state moved on, so this only needs to survive one session.
    _suppressed_pick_ids: set = field(default_factory=set, repr=False, compare=False)
    # Where save()/record()/record_pick()/undo() write by default. A plain
    # dataclass default of STATE_PATH would mean every DraftState -- including
    # a scratch one built for a --replay rehearsal -- writes over the real
    # live-draft file the moment anything is recorded. Keeping it per-instance
    # lets tooling opt into an isolated path.
    state_path: Path = field(default=STATE_PATH, repr=False, compare=False)

    # --- budgets ---------------------------------------------------------
    def spent_by(self, team: str) -> int:
        return sum(p.price for p in self.purchases if p.team == team)

    def roster_count(self, team: str) -> int:
        return sum(1 for p in self.purchases if p.team == team)

    def budget_left(self, team: str) -> int:
        return config.SALARY_CAP - self.spent_by(team)

    def spots_left(self, team: str) -> int:
        return config.ROSTER_SIZE - self.roster_count(team)

    def max_bid(self, team: str) -> int:
        """Largest legal bid that still leaves $1 per remaining open slot."""
        return config.max_bid(self.budget_left(team), self.spots_left(team))

    def all_teams(self) -> list[str]:
        seen = set(config.TEAMS.values())
        seen.update(p.team for p in self.purchases)
        seen.add(self.my_team)
        return sorted(seen)

    # --- market ----------------------------------------------------------
    def inflation(self, valuations: list) -> float:
        """Ratio of prices actually paid to modeled value, so far.

        Above 1.0 means the room is overpaying and the players you have left
        will come cheaper than your sheet says. Below 1.0 means bargains are
        gone and you should expect to pay over sheet for what remains.
        """
        lookup = {v.name: v.value for v in valuations}
        paid = 0
        modeled = 0
        for purchase in self.purchases:
            if purchase.player in lookup:
                paid += purchase.price
                modeled += lookup[purchase.player]
        return (paid / modeled) if modeled else 1.0

    def _position_paid_modeled(self, valuations: list) -> dict[str, tuple[int, int]]:
        """(paid, modeled) dollar totals per position, for the positions with
        at least one matched sale -- the shared tally inflation_by_position's
        ratio and forward_inflation_by_position's evidence weighting both
        derive from."""
        lookup = {v.name: v for v in valuations}
        paid: dict[str, int] = {}
        modeled: dict[str, int] = {}
        for purchase in self.purchases:
            match = lookup.get(purchase.player)
            if not match:
                continue
            paid[match.position] = paid.get(match.position, 0) + purchase.price
            modeled[match.position] = modeled.get(match.position, 0) + match.value
        return {pos: (paid[pos], modeled.get(pos, 0)) for pos in paid}

    def inflation_by_position(self, valuations: list) -> dict[str, float]:
        """Inflation broken out per position.

        This is the number to actually draft off. A global figure hides the
        thing you need: in a 2QB league the room reliably bids quarterbacks
        above sheet and, because the $2,000 is fixed, must therefore be bidding
        something else below sheet. Whichever position is running under 1.0 is
        where your remaining dollars buy the most points.
        """
        return {
            pos: paid / modeled
            for pos, (paid, modeled) in self._position_paid_modeled(valuations).items()
            if modeled > 0
        }

    def dollars_remaining_in_room(self) -> int:
        """Cash the other nine teams still hold. Drives late-draft leverage."""
        others = [t for t in self.all_teams() if t != self.my_team]
        return sum(self.budget_left(t) for t in others)

    def slots_left_total(self) -> int:
        return sum(self.spots_left(t) for t in self.all_teams())

    def biddable_dollars_left(self) -> int:
        """Every dollar left in the room, minus the $1 every remaining open
        roster slot will cost no matter what -- the forward-looking analogue
        of config.BIDDABLE_SURPLUS, which is exactly this number before
        anything has been sold."""
        dollars_left = sum(self.budget_left(t) for t in self.all_teams())
        return dollars_left - self.slots_left_total() * config.MIN_BID

    def remaining_pool_surplus(self, valuations: list) -> int:
        """Sheet-value surplus of the pool that will actually get drafted
        with the room's remaining money: the top N undrafted players by
        value, where N is exactly the number of roster spots left across the
        league -- the same "only the players who'll actually be rostered
        compete for the surplus" logic values.compute_values uses to build
        the sheet in the first place."""
        taken = self.taken()
        slots_left = self.slots_left_total()
        pool = sorted((v for v in valuations if v.name not in taken),
                      key=lambda v: v.value, reverse=True)[:slots_left]
        return sum(max(0, v.value - config.MIN_BID) for v in pool)

    def forward_inflation(self, valuations: list) -> float | None:
        """Ratio of dollars left in the room to the sheet-value surplus of
        what's left to buy with them -- the forward-looking counterpart to
        inflation(). Backward-looking inflation marks remaining players up
        the moment the room overpays early, at exactly the point depleted
        budgets mean they'll actually clear under sheet; this is the number
        that drives the Adjusted column and the live buy/pass call instead.

        Clamped at FORWARD_RATE_MAX rather than left to run unbounded, since
        a thin remaining surplus (the last few picks of the draft) can send
        the raw ratio to absurd multiples on money that's mostly the
        mandatory $1-per-slot floor, not real bidding pressure.

        None means "no read": zero surplus with real money still in the
        room has no priced pool left to compare that money against, so no
        ratio is meaningful. Reporting 1.0 there (as a prior version did)
        reads as "market is calm," which silently masks exactly the
        every-team-broke endgame dump this is supposed to flag. Zero
        surplus with zero money left (the draft is over) is the one
        legitimate 1.0: nothing left to misjudge, so it's a harmless
        display value rather than a sentinel.
        """
        surplus = self.remaining_pool_surplus(valuations)
        biddable = self.biddable_dollars_left()
        if surplus > 0:
            return min(FORWARD_RATE_MAX, biddable / surplus)
        if biddable > 0:
            return None
        return 1.0

    def forward_inflation_by_position(self, valuations: list,
                                      tilt_bounds: tuple[float, float] = (0.5, 2.0)
                                      ) -> dict[str, float]:
        """Forward-looking inflation, tilted per position.

        Dollars in the room aren't earmarked by position, so there's no way
        to compute a genuinely separate forward rate per position. Instead
        this takes the one forward-looking number -- which is exactly right
        in aggregate -- and skews it by how the room has actually priced
        that position so far: the ratio of that position's backward rate
        (inflation_by_position) to the overall backward rate (inflation).
        That ratio is the signal for "this position is running hot or cold
        relative to the market as a whole."

        The ratio alone is not enough to trust, though: a single early sale
        is a sample size of one, and a $3 QB going for $9 is a 3x ratio on
        no evidence at all -- clamping the ratio still lets that one sale
        double the whole QB board. So the ratio is shrunk toward 1.0 by how
        much modeled value has actually sold at that position (w, growing
        from 0 with no sales to 1 as evidence passes TILT_EVIDENCE_DOLLARS)
        before tilt_bounds clamps the result as an outer bound. Only
        positions with at least one sale get a tilt; everything else falls
        back to the plain forward rate, same as inflation_by_position's own
        fallback.

        Returns {} when there's no forward rate to tilt (forward_inflation
        is None) or no backward baseline to tilt against.
        """
        base = self.forward_inflation(valuations)
        if base is None:
            return {}
        overall_backward = self.inflation(valuations)
        if overall_backward <= 0:
            return {}
        by_position_backward = self.inflation_by_position(valuations)
        paid_modeled = self._position_paid_modeled(valuations)
        low, high = tilt_bounds
        result = {}
        for pos, rate in by_position_backward.items():
            _, modeled = paid_modeled[pos]
            w = modeled / (modeled + TILT_EVIDENCE_DOLLARS)
            tilt = max(low, min(high, 1 + w * (rate / overall_backward - 1)))
            result[pos] = base * tilt
        return result

    def taken(self) -> set[str]:
        return {p.player for p in self.purchases}

    def position_counts(self, team: str | None = None) -> dict[str, int]:
        """Positions drafted by `team`, or leaguewide when team is None."""
        counts: dict[str, int] = {}
        for p in self.purchases:
            if team is None or p.team == team:
                counts[p.position] = counts.get(p.position, 0) + 1
        return counts

    def position_spend(self, team: str | None = None) -> dict[str, int]:
        """Dollars spent by `team` at each position, or leaguewide when team
        is None."""
        spend: dict[str, int] = {}
        for p in self.purchases:
            if team is None or p.team == team:
                spend[p.position] = spend.get(p.position, 0) + p.price
        return spend

    def needs(self, team: str) -> dict[str, int]:
        """Starting slots still unfilled."""
        counts = self.position_counts(team)
        out = {}
        for pos, required in config.STARTERS.items():
            if pos == "FLEX":
                continue
            out[pos] = max(0, required - counts.get(pos, 0))
        return out

    def targets(self, team: str) -> dict[str, int]:
        """Full-bench slots still unfilled -- config.ROSTER_TARGETS, which
        goes beyond the starting lineup (a third QB for byes, bench depth at
        RB/WR/TE). needs() alone reports "all starting slots filled" at two
        quarterbacks, when the league's most important roster rule is that a
        team should carry three."""
        counts = self.position_counts(team)
        return {
            pos: max(0, target - counts.get(pos, 0))
            for pos, target in config.ROSTER_TARGETS.items()
        }

    def needs_starter(self, team: str, position: str) -> bool:
        """Whether `position` still fills an unfilled starting slot --
        needs() alone, since it skips FLEX entirely, says no as soon as a
        position's own STARTERS count is met even while the FLEX slot (RB/WR/
        TE only) sits open."""
        if self.needs(team).get(position, 0) > 0:
            return True
        if position not in config.FLEX_ELIGIBLE:
            return False
        counts = self.position_counts(team)
        flex_filled = sum(
            max(0, counts.get(pos, 0) - config.STARTERS.get(pos, 0))
            for pos in config.FLEX_ELIGIBLE
        )
        return flex_filled < config.STARTERS["FLEX"]

    # --- persistence -----------------------------------------------------
    def record(self, player: str, position: str, price: int, team: str) -> None:
        self.purchases.append(Purchase(player, position, price, normalize_team(team)))
        self.save()

    def record_pick(
        self, player: str, position: str, price: int, team: str, espn_pick_id: int
    ) -> bool:
        """Record a pick from the automated feed, keyed by ESPN's pick id.

        Returns False without recording if this pick was already imported, or
        was just removed with 'undo' -- both cases where re-adding it would be
        wrong rather than merely redundant.
        """
        if espn_pick_id in self._suppressed_pick_ids:
            return False
        if any(p.espn_pick_id == espn_pick_id for p in self.purchases):
            return False
        self.purchases.append(
            Purchase(player, position, price, normalize_team(team), espn_pick_id)
        )
        self.save()
        return True

    def undo(self) -> Purchase | None:
        if not self.purchases:
            return None
        last = self.purchases.pop()
        if last.espn_pick_id is not None:
            self._suppressed_pick_ids.add(last.espn_pick_id)
        self.save()
        return last

    def save(self, path: Path | None = None) -> None:
        path = path or self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with _save_lock:
            payload = json.dumps(
                {"my_team": self.my_team, "purchases": [asdict(p) for p in self.purchases]},
                indent=2,
            )
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload)
            os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "DraftState":
        if not path.exists():
            return cls(state_path=path)
        raw = json.loads(path.read_text())
        known = {f.name for f in fields(Purchase)}
        purchases = [Purchase(**{k: v for k, v in p.items() if k in known})
                     for p in raw.get("purchases", [])]
        return cls(my_team=raw.get("my_team", "ME"), purchases=purchases, state_path=path)
