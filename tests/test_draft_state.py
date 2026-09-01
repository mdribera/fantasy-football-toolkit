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
    # base forward rate, shrunk toward 1.0 by how much modeled value has
    # actually sold at that position -- $40 of QB evidence against
    # TILT_EVIDENCE_DOLLARS ($25) gives w = 40/65.
    base = state.forward_inflation(vals)
    backward = state.inflation_by_position(vals)
    overall_backward = state.inflation(vals)
    w = 40 / (40 + draft_state.TILT_EVIDENCE_DOLLARS)
    expected_qb_tilt = 1 + w * (backward["QB"] / overall_backward - 1)
    assert round(rates["QB"], 6) == round(base * expected_qb_tilt, 6)


def test_targets_reports_the_full_bench_not_just_starters():
    """needs() alone reports "0 unfilled" for QB at two -- targets() is the
    one that keeps pointing at the third quarterback for byes, per
    config.ROSTER_TARGETS."""
    state = draft_state.DraftState(my_team="ME")
    state.record("Josh Allen", "QB", 60, "ME")
    state.record("Lamar Jackson", "QB", 40, "ME")
    assert state.needs("ME")["QB"] == 0
    assert state.targets("ME")["QB"] == 1


def test_position_counts_with_no_team_sums_across_the_whole_league():
    """T46: the leaguewide DRAFTED panel reuses this per-team helper with no
    team filter, rather than a parallel method -- the per-team call must
    keep filtering exactly as before."""
    state = draft_state.DraftState(my_team="ME")
    state.record("Josh Allen", "QB", 60, "ME")
    state.record("Lamar Jackson", "QB", 40, "RIVAL")
    state.record("Bijan Robinson", "RB", 54, "RIVAL")

    assert state.position_counts("ME") == {"QB": 1}
    assert state.position_counts() == {"QB": 2, "RB": 1}


def test_forward_inflation_by_position_omits_positions_with_no_sales():
    """No sales at a position means no backward signal to tilt by --
    callers fall back to the plain forward rate for it, mirroring
    inflation_by_position's own fallback for the backward-looking read."""
    state = draft_state.DraftState()
    vals = make_valuations(("Only Player", "RB", 10))
    assert state.forward_inflation_by_position(vals) == {}


def test_forward_inflation_is_none_with_zero_surplus_and_cash_left():
    """T36a: the endgame money-dump case. Every roster spot is already
    filled (zero surplus to compare the room's leftover cash against), but
    the room still has real money -- reading that as 1.0 would say "market
    is calm" when there's nothing left to be calm about. It should read as
    no signal at all, and the per-position tilt has nothing to tilt."""
    state = draft_state.DraftState(my_team="ME")
    vals = make_valuations(("Player", "RB", 10))
    state.spots_left = lambda team: 0                         # type: ignore[method-assign]
    state.all_teams = lambda: ["ME"]                          # type: ignore[method-assign]
    state.budget_left = lambda team: 50                       # type: ignore[method-assign]

    assert state.forward_inflation(vals) is None
    assert state.forward_inflation_by_position(vals) == {}


def test_forward_inflation_clamps_at_forward_rate_max_on_a_tiny_surplus():
    """T36a: the last few picks of the draft can leave a razor-thin sheet
    surplus against dollars that are mostly the mandatory $1-per-slot
    floor, not real bidding pressure -- an unclamped ratio would report an
    absurd 30x read instead of the intended 3.0x ceiling."""
    state = draft_state.DraftState(my_team="ME")
    vals = make_valuations(("OnlyPlayer", "RB", 6))   # surplus = 6 - MIN_BID = 5
    state.spots_left = lambda team: 1                         # type: ignore[method-assign]
    state.all_teams = lambda: ["ME"]                          # type: ignore[method-assign]
    state.budget_left = lambda team: 151                      # biddable = 151 - 1 = 150

    assert state.remaining_pool_surplus(vals) == 5
    assert state.biddable_dollars_left() == 150
    assert state.forward_inflation(vals) == draft_state.FORWARD_RATE_MAX


