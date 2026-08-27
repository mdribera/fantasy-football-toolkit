#!/usr/bin/env python3
"""Textual live auction console, the presentation layer for `auction.py --ws`.

Textual owns the whole screen and redraws widgets in place, which is what
makes the persistent status/roster/analysis view possible and what fixes the
literal-escape-code bug the scrolling printer had: nothing here writes ANSI to
a proxied stdout.

The protocol layer is untouched. This wraps auction.WsController and polls it
on a Textual timer, so every widget update happens on the main event loop with
no cross-thread writes to race against.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Footer, ListView, RichLog, Static

import auction
from ff import config, draft_state, draft_sync, values

POLL_INTERVAL_S = 0.3  # matches the cadence of the printer thread it replaces


class Banner(Static):
    """Full-width alert line. Hidden until something needs to be impossible
    to miss: your nomination turn, or a watchdog/reconnect alert."""

    def show(self, message: str, alert: bool = False) -> None:
        self.update(Text(message))
        self.set_class(alert, "alert")
        self.display = True

    def hide(self) -> None:
        self.display = False


class StatusPanel(Static):
    """What is happening right now, always on screen."""

    nominee = reactive("")
    high_bid = reactive(0)
    high_bidder = reactive("")
    clock_s = reactive(0)
    sheet_value = reactive(0)
    adjusted_value = reactive(0)
    max_bid_amount = reactive(0)

    def render(self) -> Text:
        if not self.nominee:
            return Text.from_markup("[dim]No active nomination.[/dim]")
        clock = f"{self.clock_s}s" if self.clock_s else "-"
        clock_style = "bold red" if 0 < self.clock_s <= 5 else "yellow"
        return Text.from_markup(
            f"[bold]{self.nominee}[/bold]   "
            f"High: [bold]${self.high_bid}[/bold] ({self.high_bidder or '-'})   "
            f"Clock: [{clock_style}]{clock}[/{clock_style}]\n"
            f"Sheet ${self.sheet_value} · Adjusted ${self.adjusted_value} · "
            f"Your max bid: ${self.max_bid_amount}"
        )


class BidLog(RichLog):
    """One line per Bid event, scoped to the current nomination and cleared
    when the pointer moves to a new player. Also carries the console's own
    replies (sent, refused, cancelled) so there is one event stream to read."""


class RosterPanel(Static):
    budget_left = reactive(config.SALARY_CAP)
    spots_left = reactive(config.ROSTER_SIZE)
    max_bid_amount = reactive(0)
    slots = reactive(())    # tuple[tuple[str, int, int], ...] pos, have, need
    roster = reactive(())   # tuple[tuple[str, str, int], ...] name, pos, price

    def render(self) -> Text:
        lines = [
            f"[bold]Budget: ${self.budget_left}[/bold] / ${config.SALARY_CAP}   "
            f"{self.spots_left} spots   max bid ${self.max_bid_amount}",
            "  ".join(
                f"[{'green' if have >= need else 'yellow'}]{pos} {have}/{need}[/]"
                for pos, have, need in self.slots
            ) or "[dim]no starters required[/dim]",
            "",
        ]
        lines.extend(f"{name}  [dim]{pos}[/dim]  ${price}"
                     for name, pos, price in self.roster)
        return Text.from_markup("\n".join(lines))


class AnalysisPanel(Static):
    tier_line = reactive("")
    market_line = reactive("")
    verdict_line = reactive("")
    best_line = reactive("")

    def render(self) -> Text:
        rows = [self.tier_line, self.market_line, self.verdict_line, self.best_line]
        body = "\n".join(r for r in rows if r)
        return Text.from_markup(body or "[dim]Nothing nominated.[/dim]")


class NominationList(ListView):
    """The prepared nomination list, arrow-navigable, filtered to players who
    are still available. `n` nominates whatever is highlighted."""


class TextualWsApp(App):
    CSS_PATH = "ws_console.tcss"
    TITLE = "Auction console"

    BINDINGS = [
        Binding("b", "bid", "bid +1"),
        Binding("n", "nominate", "nominate"),
        Binding("colon", "command", "command"),
        Binding("q", "shutdown", "quit"),
    ]

    def __init__(self, ws, state: draft_state.DraftState,
                 resolver: draft_sync.PlayerResolver,
                 vals: list[values.Valuation], nomination_list: list[str]):
        super().__init__()
        self.ws = ws
        self.state = state
        self.resolver = resolver
        self.vals = vals
        self.nomination_names = nomination_list
        self.lookup = {v.name.lower(): v for v in vals}
        self._log_player_id: int | None = None

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield StatusPanel(id="status")
        with Horizontal(id="middle"):
            yield BidLog(id="bidlog", markup=True, min_width=30, wrap=True)
            yield RosterPanel(id="roster")
        yield AnalysisPanel(id="analysis")
        yield NominationList(id="nominations")
        yield Footer()

    def on_mount(self) -> None:
        self.banner = self.query_one("#banner", Banner)
        self.status = self.query_one("#status", StatusPanel)
        self.bidlog = self.query_one("#bidlog", BidLog)
        self.roster = self.query_one("#roster", RosterPanel)
        self.analysis = self.query_one("#analysis", AnalysisPanel)
        self.nominations = self.query_one("#nominations", NominationList)

        self.bidlog.border_title = "Bid log (this nomination)"
        self.roster.border_title = "Your roster"
        self.analysis.border_title = "Analysis"
        self.nominations.border_title = "Nomination list (up/down to move, n to nominate)"
        self.status.border_title = "STATUS"

        self.set_interval(POLL_INTERVAL_S, self._poll)

    async def _poll(self) -> None:
        """Placeholder until Task 3."""

    def action_shutdown(self) -> None:
        self.exit()

    def action_bid(self) -> None:
        """Placeholder until Task 6."""

    def action_nominate(self) -> None:
        """Placeholder until Task 5."""

    def action_command(self) -> None:
        """Placeholder until Task 7."""


def run_ws_console(ws, state: draft_state.DraftState,
                   resolver: draft_sync.PlayerResolver,
                   vals: list[values.Valuation], nomination_list: list[str]) -> None:
    TextualWsApp(ws, state, resolver, vals, nomination_list).run()
