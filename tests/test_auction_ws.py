"""Unit tests for the pure live-auction logic in scripts/auction.py:
WsAuctionPointer, apply_ws_event, clock_milestone, and evaluate_bid. None
of this touches a socket or a thread -- DraftRoomClient's I/O is covered
separately in tests/test_draft_ws_client.py.
"""

from __future__ import annotations

import auction
from ff import draft_state, draft_ws, values


def test_bid_event_sets_pointer():
    pointer = auction.WsAuctionPointer(nominating_team=6)
    updated = auction.apply_ws_event(pointer, draft_ws.Bid(10, 3915511, 42, 25000, 12731))
    assert updated == auction.WsAuctionPointer(3915511, 42, 6)


def test_clock_state_2_sets_pointer():
    pointer = auction.WsAuctionPointer()
    event = draft_ws.Clock(2, 12982, high_bid_team=11, player_id=3915511, high_bid_amount=41)
    updated = auction.apply_ws_event(pointer, event)
    assert updated == auction.WsAuctionPointer(3915511, 41, None)


def test_nomination_event_sets_nominating_team_and_clears_player():
    pointer = auction.WsAuctionPointer(3915511, 43, None)
    updated = auction.apply_ws_event(pointer, draft_ws.Nomination(7, 25000))
    assert updated == auction.WsAuctionPointer(None, 0, 7)


def test_clock_state_1_clears_player_and_sets_nominating_team():
    pointer = auction.WsAuctionPointer(3915511, 43, None)
    event = draft_ws.Clock(1, 25000, nominating_team=7)
    updated = auction.apply_ws_event(pointer, event)
    assert updated == auction.WsAuctionPointer(None, 0, 7)


def test_sold_resets_pointer():
    pointer = auction.WsAuctionPointer(3915511, 43, None)
    updated = auction.apply_ws_event(pointer, draft_ws.Sold(7, 3915511, 1, 43, 0))
    assert updated == auction.WsAuctionPointer(None, 0, None)


def test_clock_state_3_leaves_pointer_unchanged():
    pointer = auction.WsAuctionPointer(None, 0, None)
    updated = auction.apply_ws_event(pointer, draft_ws.Clock(3, 1248))
    assert updated is pointer


def test_init_resets_pointer_to_idle():
    # INIT's header carries an in-flight nomination, but which words hold it
    # wasn't decodable unambiguously across samples -- see
    # docs/notes/ws-protocol.md. Reset to idle rather than guess; the CLOCK
    # frame that follows every INIT within a frame or two restores it.
    pointer = auction.WsAuctionPointer(3915511, 43, 6)
    updated = auction.apply_ws_event(pointer, draft_ws.Init("somejunk"))
    assert updated == auction.WsAuctionPointer()


def test_unrelated_event_leaves_pointer_unchanged():
    pointer = auction.WsAuctionPointer(3915511, 43, 6)
    updated = auction.apply_ws_event(pointer, draft_ws.BidAck(6, 4426348, 56))
    assert updated is pointer


def test_clock_milestone_announces_each_threshold_once():
    announced: set[int] = set()
    assert auction.clock_milestone(11000, announced) is None
    assert auction.clock_milestone(9500, announced) == 10
    assert auction.clock_milestone(9000, announced) is None
    assert auction.clock_milestone(4800, announced) == 5
    assert auction.clock_milestone(1000, announced) is None


def test_evaluate_bid_no_amount_bids_high_plus_one():
    plan = auction.evaluate_bid([], current_high=40, my_max_bid=100, adjusted_value=45)
    assert plan == auction.BidReady(41)


