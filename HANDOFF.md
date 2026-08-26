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
knows where it stopped. Items are ordered by priority within each deadline.

### Before the draft (Sep 2, 2026)

- [x] **1. Automated pick ingestion for draft day.** ESPN's own
      `view=mDraftDetail` endpoint returns the full 160-pick auction skeleton
      as structured JSON (player, price, buying team, per pick) in ~0.15s --
      no browser, no selectors, no DOM to break mid-auction.
  - [x] `src/ff/draft_sync.py` polls the feed, resolves ESPN player ids off
        `values.json`, and dedupes by ESPN's own stable pick id
  - [x] `scripts/auction.py` runs the poller in a background thread and
        auto-records completed picks before every prompt; `sync` forces an
        immediate pull
  - [x] Failover: `FEED DOWN -- ENTER PICKS MANUALLY` after 3 consecutive
        failed polls; manual entry (`Josh Allen 52 RIVAL`) works unchanged the
        whole time, including with `--no-sync`
  - [x] Rehearsed with a synthetic 160-pick fixture
        (`scripts/draft_sync.py --replay`): all picks import, re-running is a
        no-op, every team lands on budget with 16 filled spots

- [ ] **2. Live rehearsal against a real ESPN mock auction.** **2026-08-26:
      the risk materialized.** Recorded `scripts/draft_sync.py --record`
      against a real practice draft for this league (`leagueId=573680074`,
      ESPN's "Practice Draft for SuperFun Football League" feature, not the
      public mock lobby) while Mark drafted live in the browser. 132
      snapshots over the course of 25+ real picks landing in the draft room
      UI (with real players and prices) -- `mDraftDetail` reported **0**
      completed picks (`playerId: -1` on every pick slot) in every single
      snapshot. The recording is saved at `data/mock-recording.jsonl` for
      further inspection (gitignored, still on disk).
  - Ruled out: not a cookie/auth problem (`ESPN_S2` was re-copied fresh
    mid-test, no change; calls were already returning 200 with valid JSON
    throughout, just with empty pick data). Not a one-off caching blip --
    zero variance across 132 polls spanning several minutes.
  - Not yet ruled out: whether this is specific to *practice* drafts (maybe
    ESPN backs practice drafts with a cheaper/batch-only write path,
    distinct from a real league's live auction) vs. a general property of
    `mDraftDetail` that would also apply on Sep 2. We have no way to test
    the real thing before draft day, so this is the crux open question.
  - [ ] Confirm whether `mDraftDetail` ever backfills once a practice draft
        *completes* (replay `data/mock-recording.jsonl` isn't useful for
        this since every snapshot is empty -- need one more poll right after
        the practice draft Mark was running finishes, if it's still
        accessible, or a fresh practice draft run to completion).
  - [x] Found the endpoint the draft room's own frontend actually uses to
        stay live: **`fantasydraft.espn.com`**, a completely separate host
        from `lm-api-reads.fantasy.espn.com` that `draft_sync.py` polls
        today. Mark captured this request from his own Network tab mid-draft
        (2026-08-26):
        `https://fantasydraft.espn.com/game-1/league-573680074/PING?1=PING%201787777561860&token=1:573680074:6:{SWID}:{sessionId}`
      (token redacted here on purpose -- it's session credential material, not
      something to have sitting in a tracked file. The shape is what matters.)
    - Read as `PING?1=PING <epoch-ms>&token=<gameId>:<leagueId>:<teamId>:<memberId>:<sessionId>`.
      Shape (repeating `PING`, a session token) strongly suggests a
      WebSocket or long-poll connection, not a plain REST poll -- this is
      almost certainly why `mDraftDetail` never updated: it's the wrong
      host/protocol entirely, not a caching or auth issue.
    - This also explains the "Duplicate Connection" incident: that host
      likely enforces one live connection per token, so *any* second
      connection with the same session (bot or a second human tab) forces
      the first one off, independent of anything `draft_sync.py` does.
  - [ ] Next: capture the *connection setup* for this host, not just the
        recurring PING -- open the Network tab's WS filter (not just
        Fetch/XHR) right when a practice draft's room first loads, and look
        for a `wss://fantasydraft.espn.com/...` upgrade request. That
        initial handshake URL plus the message format for a pick event
        (distinct from the `PING` keepalive) is what a new `draft_sync`
        source would need to open its own read-only connection. Do this
        from a tab Mark is not actively drafting in, given the one-
        connection-per-token behavior above.
  - [ ] Once the pick-event message shape is known, decide whether it's
        worth a `websocket-client` (or similar) dependency to consume it
        directly in `src/ff/draft_sync.py`, versus keeping `mDraftDetail`
        for post-draft reconciliation only and relying on manual entry live.
  - [ ] A direct GET against `fantasy.espn.com/apis/v3/games/ffl/...`
        (rather than `lm-api-reads.fantasy.espn.com`) with the same cookies
        403'd -- likely irrelevant now that the real live host
        (`fantasydraft.espn.com`) is known, but note it here so nobody
        retries it expecting a different result without new headers.
  - [ ] Once a working live endpoint (or confirmation that none exists) is
        found, update `src/ff/draft_sync.py` accordingly. If nothing live
        exists, promote manual entry (already working, `--no-sync` in
        `scripts/auction.py`) from fallback to the documented primary path,
        and demote auto-sync to "reconciliation after the draft ends."
  - [x] Install the `claude-in-chrome` extension and grant it
        `fantasy.espn.com` permission -- done, connected 2026-08-26.

