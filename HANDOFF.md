# Handoff

## Context

Repo: `/home/mark/dev/fantasy-football`. Tooling for Mark's ESPN fantasy team
**The QB's Knees** (team ID 6) in the SuperFun Football League, league ID
541797.

**The draft is Sep 2, 2026, 12:00 PM PDT** -- a salary cap (auction) draft,
$200 budget, 16 roster spots. Everything is currently oriented around that
deadline. Today is Aug 26, 2026.

Read `CLAUDE.md`, then `docs/league-analysis.md`, before doing anything.

## The domain insight the whole project is built on

The league requires **two starting quarterbacks** but scores **4 points per
passing touchdown** and **full PPR**. These pull QB value in opposite
directions:

- Scarcity is severe: 20 of ~32 startable NFL QBs start weekly, FLEX is
  RB/WR/TE only, and the in-season QB waiver wire is effectively empty.
- But 4-point passing TDs compress the top: **QB2 through QB15 span just 2.4
  points per week.** Only Josh Allen separates, and only by 3.8 pts/week.

Net result, from live projections: **Lamar Jackson + Patrick Mahomes (614 pts,
$53) beats Josh Allen + a $12 QB2 (609 pts, $52).** The elite-QB play that all
public 2QB content recommends is dominated here, because that content is
written for 6-point-TD half-PPR leagues.

Every public auction sheet and trade calculator is built for 1QB. Say so
whenever citing one.

## State: built and verified against live data

| Component | Status |
|---|---|
| `src/ff/config.py` | League constants, replacement levels, max-bid rule |
| `src/ff/scoring.py` | Exact scoring incl. D/ST yards-allowed table. Unit-checked |
| `src/ff/espn.py` | ESPN wrapper. **Live-verified** against league 541797 |
| `src/ff/sleeper.py` | Trending adds, injuries. **Live-verified** |
| `src/ff/values.py` | VORP → auction $. Produces sane board from real projections |
| `src/ff/draft_state.py` | Budgets, max bid, per-position inflation |
| `scripts/whoami.py` | Connection check. Works |
| `scripts/build_values.py` | Value board + budget plan. Works |
| `scripts/auction.py` | Live console. Simulated a 6-pick draft end to end |
| `scripts/selftest.py` | Validates model with no credentials. Passes |
| `docs/` (3 files) | Format analysis, strategy playbook, data sources |
| `.claude/skills/` (5) | draft-prep, live-auction, waiver-faab, start-sit, trade-eval |

`.env` is filled in and gitignored. **Nothing has been committed yet** -- the
repo is initialized but has no commits. Mark asked to be asked before
committing.

## Next steps

These checkboxes are the progress record across sessions. Tick items as they
land, and leave a dated note under anything partially done so the next session
knows where it stopped. Items are ordered by deadline, not by size.

### Before the draft (Sep 2, 2026)

- [ ] **1. Browser automation for draft day.** The top gap. Mark chose
      "browser automation reads the draft room, terminal alongside as backup."
      The terminal side is done and tested; the automation side does not exist.
      Mid-auction is the worst possible time to debug a selector, so treat the
      rehearsal step as mandatory rather than optional.
  - [ ] Read the ESPN draft room through the `claude-in-chrome` skill
  - [ ] Extract completed purchases (player, price, buying team)
  - [ ] Feed them into `scripts/auction.py` state
  - [ ] Build instant failover to manual entry
  - [ ] Rehearse end to end against a mock draft

- [ ] **2. Validate the replacement levels.** `config.REPLACEMENT` drives every
      price in the model, and `QB: 30` is the most consequential and least
      certain assumption. It presumes all 10 teams roster three QBs; if they
      carry two, replacement moves to ~QB22 and every QB price falls.
  - [ ] Build a sensitivity table across QB 22 / 26 / 30
  - [ ] Commit to a final value and record the reasoning in `docs/league-analysis.md`

- [ ] **3. Cross-check projections.** ESPN's numbers are the only input right
      now. Compare the top ~50 against FantasyPros superflex and DraftSharks
      (links in `docs/data-sources.md`), noting that both are 12-team superflex
      rather than 10-team strict 2QB. Investigate large disagreements.

- [ ] **4. Bye-week planning.** Not built. Matters most at QB, where the third
      quarterback exists largely to cover byes. Check that a QB pairing does
      not share one.

- [ ] **5. Verify D/ST and K projections.** ESPN projects these, but it is
      unconfirmed whether its D/ST projection models this league's unusual
      yards-allowed table (down to -7 for 550+ yards). `scoring.py` implements
      it; nothing has validated ESPN against it. Low stakes at $1 per unit, but
      relevant to streaming.

### Once the season starts

- [ ] **6. Exercise the in-season paths.** `box_scores()`, waiver flows, and
      the start-sit and waiver-faab skills have not run against real data
      because the season has not started. Expect rough edges in Week 1.
  - [ ] `box_scores()` against a live matchup
  - [ ] Waiver and FAAB flow end to end
  - [ ] `start-sit` skill against a real lineup decision

## Gotchas

- **Do not score from `projected_breakdown`.** It mixes season totals with
  per-game values under partially-translated stat keys. An earlier version did
  this and produced Josh Allen at 143 points. Use
  `projected_total_points`, which ESPN computes inside the league context and
  which already reflects this league's scoring (verified: 4-pt pass TDs, full
  PPR). `scoring.py` is for raw stat lines from *other* sources.

- **`espn_s2` rotates.** When ESPN calls start failing, re-copy the cookie
  before debugging anything else. `scripts/whoami.py` diagnoses it.

- **Team IDs skip 9.** The ten teams are IDs 1-8, 10, 11.

- **`data/` is gitignored** except `.gitkeep`. Cached pulls, `values.json` and
  draft state all live there and will not persist to the repo.

- Rebuild values with `--cache` to reuse the last ESPN pull; a fresh pull of
  600 players takes a couple of minutes.

## Mark's working preferences

From the global `CLAUDE.md`: treat him as a colleague, not "the user". Simple
over clever. Small, testable increments with him in the loop. **Discuss plans
before implementing.** Ask rather than assume. Explain the "why" behind
implementation choices. No em dashes in docs. Never commit without being asked,
and never add AI attribution to commits.
