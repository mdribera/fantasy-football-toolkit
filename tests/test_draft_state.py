"""Unit tests for DraftState's forward-looking inflation (T6).

Backward-looking inflation (dollars paid over sheet value of players
already sold) marks remaining players up the moment the room overpays
early -- exactly when depleted budgets mean they'll clear under sheet.
These tests pin down the forward-looking replacement: dollars left in the
room over the sheet-value surplus of what's left to buy with them.
"""

from __future__ import annotations

from ff import config, draft_state, values


def make_valuations(*rows: tuple[str, str, int]) -> list[values.Valuation]:
    """rows: (name, position, value). Everything else is a placeholder --
    forward_inflation only reads name/position/value."""
    return [
        values.Valuation(
            name=name, position=position, pro_team="", projected_points=0.0,
            replacement_points=0.0, vorp=0.0, value=value,
        )
        for name, position, value in rows
    ]


def test_forward_inflation_is_exactly_one_with_nothing_sold():
    """By construction, a team's budget minus $1 per open slot summed across
    the league equals the sheet-value surplus of the same number of top
    players -- this is the exact scenario compute_values distributes the
    league's $1,840 biddable surplus against, so nothing sold must read as
    neither a bargain nor an overpay."""
    state = draft_state.DraftState()
    # 2 open slots per team x 10 teams = 20 total, so the top 20 players by
    # value are exactly what the room's biddable dollars get compared to.
    state.spots_left = lambda team: 2                      # type: ignore[method-assign]
    state.all_teams = lambda: list(config.TEAMS.values())   # type: ignore[method-assign]
    biddable = state.biddable_dollars_left()  # 10 * (200 - 1*2) = 1980

    # Craft a pool whose top 20 players sum to exactly that surplus.
    per_player_surplus = biddable // 20
    rows = [(f"Player{i}", "RB", 1 + per_player_surplus) for i in range(20)]
    rows += [(f"Filler{i}", "RB", 1) for i in range(5)]     # outside the top 20, must not count
    vals = make_valuations(*rows)

    assert state.forward_inflation(vals) == 1.0


def test_forward_inflation_drops_after_an_early_overpay():
    """The whole point of T6: an early overpay must push the *remaining*
    rate down, not up -- the direction backward-looking inflation gets
    backward, by marking remaining players up at exactly the moment
    depleted budgets mean they'll actually clear under sheet.

    Star RB sold for $100 against a $90 sheet value -- backward-looking
    inflation reads that as the room paying over sheet (correctly). But
    with plenty of sheet-value depth still on the board and two teams'
    combined budgets already thinned out, what's left should be projected
    to clear *under* sheet, not over."""
    state = draft_state.DraftState()
    vals = make_valuations(
        ("Star RB", "RB", 90),
        *[(f"Depth{i}", "RB", 30) for i in range(6)],   # plenty left on the board
    )
    state.purchases.append(draft_state.Purchase("Star RB", "RB", 100, "FWD"))
    state.spots_left = lambda team: 3                        # type: ignore[method-assign]
    state.all_teams = lambda: ["ME", "FWD"]                  # type: ignore[method-assign]
    state.budget_left = lambda team: 10 if team == "FWD" else 100  # type: ignore[method-assign]

    assert state.inflation(vals) > 1.0     # backward: the room overpaid

    # Biddable dollars left: (100 + 10) - 6 open slots * $1 = 104.
    # Remaining pool (6 slots, all Depth players): surplus 6 * (30-1) = 174.
    rate = state.forward_inflation(vals)
    assert round(rate, 4) == round(104 / 174, 4)
    assert rate < 1.0                      # forward: what's left is a bargain


def test_forward_inflation_by_position_tilts_by_the_backward_read():
    """A position running hot backward (paying over sheet so far) should
    still tilt its forward rate up relative to the market as a whole, even
    though the aggregate forward rate itself can be well under 1.0 -- that's
    the "positions diverge" half of T6, the part a single global number
    can't represent at all."""
    state = draft_state.DraftState(my_team="ME")
    vals = make_valuations(
        ("Hot QB", "QB", 40),
        ("Cold RB", "RB", 40),
        ("Remaining QB", "QB", 20),
        ("Remaining RB", "RB", 20),
    )
    # QB sold at 1.5x sheet, RB sold at 0.5x sheet -- same total, opposite
    # signal per position.
    state.purchases.append(draft_state.Purchase("Hot QB", "QB", 60, "RIVAL"))
    state.purchases.append(draft_state.Purchase("Cold RB", "RB", 20, "RIVAL"))
    state.spots_left = lambda team: 1                        # type: ignore[method-assign]
    state.all_teams = lambda: ["ME", "RIVAL"]                # type: ignore[method-assign]
    state.budget_left = lambda team: 100                     # type: ignore[method-assign]

    rates = state.forward_inflation_by_position(vals)
    assert rates["QB"] > rates["RB"]
    # Every position that sold something is tilted relative to the same
    # base forward rate.
    base = state.forward_inflation(vals)
    backward = state.inflation_by_position(vals)
    overall_backward = state.inflation(vals)
    assert round(rates["QB"], 6) == round(base * (backward["QB"] / overall_backward), 6)


def test_forward_inflation_by_position_omits_positions_with_no_sales():
    """No sales at a position means no backward signal to tilt by --
    callers fall back to the plain forward rate for it, mirroring
    inflation_by_position's own fallback for the backward-looking read."""
    state = draft_state.DraftState()
    vals = make_valuations(("Only Player", "RB", 10))
    assert state.forward_inflation_by_position(vals) == {}