- [ ] **3. Fix the console's inflation adjustment.** Two problems, both in the
      number the console consults most. `DraftState.inflation()` is
      backward-looking (dollars paid over sheet value of players already
      sold), so when the room overpays early it marks the remaining players
      *up* at exactly the moment depleted budgets mean they will clear *under*
      sheet -- the live-auction skill's own "worth $15, clears at $1-2" window.
      And `best` applies the single global rate to every position, while the
      model's core claim is that positions diverge. Replace with
      forward-looking inflation -- dollars remaining in the room divided by
      sheet value of the remaining draftable pool -- computed per position,
      and drive the Adjusted column off that.

- [ ] **4. Validate the replacement levels.** `config.REPLACEMENT` drives
      every price in the model, and `QB: 30` is the most consequential and
      least certain assumption. It presumes all 10 teams roster three QBs; if
      they carry two, replacement moves to ~QB22, and because the QB board
      falls off a cliff around QB26, the baseline swing is large.
  - [ ] Build a sensitivity table across QB 22 / 26 / 30
  - [ ] Commit to a final value and record the reasoning in
        `docs/league-analysis.md`
  - Scope note: the choice moves the cross-position budget split and the
    Allen walk-away price. It does not move the two-from-the-band
    recommendation, which is a points comparison and replacement-invariant.
    The per-position `market` readout also self-corrects live: a room pricing
    QBs like replacement is QB22 shows up as QB inflation under 1.0.

- [ ] **5. Draft-day guardrails in the console.**
  - [ ] Bye weeks: add a `bye` field to `values.json` and a column to
        `best`/`need`, and flag a QB pairing that shares a bye. Matters most
        at QB, where the third quarterback exists largely to cover byes.
  - [ ] Surface the three-QB rule: `needs()` counts starting slots only, so
        the console reports "all starting slots filled" at two QBs. The most
        important roster rule in the league should be on screen in `me`, not
        only in the skill text.

- [ ] **6. Prepare a nomination list.** The strategy doc calls early QB
      nominations the highest-leverage tactic in a 2QB auction, but nothing
      produces the actual list. Write down 10-15 names to nominate: QBs
      outside the target band, plus expensive players not being targeted.

- [ ] **7. Cross-check the QB projection band.** The strategy's central claim
      -- QB2 through QB15 span 2.4 points per week -- rests on ESPN's
      projections alone. Compare the top ~30 QBs against FantasyPros superflex
      and DraftSharks (links in `docs/data-sources.md`), noting both are
      12-team superflex rather than 10-team strict 2QB. If consensus shows a
      wider spread, the skip-Allen conclusion weakens and the walk-away price
      moves. Skill-player spot checks are secondary.

### Once the season starts

- [ ] **8. Exercise the in-season paths.** `box_scores()`, waiver flows, and
      the start-sit and waiver-faab skills have not run against real data
      because the season has not started. Expect rough edges in Week 1.
  - [ ] `box_scores()` against a live matchup
  - [ ] Waiver and FAAB flow end to end
  - [ ] `start-sit` skill against a real lineup decision

- [ ] **9. Rival FAAB tracking.** The waiver-faab skill sizes bids against our
      own $100, but the right bid depends on what rivals still hold, and ESPN
      exposes each team's remaining budget. Surface it alongside the free
      agent scan.

- [ ] **10. Verify D/ST and K projections.** ESPN projects these, but it is
      unconfirmed whether its D/ST projection models this league's unusual
      yards-allowed table (down to -7 for 550+ yards). `scoring.py` implements
      it; nothing has validated ESPN against it. Low stakes at $1 per unit on
      draft day, so this waits until the first streaming decision makes it
      relevant.

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