def test_evaluate_bid_explicit_amount():
    plan = auction.evaluate_bid(["45"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert plan == auction.BidReady(45)


def test_evaluate_bid_refuses_non_numeric():
    plan = auction.evaluate_bid(["forty"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert isinstance(plan, auction.BidRefused)


def test_evaluate_bid_refuses_at_or_below_current_high():
    plan = auction.evaluate_bid(["40"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert isinstance(plan, auction.BidRefused)


def test_evaluate_bid_refuses_above_max_bid():
    plan = auction.evaluate_bid(["101"], current_high=40, my_max_bid=100, adjusted_value=200)
    assert isinstance(plan, auction.BidRefused)


def test_evaluate_bid_confirms_big_jump_over_current_high():
    plan = auction.evaluate_bid(["55"], current_high=40, my_max_bid=100, adjusted_value=200)
    assert isinstance(plan, auction.BidNeedsConfirmation)
    assert plan.amount == 55


def test_evaluate_bid_confirms_far_over_sheet_value():
    plan = auction.evaluate_bid(["46"], current_high=40, my_max_bid=100, adjusted_value=20)
    assert isinstance(plan, auction.BidNeedsConfirmation)


def test_evaluate_bid_ready_when_small_jump_and_near_sheet():
    plan = auction.evaluate_bid(["41"], current_high=40, my_max_bid=100, adjusted_value=45)
    assert plan == auction.BidReady(41)


def test_evaluate_bid_ready_at_exact_jump_boundary():
    # jump == TYPO_GUARD_JUMP exactly ($10): only "more than" $10 should confirm.
    plan = auction.evaluate_bid(["50"], current_high=40, my_max_bid=100, adjusted_value=None)
    assert plan == auction.BidReady(50)


def test_evaluate_bid_ready_at_exact_sheet_multiple_boundary():
    # amount == adjusted_value * TYPO_GUARD_SHEET_MULTIPLE exactly (1.5x): only
    # "exceeds" should confirm. Keep the jump small so only the sheet-multiple
    # check is in play.
    plan = auction.evaluate_bid(["30"], current_high=25, my_max_bid=100, adjusted_value=20)
    assert plan == auction.BidReady(30)


# --- next_equivalent / bid_verdict -------------------------------------

def _val(name, position, value, tier):
    return values.Valuation(
        name=name, position=position, pro_team="", projected_points=0.0,
        replacement_points=0.0, vorp=0.0, value=value, tier=tier,
    )


BOARD = [
    _val("Bijan Robinson", "RB", 43, 2),
    _val("Kenneth Walker III", "RB", 38, 2),
    _val("Tony Pollard", "RB", 22, 3),
    _val("Justin Jefferson", "WR", 52, 1),
]


def test_next_equivalent_prefers_the_same_tier():
    match = auction.next_equivalent(BOARD, set(), "RB", 2, "Bijan Robinson")
    assert match.name == "Kenneth Walker III"


def test_next_equivalent_falls_through_to_the_next_tier_down():
    taken = {"Kenneth Walker III"}
    match = auction.next_equivalent(BOARD, taken, "RB", 2, "Bijan Robinson")
    assert match.name == "Tony Pollard"
    assert match.tier == 3


def test_next_equivalent_never_returns_a_better_tier():
    match = auction.next_equivalent(BOARD, set(), "WR", 2, None)
    assert match is None


def test_next_equivalent_returns_none_when_the_position_is_exhausted():
    taken = {"Kenneth Walker III", "Tony Pollard"}
    assert auction.next_equivalent(BOARD, taken, "RB", 2, "Bijan Robinson") is None


# --- QB bye clash (T8) --------------------------------------------------

def _qb(name, bye):
    return values.Valuation(
        name=name, position="QB", pro_team="", projected_points=0.0,
        replacement_points=0.0, vorp=0.0, value=1, bye=bye,
    )


QB_BOARD = [_qb("Josh Allen", 7), _qb("Lamar Jackson", 7), _qb("Jayden Daniels", 12)]


def test_rostered_qb_bye_clash_finds_a_shared_bye():
    state = draft_state.DraftState(my_team="ME")
    state.record("Josh Allen", "QB", 60, "ME")
    state.record("Lamar Jackson", "QB", 40, "ME")
    assert auction.rostered_qb_bye_clash(state, "ME", QB_BOARD) == 7


def test_rostered_qb_bye_clash_is_none_when_byes_differ():
    state = draft_state.DraftState(my_team="ME")
    state.record("Josh Allen", "QB", 60, "ME")
    state.record("Jayden Daniels", "QB", 39, "ME")
    assert auction.rostered_qb_bye_clash(state, "ME", QB_BOARD) is None


def test_qb_bye_would_clash_checks_a_candidate_before_buying():
    state = draft_state.DraftState(my_team="ME")
    state.record("Josh Allen", "QB", 60, "ME")
    lamar = next(v for v in QB_BOARD if v.name == "Lamar Jackson")
    daniels = next(v for v in QB_BOARD if v.name == "Jayden Daniels")
    assert auction.qb_bye_would_clash(state, "ME", QB_BOARD, lamar) is True
    assert auction.qb_bye_would_clash(state, "ME", QB_BOARD, daniels) is False


def test_me_table_footer_still_wants_the_third_qb_after_starters_are_filled():
    """T8: needs() alone says "all starting slots filled" at two QBs -- the
    `me` footer (used by both the REST console and --ws's :me command) has
    to keep pointing at the third one, not declare victory."""
    state = draft_state.DraftState(my_team="ME")
    state.record("Josh Allen", "QB", 60, "ME")
    state.record("Lamar Jackson", "QB", 40, "ME")
    for pos in ("RB", "RB", "WR", "WR", "TE", "D/ST", "K"):
        state.record(f"Filler {pos} {state.roster_count('ME')}", pos, 1, "ME")
    _, footer = auction.me_table(state, QB_BOARD)
    assert "All starting slots filled" in footer
    assert "QB x1" in footer


def test_qb_bye_would_clash_ignores_non_qb_and_bye_free_players():
    state = draft_state.DraftState(my_team="ME")
    state.record("Josh Allen", "QB", 60, "ME")
    non_qb = values.Valuation(name="Some RB", position="RB", pro_team="",
                              projected_points=0.0, replacement_points=0.0,
                              vorp=0.0, value=1, bye=7)
    no_bye = values.Valuation(name="No Bye QB", position="QB", pro_team="",
                              projected_points=0.0, replacement_points=0.0,
                              vorp=0.0, value=1, bye=None)
    assert auction.qb_bye_would_clash(state, "ME", QB_BOARD, non_qb) is False
    assert auction.qb_bye_would_clash(state, "ME", QB_BOARD, no_bye) is False


def test_bid_verdict_bands():
    assert auction.bid_verdict(30, 40).label == "good value"
    assert auction.bid_verdict(40, 40).label == "fair"
    assert auction.bid_verdict(50, 40).label == "pricey"
    assert auction.bid_verdict(70, 40).label == "overpaying"


def test_bid_verdict_boundaries_match_the_market_and_typo_guard_cutoffs():
    # 0.9 and 1.1 are the 'market' read's cutoffs; 1.5 is TYPO_GUARD_SHEET_MULTIPLE.
    assert auction.bid_verdict(36, 40).label == "fair"        # exactly 0.9
    assert auction.bid_verdict(44, 40).label == "fair"        # exactly 1.1
    assert auction.bid_verdict(60, 40).label == "pricey"      # exactly 1.5
    assert auction.bid_verdict(61, 40).label == "overpaying"


# --- nomination_board / save_nomination_list ---------------------------

def _board_val(name, position, value, tier, bye=None, espn_avg=None, projected_points=0.0):
    return values.Valuation(
        name=name, position=position, pro_team="", projected_points=projected_points,
        replacement_points=0.0, vorp=0.0, value=value, tier=tier,
        bye=bye, espn_avg=espn_avg,
    )


BIG_BOARD = [
    _board_val("Josh Allen", "QB", 40, 1, bye=7, espn_avg=33.76, projected_points=400.0),
    _board_val("Lamar Jackson", "QB", 33, 1, bye=8, espn_avg=45.0, projected_points=380.0),
    _board_val("Bijan Robinson", "RB", 43, 2, bye=5, espn_avg=60.0, projected_points=300.0),
    _board_val("Kenneth Walker III", "RB", 38, 2, bye=10, espn_avg=28.0, projected_points=280.0),
    _board_val("Justin Jefferson", "WR", 52, 1, bye=6, espn_avg=55.0, projected_points=310.0),
]


def _board_state(taken_players=()):
    state = draft_state.DraftState(my_team="ME")
    for i, name in enumerate(taken_players):
        state.purchases.append(
            draft_state.Purchase(player=name, position="RB", price=1, team="ME")
        )
    return state


def test_nomination_board_hides_taken_players():
    state = _board_state(["Josh Allen"])
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.0)
    assert "Josh Allen" not in [r.name for r in rows]
    assert len(rows) == len(BIG_BOARD) - 1


def test_nomination_board_availability_is_case_insensitive():
    # taken() stores names as recorded; the board must not show a player
    # back to back with a different-case spelling of an already-sold name.
    state = _board_state(["josh allen"])
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.0)
    assert "Josh Allen" not in [r.name for r in rows]


def test_nomination_board_applies_inflation_to_adjusted():
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.5)
    allen = next(r for r in rows if r.name == "Josh Allen")
    assert allen.adjusted == 60
    assert allen.edge == 40 - 60


def test_nomination_board_query_filters_by_substring_case_insensitive():
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.0, query="jack")
    assert [r.name for r in rows] == ["Lamar Jackson"]


def test_nomination_board_position_filter():
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.0, position="rb")
    assert {r.name for r in rows} == {"Bijan Robinson", "Kenneth Walker III"}


def test_nomination_board_position_filter_flex_matches_rb_wr_te():
    board = BIG_BOARD + [_board_val("Travis Kelce", "TE", 25, 3, bye=9)]
    state = _board_state()
    rows = auction.nomination_board(board, state, inflation=1.0, position="flex")
    assert {r.name for r in rows} == {
        "Bijan Robinson", "Kenneth Walker III", "Justin Jefferson", "Travis Kelce",
    }


def test_nomination_board_starred_only():
    state = _board_state()
    rows = auction.nomination_board(
        BIG_BOARD, state, inflation=1.0,
        starred={"Josh Allen"}, starred_only=True,
    )
    assert [r.name for r in rows] == ["Josh Allen"]
    assert rows[0].starred is True


def test_nomination_board_starred_rows_lead_regardless_of_sort():
    state = _board_state()
    rows = auction.nomination_board(
        BIG_BOARD, state, inflation=1.0,
        starred={"Kenneth Walker III"}, sort="rank",
    )
    assert rows[0].name == "Kenneth Walker III"


def test_nomination_board_sort_rec_orders_by_edge_descending():
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=0.5, sort="rec")
    edges = [r.edge for r in rows]
    assert edges == sorted(edges, reverse=True)


