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
    state.purchases.append(draft_state.Purchase("Star RB", "RB", 100, "TEAM5"))
    state.spots_left = lambda team: 3                        # type: ignore[method-assign]
    state.all_teams = lambda: ["MARK", "TEAM5"]                  # type: ignore[method-assign]
    state.budget_left = lambda team: 10 if team == "TEAM5" else 100  # type: ignore[method-assign]

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
    state = draft_state.DraftState(my_team="MARK")
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
    state.all_teams = lambda: ["MARK", "RIVAL"]                # type: ignore[method-assign]
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
    state = draft_state.DraftState(my_team="MARK")
    state.record("Josh Allen", "QB", 60, "MARK")
    state.record("Lamar Jackson", "QB", 40, "MARK")
    assert state.needs("MARK")["QB"] == 0
    assert state.targets("MARK")["QB"] == 1


def test_needs_starter_true_for_an_unfilled_starting_position():
    state = draft_state.DraftState(my_team="MARK")
    assert state.needs_starter("MARK", "QB") is True


def test_needs_starter_true_for_flex_eligible_once_starters_are_met():
    """RB's own STARTERS count (2) is met, but nothing has filled the open
    FLEX slot yet -- needs() alone would report 0 and miss this."""
    state = draft_state.DraftState(my_team="MARK")
    state.record("Bijan Robinson", "RB", 54, "MARK")
    state.record("Kenneth Walker III", "RB", 38, "MARK")
    assert state.needs("MARK")["RB"] == 0
    assert state.needs_starter("MARK", "RB") is True


def test_needs_starter_false_once_another_position_has_filled_flex():
    """A third WR beyond WR's own starting requirement fills FLEX, so RB no
    longer needs a starter even though RB itself sits at exactly 2."""
    state = draft_state.DraftState(my_team="MARK")
    state.record("Bijan Robinson", "RB", 54, "MARK")
    state.record("Kenneth Walker III", "RB", 38, "MARK")
    state.record("Justin Jefferson", "WR", 52, "MARK")
    state.record("Puka Nacua", "WR", 45, "MARK")
    state.record("Amon-Ra St. Brown", "WR", 40, "MARK")
    assert state.needs_starter("MARK", "RB") is False


def test_needs_starter_false_for_a_covered_non_flex_position():
    """K and D/ST aren't FLEX-eligible, so meeting their own starting
    requirement is the whole answer -- no FLEX fallback to check."""
    state = draft_state.DraftState(my_team="MARK")
    state.record("Some Kicker", "K", 1, "MARK")
    assert state.needs_starter("MARK", "K") is False


def test_position_counts_with_no_team_sums_across_the_whole_league():
    """T46: the leaguewide DRAFTED panel reuses this per-team helper with no
    team filter, rather than a parallel method -- the per-team call must
    keep filtering exactly as before."""
    state = draft_state.DraftState(my_team="MARK")
    state.record("Josh Allen", "QB", 60, "MARK")
    state.record("Lamar Jackson", "QB", 40, "RIVAL")
    state.record("Bijan Robinson", "RB", 54, "RIVAL")

    assert state.position_counts("MARK") == {"QB": 1}
    assert state.position_counts() == {"QB": 2, "RB": 1}


def test_position_spend_sums_prices_for_one_team():
    """T67: the Roster panel's plan-dollars row reads this per position, so
    it has to sum price, not count, and stay scoped to the requested team."""
    state = draft_state.DraftState(my_team="MARK")
    state.record("Josh Allen", "QB", 60, "MARK")
    state.record("Lamar Jackson", "QB", 40, "MARK")
    state.record("Bijan Robinson", "RB", 54, "RIVAL")

    assert state.position_spend("MARK") == {"QB": 100}


def test_position_spend_with_no_team_sums_across_the_whole_league():
    state = draft_state.DraftState(my_team="MARK")
    state.record("Josh Allen", "QB", 60, "MARK")
    state.record("Lamar Jackson", "QB", 40, "RIVAL")
    state.record("Bijan Robinson", "RB", 54, "RIVAL")

    assert state.position_spend() == {"QB": 100, "RB": 54}


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
    state = draft_state.DraftState(my_team="MARK")
    vals = make_valuations(("Player", "RB", 10))
    state.spots_left = lambda team: 0                         # type: ignore[method-assign]
    state.all_teams = lambda: ["MARK"]                          # type: ignore[method-assign]
    state.budget_left = lambda team: 50                       # type: ignore[method-assign]

    assert state.forward_inflation(vals) is None
    assert state.forward_inflation_by_position(vals) == {}


