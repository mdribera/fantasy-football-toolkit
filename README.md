# SuperFun Football League

Tooling for managing **The QB's Knees** in a 10-team ESPN fantasy football
league: salary cap auction, $200 budget, full PPR, **two starting
quarterbacks**.

Draft: **Sep 2, 2026, 12:00 PM PDT.**

## Why this exists

Every public auction value sheet is built for a 1QB league, and using one here
is actively misleading.

The league spends exactly $2,000 no matter the format. A 1QB league puts about
6% of that into quarterbacks; this one puts **17%**. That extra ~$230 does not
appear from nowhere -- it comes straight out of the running back and receiver
markets. So a downloaded chart overprices skill players and underprices the QB
squeeze, and both errors compound.

This project re-derives values from projections against the league's real
settings.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # league ID + SWID/espn_s2 cookies
.venv/bin/python scripts/whoami.py
.venv/bin/python scripts/build_values.py --plan
```

`scripts/selftest.py` validates the model without needing credentials.

## What it does

**`scripts/build_values.py`** pulls projections from ESPN's league-context API
and prices every player: projected points minus a positional replacement
baseline, with the $1,840 biddable surplus distributed in proportion. Writes
`data/values.json` and a tiered board.

**`scripts/auction.py`** is the live draft console. Record every purchase in
the room and it tracks budgets, computes your max bid, flags unfilled starting
slots, and reports **per-position market inflation** -- which position the room
is overpaying for, and therefore which one is going cheap.

## What the model currently says

Against live ESPN projections:

- **QB2 through QB15 span 2.4 points per week.** Only Josh Allen (370 proj)
  separates from the field, and even he is worth just 3.8 pts/week over QB6.
- Consequently **Lamar Jackson + Patrick Mahomes (614 pts, $39) beats Josh
  Allen + a $3 QB2 (609 pts, $38)**. The elite-QB play is dominated.
- Tight end is a punt: every TE in the league prices at $99 combined.
- Kickers and defenses are $1, last, always.

## Credentials

`.env` is gitignored. Get `SWID` and `espn_s2` from browser cookies at
fantasy.espn.com (F12 → Application → Cookies). `espn_s2` rotates
periodically; re-copy it when calls start failing.