def test_nomination_board_sort_bye():
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.0, sort="bye")
    byes = [r.valuation.bye for r in rows]
    assert byes == sorted(byes, reverse=True)


def test_nomination_board_sort_proj():
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.0, sort="proj")
    proj = [r.valuation.projected_points for r in rows]
    assert proj == sorted(proj, reverse=True)


def test_nomination_board_sort_name_ascending():
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=1.0, sort="name")
    names = [r.name for r in rows]
    assert names == sorted(names)


def test_nomination_board_none_inflation_gives_none_adjusted_and_edge():
    """T36a: a plain None (the whole-board "no read" case) must leave every
    row's adjusted/edge as None rather than crashing on `value * None`."""
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=None)
    assert all(r.adjusted is None for r in rows)
    assert all(r.edge is None for r in rows)


def test_nomination_board_dict_rate_falls_back_to_none_overall():
    """T36a: a per-position dict with a real QB rate but no read for the
    other positions (dict fallback recomputes the overall rate, which can
    itself be None) -- QB rows still price, everything else reads None
    rather than silently reusing a stale or wrong number."""
    state = _board_state()
    state.spots_left = lambda team: 0                        # type: ignore[method-assign]
    state.all_teams = lambda: ["ME"]                          # type: ignore[method-assign]
    state.budget_left = lambda team: 50                       # type: ignore[method-assign]

    rows = auction.nomination_board(BIG_BOARD, state, inflation={"QB": 1.2})
    allen = next(r for r in rows if r.name == "Josh Allen")
    assert allen.adjusted == 48
    assert allen.edge == 40 - 48

    bijan = next(r for r in rows if r.name == "Bijan Robinson")
    assert bijan.adjusted is None
    assert bijan.edge is None