def test_forward_inflation_clamps_at_forward_rate_max_on_a_tiny_surplus():
    """T36a: the last few picks of the draft can leave a razor-thin sheet
    surplus against dollars that are mostly the mandatory $1-per-slot
    floor, not real bidding pressure -- an unclamped ratio would report an
    absurd 30x read instead of the intended 3.0x ceiling."""
    state = draft_state.DraftState(my_team="MARK")
    vals = make_valuations(("OnlyPlayer", "RB", 6))   # surplus = 6 - MIN_BID = 5
    state.spots_left = lambda team: 1                         # type: ignore[method-assign]
    state.all_teams = lambda: ["MARK"]                          # type: ignore[method-assign]
    state.budget_left = lambda team: 151                      # biddable = 151 - 1 = 150

    assert state.remaining_pool_surplus(vals) == 5
    assert state.biddable_dollars_left() == 150
    assert state.forward_inflation(vals) == draft_state.FORWARD_RATE_MAX


def test_forward_inflation_is_one_when_the_draft_is_over():
    """Zero surplus and zero cash both hitting zero at once is the one
    legitimate case for the 1.0 sentinel: the draft is over, so there is
    nothing left to misjudge and no reason to show "no read"."""
    state = draft_state.DraftState(my_team="MARK")
    vals = make_valuations(("Player", "RB", 10))
    state.spots_left = lambda team: 0                         # type: ignore[method-assign]
    state.all_teams = lambda: ["MARK"]                          # type: ignore[method-assign]
    state.budget_left = lambda team: 0                        # type: ignore[method-assign]

    assert state.forward_inflation(vals) == 1.0


def test_forward_inflation_by_position_shrinks_a_single_cheap_qb_sale():
    """T36b audit repro: a $3 QB sold for $9 is a 3x paid/modeled ratio on
    a sample size of one -- the pre-T36b clamp would still let that single
    sale double the whole QB board (tilt 2.0). Weighting the tilt by
    modeled-dollar evidence keeps a thin sample from swinging the read that
    hard, while a handful of at-sheet non-QB sales gives the room a mostly
    calm backward baseline to tilt against."""
    state = draft_state.DraftState(my_team="MARK")
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
    state.all_teams = lambda: ["MARK", "RIVAL"]                 # type: ignore[method-assign]
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
    state = draft_state.DraftState(my_team="MARK")
    vals = make_valuations(
        *[(f"QB{i}", "QB", 50) for i in range(10)],
        *[(f"RB{i}", "RB", 50) for i in range(10)],
        *[(f"Depth{i}", "WR", 20) for i in range(20)],
    )
    for i in range(10):
        state.purchases.append(draft_state.Purchase(f"QB{i}", "QB", 65, "RIVAL"))  # 1.3x sheet
        state.purchases.append(draft_state.Purchase(f"RB{i}", "RB", 50, "RIVAL"))  # at sheet
    state.spots_left = lambda team: 10                        # type: ignore[method-assign]
    state.all_teams = lambda: ["MARK", "RIVAL"]                 # type: ignore[method-assign]
    state.budget_left = lambda team: 400                      # type: ignore[method-assign]

    base = state.forward_inflation(vals)
    overall_backward = state.inflation(vals)
    backward = state.inflation_by_position(vals)
    raw_ratio = backward["QB"] / overall_backward

    tilt = state.forward_inflation_by_position(vals)["QB"] / base
    assert abs(tilt - raw_ratio) < 0.02


def _by_name(**projections: float):
    """Build lineup_slots' projected callable from a name -> points map --
    a purchase whose name is missing sorts as though unprojected (a player
    off the sheet, or a name resolution failure)."""
    return lambda p: projections.get(p.player, float("-inf"))


def test_lineup_slots_reserves_every_starter_and_bench_row_when_empty():
    """T68: the roster panel always shows all 16 slots -- QB, QB, RB, RB, WR,
    WR, TE, FLEX, D/ST, K, then 6 bench rows -- whether anyone fills them or
    not, matching config.STARTERS's order plus config.BENCH_SLOTS."""
    slots = draft_state.lineup_slots([], _by_name())
    assert [label for label, _ in slots] == [
        "QB", "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "D/ST", "K",
        "BE", "BE", "BE", "BE", "BE", "BE",
    ]
    assert all(purchase is None for _, purchase in slots)