def test_forward_inflation_is_one_when_the_draft_is_over():
    """Zero surplus and zero cash both hitting zero at once is the one
    legitimate case for the 1.0 sentinel: the draft is over, so there is
    nothing left to misjudge and no reason to show "no read"."""
    state = draft_state.DraftState(my_team="ME")
    vals = make_valuations(("Player", "RB", 10))
    state.spots_left = lambda team: 0                         # type: ignore[method-assign]
    state.all_teams = lambda: ["ME"]                          # type: ignore[method-assign]
    state.budget_left = lambda team: 0                        # type: ignore[method-assign]

    assert state.forward_inflation(vals) == 1.0


def test_forward_inflation_by_position_shrinks_a_single_cheap_qb_sale():
    """T36b audit repro: a $3 QB sold for $9 is a 3x paid/modeled ratio on
    a sample size of one -- the pre-T36b clamp would still let that single
    sale double the whole QB board (tilt 2.0). Weighting the tilt by
    modeled-dollar evidence keeps a thin sample from swinging the read that
    hard, while a handful of at-sheet non-QB sales gives the room a mostly
    calm backward baseline to tilt against."""
    state = draft_state.DraftState(my_team="ME")
    vals = make_valuations(
        ("Cheap QB", "QB", 3),
        *[(f"Filler{i}", "RB", 50) for i in range(5)],
        ("Josh Allen", "QB", 80),
        *[(f"Depth{i}", "RB", 20) for i in range(10)],
    )
    state.purchases.append(draft_state.Purchase("Cheap QB", "QB", 9, "RIVAL"))
    for i in range(5):
        state.purchases.append(draft_state.Purchase(f"Filler{i}", "RB", 50, "RIVAL"))
    state.spots_left = lambda team: 8                         # type: ignore[method-assign]
    state.all_teams = lambda: ["ME", "RIVAL"]                 # type: ignore[method-assign]
    state.budget_left = lambda team: 150                      # type: ignore[method-assign]

    base = state.forward_inflation(vals)
    rate = state.forward_inflation_by_position(vals)["QB"]
    assert rate > base            # still tilted up, the sale really was over sheet
    assert rate < base * 1.4      # but nowhere near the old clamped-at-2.0 double


def test_forward_inflation_by_position_tilt_approaches_raw_ratio_with_more_evidence():
    """T36b: as modeled-dollar evidence at a position grows past
    TILT_EVIDENCE_DOLLARS, the shrinkage weight w approaches 1 and the tilt
    should converge on the raw (unshrunk) backward-vs-backward ratio,
    rather than staying pinned near 1.0 the way a single cheap sale does."""
    state = draft_state.DraftState(my_team="ME")
    vals = make_valuations(
        *[(f"QB{i}", "QB", 50) for i in range(10)],
        *[(f"RB{i}", "RB", 50) for i in range(10)],
        *[(f"Depth{i}", "WR", 20) for i in range(20)],
    )
    for i in range(10):
        state.purchases.append(draft_state.Purchase(f"QB{i}", "QB", 65, "RIVAL"))  # 1.3x sheet
        state.purchases.append(draft_state.Purchase(f"RB{i}", "RB", 50, "RIVAL"))  # at sheet
    state.spots_left = lambda team: 10                        # type: ignore[method-assign]
    state.all_teams = lambda: ["ME", "RIVAL"]                 # type: ignore[method-assign]
    state.budget_left = lambda team: 400                      # type: ignore[method-assign]

    base = state.forward_inflation(vals)
    overall_backward = state.inflation(vals)
    backward = state.inflation_by_position(vals)
    raw_ratio = backward["QB"] / overall_backward

    tilt = state.forward_inflation_by_position(vals)["QB"] / base
    assert abs(tilt - raw_ratio) < 0.02
