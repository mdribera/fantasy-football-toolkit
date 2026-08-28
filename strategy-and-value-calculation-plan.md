# Strategy & value-calculation batch: T7, T36, T39, T28, T30, T32(doc), T34(doc), T37, T38

## Context

The Aug 28 value-model audit produced two signed-off items: T36 (two
live-model defects in the forward-inflation math) and T39 (strategy-doc
updates from the league's 2023-2025 auction history). Mark asked to fold
in every other open task that groups naturally with strategy and value
calculation. That adds: T28 (verdict over/under diff -- same verdict code
T36 touches), T30 (`pos flex` board filter -- same `nomination_board`
function, serves the new FLEX guidance), T34's now-answerable guidance
(same doc edit as T39), T32's runbook step (doc now, execution Sep 2),
T37 (value-model guard), and T38 (Sleeper projection source).

Mark also called T7 now rather than live on draft day: the three-year
room history is consistent enough (QB spend 20.4%/18.6%/17.2% against
the QB26 row's predicted 17.8%, effective replacement QB25-27 every
year) to commit the sheet to a **QB26 baseline** before the draft.

Excluded as not groupable: T25/T19/T4/T11/T24 (ws-protocol and console
plumbing), T26/T27/T29/T31 (UI/input polish, not value), T13/T33 (need
their own design pass and sign-off on auto-bid firing rules), T14-T18
(need season data).

Code freeze Sep 1, draft Sep 2. Work in priority order: **T7 first**
(the rebuilt QB26 board feeds every dollar figure the doc workstream
quotes), then A (live console, freeze-critical), B (docs), C (model
guard), D (projection source).
Evidence and repro details: `TASKS.md` T36 entries,
`docs/notes/auction-room-history.md`,
`docs/notes/qb-baseline-decision-aid.md`.

Mark's call on the endgame regime: **clamp the forward rate at 3.0x**,
plus the no-read sentinel at exactly zero surplus.

---

## Workstream T7: commit the QB baseline to 26 (do first)

1. `src/ff/config.py`: `ReplacementLevel.QB: 30` -> `26`. Rewrite the
   field comment and class docstring reasoning: the league's own
   2023-2025 auctions priced a QB26 baseline every year (QB spend
   17-20% vs QB26's predicted 17.8%; 25-27 QBs drafted, 4-6 teams
   stopping at two). `ROSTER_TARGETS.QB: 3` is untouched -- the
   baseline describes the market, not our own roster plan, and the
   three-QB rule stands.
2. Rebuild the board: `build_values.py --cache` (fresh pull still
   happens Sep 2 per T32). Expected anchors from the sensitivity run:
   Allen $35, Lamar $23, band QBs $13-19, Gibbs $53, Nacua $47.
3. `docs/league-analysis.md`: update the replacement-level table's QB
   row and reasoning; rewrite the "Sensitivity: the QB baseline"
   section evergreen -- the baseline is QB26 on three-year room
   evidence (cite `docs/notes/auction-room-history.md`), the
   sensitivity table remains as the swing analysis, and the live
   console's per-position market read still self-corrects if the 2026
   room deviates.
4. `docs/notes/qb-baseline-decision-aid.md`: reframe -- the sheet now
   ships at QB26, so the live question inverts: watch the first QB
   sales for evidence the room is deviating *up* toward QB30 (band QBs
   clearing $20+ with 3+ bidders means band QBs are worth more than
   sheet and the third QB must come earlier). The reference-board table
   and sub-tier tells stay; "the QB30 column" language flips to "the
   sheet column (QB26)".
5. `scripts/replacement_sensitivity.py`: docstring touch-up so it
   describes the check evergreen (baselines 22/26/30 stay; it no longer
   presumes 30 is the live default).
6. TASKS.md: tick T7's final sub-item, move T7 to Done with a one-line
   summary.

## Workstream A: live console value fixes (T36a, T36b, T28, T30)

### T36a: endgame forward-inflation fix

`src/ff/draft_state.py`

- Add module constants `FORWARD_RATE_MAX = 3.0` and
  `TILT_EVIDENCE_DOLLARS = 25`.
- `forward_inflation()` returns `float | None`:
  - surplus > 0: `min(FORWARD_RATE_MAX, biddable / surplus)`
  - surplus == 0, biddable > 0: `None` (no read -- no priced pool left
    to compare the money against)
  - surplus == 0, biddable == 0: `1.0` (draft over, harmless display)
  - Docstring: None means "no read", and why.
- `forward_inflation_by_position()`: base None -> return `{}`.

Consumers of the now-optional rate:

- `scripts/ws_console.py`
  - `_adjusted()` (~557) returns `int | None`: None when no match OR
    rate is None (currently returns `0` for no match).
  - `_sync_pointer()` (~539): `StatusPanel.adjusted_value` and `edge`
    reactives default to `None`; `render()` (~126) shows `Adjusted -` /
    `Edge -` when None.
  - `_refresh_analysis()` (~686) and `_start_bid()` (~727) already
    tolerate None via `bid_verdict`/`evaluate_bid`'s existing None
    paths.
- `scripts/auction.py`
  - `bid_verdict()` (~307): falsy-adjusted label changes `"unpriced"`
    -> `"no read"` (it only renders for matched players whose market
    read is gone; unmatched players never reach the verdict line).
    Style stays `dim`.
  - `nomination_board()` (~505): `overall` and per-row `rate` may be
    None. `BoardRow.adjusted`/`.edge` become `int | None`; sort keys
    use `or 0`.
  - `ws_console._board_cells()` (~804): render `-` for None
    adjusted/edge (existing `-` convention for espn_avg/bye).
  - `best_table()` (~641) / `market_table()` (~691): guard the
    `x{overall:.2f}` title formats ("no read") and per-row rates
    (`-` cells); `market_table` with `{}` rates gets a dim
    "no per-position read yet" row.

### T36b: evidence-weighted tilt

`src/ff/draft_state.py`

- New private helper `_position_paid_modeled(valuations) ->
  dict[str, tuple[int, int]]`; refactor `inflation_by_position()` to
  derive rates from it (behavior unchanged).
- `forward_inflation_by_position()`: shrink the tilt toward 1.0 by
  modeled-dollar evidence before clamping:

      w = modeled / (modeled + TILT_EVIDENCE_DOLLARS)
      tilt = clamp(1 + w * (rate_pos / overall_backward - 1), *tilt_bounds)

  Keep the `tilt_bounds` (0.5, 2.0) parameter as the outer bound.
  Docstring: one bid-up cheap QB must not double the QB board.

Sanity check (audit repro): one $3 QB sold at $9 plus five at-sheet
non-QB sales -> modeled=$3, w~0.11, raw ratio ~2.9 -> tilt ~1.2 (was
clamped 2.0); Allen Adjusted ~$48 instead of $80.

### T28: verdict shows the over/under diff

`scripts/ws_console.py` `_refresh_analysis()` (~687): verdict line
becomes e.g. `Verdict: pricey by +$6 at $41` where the diff is
`high_bid - adjusted` -- computed against the **Adjusted** value, the
same baseline the verdict ratio itself uses (the T28 example quotes
Sheet, but Sheet and Adjusted coincide with no market data; using the
verdict's own baseline keeps label and number consistent on an inflated
board). Negative diff for a good-value read (`good value by -$4`).
When adjusted is None (T36a) the line is `Verdict: no read` with no
diff. Keep `Verdict.label`/`style` as-is; the diff is formatted at the
call site.

### T30: `pos flex` board filter

- `scripts/auction.py` `nomination_board()` (~535): when
  `position.upper() == "FLEX"`, filter to
  `v.position in config.FLEX_ELIGIBLE` (RB/WR/TE) instead of equality.
- `scripts/ws_console.py`: extend the `:pos` command's accepted values
  and the board title/status line to include `flex` (find the `:pos`
  handler and any position validation near the command dispatch).

## Workstream B: docs (T39, T34 guidance, T32 runbook step)

### T39 + T34: `docs/auction-strategy.md`

Content per the signed-off T39 spec in TASKS.md, sourced from
`docs/notes/auction-room-history.md`. Evergreen voice, no em dashes.
**Every dollar figure in the doc is re-quoted from the rebuilt QB26
board** (Workstream T7): the budget-allocation table from the new
`build_values.py --plan` output, the QB band prices, Allen's price and
walk-away, the league-wide position totals, and the QB-pair table's
costs (the points are projection-invariant, the dollars are not).

1. **The quarterback decision**: the room's three-year pattern (exactly
   five QBs clear $30+ every year, then the shelf; QB spend 17-20%
   every year, the QB26 row); third-QB timing rule; pointer to
   `docs/notes/qb-baseline-decision-aid.md` for the live baseline call.