def test_nomination_board_sort_adj_and_rec_tolerate_none_rates():
    """Sort keys fall back to `or 0` for None adjusted/edge -- a whole-board
    no-read state must still sort (all rows tie, which is fine) instead of
    raising on `None < int`."""
    state = _board_state()
    rows = auction.nomination_board(BIG_BOARD, state, inflation=None, sort="adj")
    assert len(rows) == len(BIG_BOARD)
    rows = auction.nomination_board(BIG_BOARD, state, inflation=None, sort="rec")
    assert len(rows) == len(BIG_BOARD)


def test_save_nomination_list_round_trips(tmp_path):
    path = tmp_path / "nomination-list.txt"
    auction.save_nomination_list(path, ["Josh Allen", "Bijan Robinson"])
    assert auction.load_nomination_list(path) == ["Josh Allen", "Bijan Robinson"]


def test_save_nomination_list_empty_list_writes_empty_file(tmp_path):
    path = tmp_path / "nomination-list.txt"
    auction.save_nomination_list(path, [])
    assert path.read_text() == ""
    assert auction.load_nomination_list(path) == []


def test_bid_verdict_without_a_sheet_value_is_neutral():
    verdict = auction.bid_verdict(40, None)
    assert verdict.label == "no read"
    assert verdict.style == "dim"