def test_lineup_slots_starters_go_to_the_top_projected_at_each_position():
    """A third QB beyond the two starting slots goes to bench, ranked below
    both starters regardless of draft order."""
    roster = [
        draft_state.Purchase("Third QB", "QB", 5, "MARK"),
        draft_state.Purchase("Best QB", "QB", 60, "MARK"),
        draft_state.Purchase("Second QB", "QB", 40, "MARK"),
    ]
    projected = _by_name(**{"Best QB": 400, "Second QB": 350, "Third QB": 200})
    slots = draft_state.lineup_slots(roster, projected)

    qb_rows = [p.player for label, p in slots if label == "QB"]
    bench_rows = [p.player for label, p in slots if label == "BE" and p]
    assert qb_rows == ["Best QB", "Second QB"]
    assert bench_rows == ["Third QB"]


def test_lineup_slots_flex_takes_the_best_leftover_after_direct_slots_fill():
    """FLEX only claims a player once RB, WR and TE's own starting slots are
    already spoken for -- the third RB here outranks the TE but still lands
    in FLEX, not ahead of either starting RB."""
    roster = [
        draft_state.Purchase("RB1", "RB", 50, "MARK"),
        draft_state.Purchase("RB2", "RB", 40, "MARK"),
        draft_state.Purchase("RB3", "RB", 30, "MARK"),
        draft_state.Purchase("WR1", "WR", 45, "MARK"),
        draft_state.Purchase("WR2", "WR", 35, "MARK"),
        draft_state.Purchase("TE1", "TE", 10, "MARK"),
    ]
    projected = _by_name(RB1=300, RB2=250, RB3=200, WR1=280, WR2=260, TE1=150)
    slots = draft_state.lineup_slots(roster, projected)

    by_label = {label: (p.player if p else None) for label, p in slots
                if label in ("RB", "WR", "TE", "FLEX")}
    rb_rows = [p.player for label, p in slots if label == "RB"]
    assert rb_rows == ["RB1", "RB2"]
    assert by_label["FLEX"] == "RB3"


def test_lineup_slots_never_gives_flex_to_a_kicker_or_defense():
    """K and D/ST aren't in config.FLEX_ELIGIBLE, so a lone kicker fills the
    K slot and leaves FLEX empty rather than borrowing it."""
    roster = [draft_state.Purchase("Some Kicker", "K", 1, "MARK")]
    slots = draft_state.lineup_slots(roster, _by_name(**{"Some Kicker": 100}))
    by_label = dict((label, p.player if p else None) for label, p in slots
                    if label in ("K", "FLEX"))
    assert by_label["K"] == "Some Kicker"
    assert by_label["FLEX"] is None


def test_lineup_slots_ranks_an_unprojected_player_last_at_his_position():
    """A drafted player missing from the sheet (cut, renamed, a typo) still
    fills his position's starting slot -- position decides eligibility, the
    projection only orders within it -- but sorts behind anyone with a real
    number."""
    roster = [
        draft_state.Purchase("Known QB", "QB", 55, "MARK"),
        draft_state.Purchase("Unmatched QB", "QB", 30, "MARK"),
    ]
    projected = _by_name(**{"Known QB": 380})  # "Unmatched QB" has no entry
    slots = draft_state.lineup_slots(roster, projected)
    qb_rows = [p.player for label, p in slots if label == "QB"]
    assert qb_rows == ["Known QB", "Unmatched QB"]


def test_lineup_slots_sends_an_unknown_position_to_the_bench():
    """draft_sync.PlayerResolver records position "?" for a player it can't
    resolve -- that matches no starting slot, so it falls to the bench like
    any other leftover instead of raising or silently vanishing."""
    roster = [draft_state.Purchase("Mystery Player", "?", 1, "MARK")]
    slots = draft_state.lineup_slots(roster, _by_name(**{"Mystery Player": 999}))
    assert all(p is None for label, p in slots if label != "BE")
    bench_players = [p.player for label, p in slots if label == "BE" and p]
    assert bench_players == ["Mystery Player"]


def test_lineup_slots_never_drops_a_purchase_past_roster_size():
    """A roster past the normal 16 (a corrupt state, or one of the
    POSITION_MAX overstacks) grows the bench instead of hiding anyone --
    2 WRs go to the WR starting slots, 1 to FLEX, and all 14 remaining
    overflow the usual 6 bench rows rather than dropping any of them."""
    roster = [draft_state.Purchase(f"WR{i}", "WR", 1, "MARK") for i in range(17)]
    projected = _by_name(**{f"WR{i}": 100 - i for i in range(17)})
    slots = draft_state.lineup_slots(roster, projected)
    bench_rows = [label for label, _ in slots if label == "BE"]
    assert len(bench_rows) == 14
    seen = {p.player for _, p in slots if p is not None}
    assert seen == {f"WR{i}" for i in range(17)}