2. **Where the value is**: QB savings flood into WR (39% share vs the
   model's 29%, three years running) while RB share matches the model;
   under the QB26 rebase elite RBs at $50-53 are fair, RB6-15 at $30 is
   the fairest band on the board, WR is the one genuine overpay market.
   Qualify the crash advice: the crash sells rank-16-30 depth at $6-12,
   not starters -- engage the mid-band $26-32 shelf in the first 40-50
   picks.
3. **Tight end is a punt**: someone pays $25+ for the top TE every
   year; let them.
4. **Nomination tactics**: sharpen to the elite QB names specifically,
   plus the top TE and elite WRs as budget drains; keep quiet about
   mid-band QBs and RB6-15.
5. **T34 (the answerable half), same doc**: early-overbid guidance --
   the front-loading is structural (63-72% of money in the first 40
   picks, three years running), so a broadly negative early
   Adjusted/Edge readout is the room's normal pace, not a blip. The
   discriminator: backward rate high while forward rate sits below ~1
   across 5+ sales spanning 2+ positions means the back half clears
   under sheet by budget conservation -- wait it out; a single hot sale
   barely moves either aggregate. T34's residual (validate against the
   2026 draft's own curve) stays open in TASKS.md.

### T32: runbook step (doc now, execution Sep 2)

`docs/draft-day-runbook.md`: add the morning-of step next to the
code-freeze note -- rerun `build_values.py --plan` with a fresh
(non-`--cache`) ESPN pull, then rerun
`scripts/replacement_sensitivity.py` so the decision aid's reference
boards are current. T32 itself stays open until executed Sep 2.

## Workstream C: T37 value-model guard

`src/ff/values.py` `compute_values()`: raise `ValueError` (clear
message) when `sum(replacement.rank_for(pos) - 1 for pos in positions)`
exceeds `config.TOTAL_ROSTER_SPOTS` -- the condition under which the
position-blind top-N clip would silently zero real players' surplus.
Runs at board-build time (`build_values.py`), never during the live
draft. Test in `tests/test_values_replacement.py`.

## Workstream D: T38 Sleeper projection source

`src/ff/sleeper.py`: add `season_projections(season, positions=("QB",
"RB", "WR", "TE")) -> list[dict]` fetching
`https://api.sleeper.com/projections/nfl/{season}?season_type=regular&position[]=...`
per position. Map stat keys to `scoring.py` names (`pass_yd` ->
`passing_yards`, `pass_td` -> `passing_tds`, `pass_int` ->
`interceptions`, `rush_yd`/`rush_td`, `rec`/`rec_yd`/`rec_td`,
`fum_lost`, 2pt variants); return
`{name, position, pro_team, stats, adp_2qb}` rows. Follow the module's
existing conventions (plain dicts, `data/cache/` caching like the
player dump, requests session). Method reference:
`docs/notes/projection-crosscheck-sleeper.md`. Unit test with a canned
fixture (no live call in tests).

## Tests

`tests/test_draft_state.py` (extends the T6 pins, same
`make_valuations` + method-stubbing pattern):

- Update `test_forward_inflation_by_position_tilts_by_the_backward_read`:
  the exact-equality assertion (~line 107) becomes the shrinkage formula
  (modeled=$40 -> w=40/65); keep the directional `QB > RB` assert.
- New: zero surplus + cash -> None and `{}`; tiny surplus (surplus $5,
  biddable $150) -> clamps at 3.0; biddable 0 + surplus 0 -> 1.0; the
  T36b cheap-QB repro -> QB rate < base * 1.4; evidence growth -> tilt
  approaches the raw ratio.

Elsewhere: `nomination_board` flex filter and None-rate rows
(`tests/test_auction_ws.py`), verdict "no read"/diff line and StatusPanel
None rendering (`tests/test_ws_console.py` -- grep for "unpriced",
"Verdict:", `adjusted_value` assertions and update), T37 test, T38
fixture test.

## TASKS.md bookkeeping (inline, as each lands)

Tick and move to Done with one-line summaries: T36, T39, T28, T30, T37,
T38. T32 stays open with a note (runbook step written; execution Sep 2
morning). T34 gets a note (answerable half folded into
auction-strategy.md; residual: validate against the 2026 draft's curve
post-draft).

## Verification

1. `.venv/bin/python -m pytest tests/` -- full suite green.
2. `.venv/bin/python scripts/selftest.py` -- still passes.
3. After the T7 flip, `build_values.py --cache` output matches the
   sensitivity run's QB26 anchors (Allen $35, Lamar $23, Gibbs $53,
   Nacua $47) and the T37 guard passes on the new config
   (sum of ranks - 6 = 137 <= 160). `replacement_sensitivity.py` still
   runs and its QB26 column matches the live board.
4. Re-run the audit's regime simulations against the fixed code:
   endgame regime shows "no read", not 1.0; tiny-surplus shows 3.0, not
   30; cheap-QB regime shows QB tilt ~1.2, not clamped 2.0.
5. Replay smoke test of the ws console against the 2026-08-27 rehearsal
   capture (`data/ws-log-1787888473.jsonl`, the harness
   `tests/test_ws_console.py` already uses): healthy mid-draft rates,
   so panels should render unchanged -- a regression check. Also
   exercise `:pos flex` and the verdict diff line in that replay.
6. `season_projections(2026, ("QB",))` one-off live smoke: row count
   ~350, Josh Allen present with a scoreable stat line.
7. Docs: proofread for em dashes and stale numbers; every claim traces
   to `docs/notes/auction-room-history.md`.