def test_bid_verdict_styles_run_green_to_red():
    assert auction.bid_verdict(30, 40).style == "green"
    assert auction.bid_verdict(40, 40).style == "dim"
    assert auction.bid_verdict(50, 40).style == "yellow"
    assert auction.bid_verdict(70, 40).style == "red"


def test_evaluate_bid_refuses_when_you_already_hold_the_high():
    plan = auction.evaluate_bid([], 40, 100, 50, already_high=True)
    assert isinstance(plan, auction.BidRefused)
    assert "already hold" in plan.reason


def test_load_state_fresh_ignores_an_existing_file(tmp_path):
    path = tmp_path / "draft-state.json"
    draft_state.DraftState(
        purchases=[draft_state.Purchase("Lamar Jackson", "QB", 62, "FWD")],
        state_path=path,
    ).save()
    state = auction.load_state(fresh=True, path=path)
    assert state.purchases == []


def test_load_state_not_fresh_loads_existing_data(tmp_path):
    path = tmp_path / "draft-state.json"
    draft_state.DraftState(
        purchases=[draft_state.Purchase("Lamar Jackson", "QB", 62, "FWD")],
        state_path=path,
    ).save()
    state = auction.load_state(fresh=False, path=path)
    assert state.purchases[0].player == "Lamar Jackson"


def test_load_state_fresh_backs_up_the_existing_file(tmp_path):
    path = tmp_path / "draft-state.json"
    draft_state.DraftState(
        purchases=[draft_state.Purchase("Lamar Jackson", "QB", 62, "FWD")],
        state_path=path,
    ).save()
    auction.load_state(fresh=True, path=path)
    backup = path.with_suffix(".json.bak")
    assert backup.exists()
    assert "Lamar Jackson" in backup.read_text()


def test_load_state_fresh_without_an_existing_file_does_not_create_a_backup(tmp_path):
    path = tmp_path / "draft-state.json"
    auction.load_state(fresh=True, path=path)
    backup = path.with_suffix(".json.bak")
    assert not backup.exists()


# --- reconcile_init -------------------------------------------------------

class _FakeResolver:
    """Stands in for draft_sync.PlayerResolver: a fixed id -> (name,
    position) map, with no live-API fallback, so reconcile tests never touch
    the network."""

    def __init__(self, by_id: dict[int, tuple[str, str]]):
        self._by_id = by_id

    def resolve(self, player_id: int) -> tuple[str, str]:
        return self._by_id.get(player_id, (f"ESPN#{player_id}", "?"))


