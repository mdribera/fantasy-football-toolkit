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
from ff import config, draft_state, draft_sync, draft_ws, values

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
        self._last_bid_team = ""

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
        self._refresh_panels()

    async def _poll(self) -> None:
        """Drain the websocket and push everything at the widgets.

        Wrapped whole: one malformed frame must never take the live display
        down mid-auction. The banner says so loudly instead.
        """
        try:
            self._drain()
        except Exception as exc:                                  # noqa: BLE001
            self.banner.show(
                f"LIVE FEED ERROR: {exc!r} -- display and auto-record hit an error on one "
                "frame and are continuing. Check ws-log-*.jsonl and your roster carefully.",
                alert=True,
            )

    def _drain(self) -> None:
        for alert in self.ws.drain_alerts():
            self.banner.show(alert, alert=True)

        events = self.ws.drain()
        if not events:
            return

        # self.ws.drain() already folded every event into self.ws.pointer, so
        # this comparison is settled before the loop below runs. Doing the
        # reset here, rather than after the loop, matters: a Bid or Clock
        # event for the new nomination in this same batch needs to land on
        # top of a blank slate, not get overwritten back to it.
        if self.ws.pointer.player_id != self._log_player_id:
            self.bidlog.clear()
            self._log_player_id = self.ws.pointer.player_id
            self.status.clock_s = 0
            self._last_bid_team = ""

        taken = {name.lower() for name in self.state.taken()}

        for event in events:
            if isinstance(event, draft_ws.Nomination):
                team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
                self._flash(f"[bold]NOMINATION[/bold] {team} is on the clock")
            elif isinstance(event, draft_ws.Bid):
                team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
                self._last_bid_team = team
                name, _ = self.resolver.resolve(event.player_id)
                self._flash(f"{team:<7} ${event.amount}  [dim]{name}[/dim]")
            elif isinstance(event, draft_ws.Clock) and event.state == 2:
                self.status.clock_s = event.remaining_ms // 1000
                self._last_bid_team = config.TEAMS.get(event.high_bid_team, self._last_bid_team)
                milestone = self.ws.milestone(event)
                if milestone:
                    self._flash(f"[yellow]{milestone}s left[/yellow], "
                                f"high bid ${event.high_bid_amount}")
            elif isinstance(event, draft_ws.Sold):
                team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
                name, position = self.resolver.resolve(event.player_id)
                if name.lower() in taken:
                    self._flash(f"[dim]SOLD {name} already recorded by hand, "
                                f"skipping duplicate.[/dim]")
                elif self.state.record_pick(name, position, event.price, team,
                                            espn_pick_id=event.player_id):
                    taken.add(name.lower())
                    match = self.lookup.get(name.lower())
                    note = (f" (sheet ${match.value}, {match.value - event.price:+d})"
                            if match else "")
                    self._flash(f"[green]SOLD[/green] {name} ${event.price} "
                                f"-> {team}{note}")
            elif isinstance(event, draft_ws.WsError):
                self._flash(f"[yellow]unparsed frame:[/yellow] {event.raw!r} "
                            f"({event.reason})")

        self._sync_pointer()
        self._refresh_panels()

    def _sync_pointer(self) -> None:
        """Fold the pointer into StatusPanel. The pointer, not this app, is
        the single source of truth for what is happening right now."""
        pointer = self.ws.pointer
        if pointer.player_id is None:
            self.status.nominee = ""
            self.status.high_bid = 0
            self.status.high_bidder = ""
            return

        name, position = self.resolver.resolve(pointer.player_id)
        match = self.lookup.get(name.lower())
        team = f" ({position}" + (f", {match.pro_team})" if match and match.pro_team else ")")
        self.status.nominee = f"{name}{team}"
        self.status.high_bid = pointer.high_bid
        self.status.high_bidder = self._high_bidder_label()
        self.status.sheet_value = match.value if match else 0
        self.status.adjusted_value = self._adjusted(match)
        self.status.max_bid_amount = self.state.max_bid(self.state.my_team)

    def _high_bidder_label(self) -> str:
        return self._last_bid_team

    def _adjusted(self, match: values.Valuation | None) -> int:
        if not match:
            return 0
        return max(1, round(match.value * self.state.inflation(self.vals)))

    def _flash(self, message: str) -> None:
        self.bidlog.write(message)

    def _refresh_panels(self) -> None:
        self._refresh_roster()
        self._refresh_analysis()

    def _refresh_roster(self) -> None:
        me = self.state.my_team
        counts = self.state.position_counts(me)
        self.roster.budget_left = self.state.budget_left(me)
        self.roster.spots_left = self.state.spots_left(me)
        self.roster.max_bid_amount = self.state.max_bid(me)
        self.roster.slots = tuple(
            (pos, counts.get(pos, 0), required)
            for pos, required in config.STARTERS.items()
            if pos != "FLEX"
        )
        self.roster.roster = tuple(
            (p.player, p.position, p.price)
            for p in self.state.purchases if p.team == me
        )

    def _neediest_position(self) -> str | None:
        """First unfilled starting slot in STARTERS order, which is the order
        'need' already iterates. Deterministic, and good enough: the panel is
        a pointer at where your dollars have to go, not a ranking."""
        for pos, count in self.state.needs(self.state.my_team).items():
            if count > 0:
                return pos
        return None

    def _refresh_analysis(self) -> None:
        pointer = self.ws.pointer
        if pointer.player_id is None:
            self.analysis.tier_line = ""
            self.analysis.market_line = ""
            self.analysis.verdict_line = ""
            self.analysis.best_line = ""
            return

        name, _ = self.resolver.resolve(pointer.player_id)
        match = self.lookup.get(name.lower())
        taken = self.state.taken()

        if match:
            equivalent = auction.next_equivalent(
                self.vals, taken, match.position, match.tier, match.name)
            if equivalent is None:
                self.analysis.tier_line = (
                    f"[bold red]Tier {match.tier} {match.position} -- nothing "
                    f"equivalent left.[/bold red]")
            elif equivalent.tier == match.tier:
                self.analysis.tier_line = (
                    f"Tier {match.tier} {match.position} -- next: "
                    f"{equivalent.name} (${equivalent.value})")
            else:
                self.analysis.tier_line = (
                    f"[yellow]Tier {match.tier} {match.position} -- last one. "
                    f"Next tier: {equivalent.name} (${equivalent.value})[/yellow]")

            by_position = self.state.inflation_by_position(self.vals)
            if match.position in by_position:
                rate, scope = by_position[match.position], match.position
            else:
                rate, scope = self.state.inflation(self.vals), "overall"
            read = ("over sheet" if rate > 1.1 else
                    "under sheet" if rate < 0.9 else "at sheet")
            self.analysis.market_line = f"Market: {scope} paying x{rate:.2f} ({read})"

            verdict = auction.bid_verdict(pointer.high_bid, self._adjusted(match))
            self.analysis.verdict_line = (
                f"Verdict: [{verdict.style}]{verdict.label}[/{verdict.style}] "
                f"at ${pointer.high_bid}")
        else:
            self.analysis.tier_line = f"[dim]{name} is not on your board.[/dim]"
            self.analysis.market_line = ""
            self.analysis.verdict_line = ""

        need = self._neediest_position()
        pool = sorted(
            (v for v in self.vals
             if v.name not in taken and (need is None or v.position == need)),
            key=lambda v: v.value, reverse=True)[:3]
        label = f"neediest -- {need}" if need else "all starters filled"
        self.analysis.best_line = (
            f"Best remaining ({label}): "
            + (", ".join(f"{v.name} {v.position} ${v.value}" for v in pool) or "none")
        )

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