RESOLVER = _FakeResolver({
    4241478: ("Bijan Robinson", "RB"),
    4239993: ("Puka Nacua", "WR"),
})


def _init(picks):
    return draft_ws.InitState(league_id=999, picks=tuple(picks))


def test_reconcile_init_adds_sales_the_console_never_witnessed(tmp_path):
    state = draft_state.DraftState(state_path=tmp_path / "draft-state.json")
    init = _init([draft_ws.InitPick(pick_number=1, team_id=2, player_id=4241478, price=10)])

    report = auction.reconcile_init(state, init, RESOLVER)

    assert len(report.added) == 1
    added = report.added[0]
    assert (added.player, added.team, added.price) == ("Bijan Robinson", "AUBREY", 10)
    assert report.corrected == ()
    assert report.removed == ()
    assert [p.player for p in state.purchases] == ["Bijan Robinson"]


def test_reconcile_init_is_a_no_op_when_state_already_matches(tmp_path):
    path = tmp_path / "draft-state.json"
    state = draft_state.DraftState(
        purchases=[draft_state.Purchase("Bijan Robinson", "RB", 10, "AUBREY", 4241478)],
        state_path=path,
    )
    init = _init([draft_ws.InitPick(pick_number=1, team_id=2, player_id=4241478, price=10)])

    report = auction.reconcile_init(state, init, RESOLVER)

    assert report == auction.ReconcileReport((), (), ())
    assert len(state.purchases) == 1


def test_reconcile_init_overwrites_a_conflicting_local_purchase(tmp_path):
    path = tmp_path / "draft-state.json"
    state = draft_state.DraftState(
        purchases=[draft_state.Purchase("Bijan Robinson", "RB", 8, "ME", 4241478)],
        state_path=path,
    )
    init = _init([draft_ws.InitPick(pick_number=1, team_id=2, player_id=4241478, price=10)])

    report = auction.reconcile_init(state, init, RESOLVER)

    assert len(report.corrected) == 1
    fixed = report.corrected[0]
    assert (fixed.team, fixed.price) == ("AUBREY", 10)
    assert len(state.purchases) == 1
    assert (state.purchases[0].team, state.purchases[0].price) == ("AUBREY", 10)


def test_reconcile_init_removes_a_purchase_the_server_does_not_have(tmp_path):
    path = tmp_path / "draft-state.json"
    state = draft_state.DraftState(
        purchases=[draft_state.Purchase("Ghost Player", "RB", 5, "ME")],
        state_path=path,
    )
    init = _init([])  # nothing sold according to the server

    report = auction.reconcile_init(state, init, RESOLVER)

    assert len(report.removed) == 1
    assert report.removed[0].player == "Ghost Player"
    assert state.purchases == []


def test_reconcile_init_matches_a_hand_typed_purchase_by_name(tmp_path):
    # state.record() (the ":record"/plain-typed path) leaves espn_pick_id
    # unset -- reconcile must still recognize it as the same sale rather
    # than treating it as both a duplicate add and a stale local extra.
    path = tmp_path / "draft-state.json"
    state = draft_state.DraftState(state_path=path)
    state.record("Bijan Robinson", "RB", 10, "AUBREY")
    init = _init([draft_ws.InitPick(pick_number=1, team_id=2, player_id=4241478, price=10)])

    report = auction.reconcile_init(state, init, RESOLVER)

    assert report.added == ()
    assert report.corrected == ()
    assert report.removed == ()
    assert len(state.purchases) == 1
    assert state.purchases[0].espn_pick_id == 4241478


def test_reconcile_init_saves_state_exactly_once(tmp_path, monkeypatch):
    path = tmp_path / "draft-state.json"
    state = draft_state.DraftState(state_path=path)
    init = _init([
        draft_ws.InitPick(pick_number=1, team_id=2, player_id=4241478, price=10),
        draft_ws.InitPick(pick_number=2, team_id=6, player_id=4239993, price=7),
    ])
    calls = []
    monkeypatch.setattr(state, "save", lambda *a, **k: calls.append(1))

    auction.reconcile_init(state, init, RESOLVER)

    assert calls == [1]
