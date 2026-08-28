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

import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, RichLog, Static

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


class NominationRow(ListItem):
    """One prepared nomination. Carries the player's name, tier, and both
    values so `n` can act on whatever is highlighted without re-parsing the
    rendered label, and so callers can read the same numbers without
    scraping rendered text."""

    def __init__(self, name: str, position: str, tier: str, value: str, adjusted: str):
        super().__init__(Label(f"{name:<26}{position:<5}{tier:<7}{value:<7}{adjusted}"))
        self.player_name = name
        self.tier = tier
        self.sheet_value = value
        self.adjusted_value = adjusted


class NominationList(ListView):
    """The prepared nomination list, arrow-navigable, filtered to players who
    are still available. `n` nominates whatever is highlighted."""


class ConfirmBidScreen(ModalScreen[bool]):
    """The typo guard, modal rather than inline so a busy background cannot
    hide it and an ambiguous keystroke cannot answer it by accident."""

    BINDINGS = [
        Binding("y", "confirm", "yes"),
        Binding("n", "refuse", "no"),
        Binding("escape", "refuse", "no"),
    ]

    def __init__(self, amount: int, player: str, reason: str):
        super().__init__()
        self.amount = amount
        self.player = player
        self.reason = reason

    def compose(self) -> ComposeResult:
        yield Static(
            Text.from_markup(
                f"[yellow]{self.reason}[/yellow]\n\n"
                f"Bid [bold]${self.amount}[/bold] on [bold]{self.player}[/bold]?\n\n"
                f"[bold]y[/bold] yes    [bold]n[/bold] no"
            ),
            id="confirm-dialog",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_refuse(self) -> None:
        self.dismiss(False)


class TextualWsApp(App):
    CSS_PATH = "ws_console.tcss"
    TITLE = "Auction console"
    BID_WATCHDOG_TIMEOUT_S = 4  # CLOCK ticks ~1/s, so a few seconds of slack
                                # before treating a sent bid as unconfirmed

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
        self._last_bid_team_id: int | None = None
        self._pending_bid: tuple[int, int, float] | None = None
        self._init_backed_up = False  # back up draft-state.json once, before
                                       # the first INIT reconcile may prune it

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield StatusPanel(id="status")
        with Horizontal(id="middle"):
            yield BidLog(id="bidlog", markup=True, min_width=30, wrap=True)
            yield RosterPanel(id="roster")
        yield AnalysisPanel(id="analysis")
        yield NominationList(id="nominations")
        yield Input(id="command", placeholder="b 45 | undo | market | teams | best RB | need | me | quit")
        yield Footer()

    async def on_mount(self) -> None:
        self.banner = self.query_one("#banner", Banner)
        self.status = self.query_one("#status", StatusPanel)
        self.bidlog = self.query_one("#bidlog", BidLog)
        self.roster = self.query_one("#roster", RosterPanel)
        self.analysis = self.query_one("#analysis", AnalysisPanel)
        self.nominations = self.query_one("#nominations", NominationList)
        self.command = self.query_one("#command", Input)

        self.bidlog.border_title = "Bid log (this nomination)"
        self.roster.border_title = "Your roster"
        self.analysis.border_title = "Analysis"
        self.nominations.border_title = (
            "Nomination list (up/down to move, n to nominate) -- "
            "name / pos / tier / sheet / adjusted")
        self.status.border_title = "STATUS"

        await self._reload_nominations()
        self._refresh_panels()
        self.set_interval(POLL_INTERVAL_S, self._poll)

    async def _poll(self) -> None:
        """Drain the websocket and check the bid watchdog every tick,
        regardless of whether any frames arrived this tick -- a silent
        socket is exactly the case the watchdog exists to catch, not just
        a busy one.
        """
        self._guarded_drain()
        self._check_bid_watchdog()

    def _guarded_drain(self) -> None:
        """Wrapped whole: one malformed frame must never take the live
        display down mid-auction, whether triggered by the poll timer or
        by a bid keypress that drains proactively. The banner says so
        loudly instead.
        """
        try:
            self._drain()
        except Exception as exc:                                  # noqa: BLE001
            message = (
                f"LIVE FEED ERROR: {exc!r} -- display and auto-record hit an error on one "
                "frame and are continuing. Check ws-log-*.jsonl and your roster carefully.")
            self.banner.show(message, alert=True)
            self._flash(f"[red]{message}[/red]")

    def _drain(self) -> None:
        for alert in self.ws.drain_alerts():
            self.banner.show(alert, alert=True)
            self._flash(f"[red]{alert}[/red]")

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
            self._last_bid_team_id = None

        taken = {name.lower() for name in self.state.taken()}

        for event in events:
            if isinstance(event, draft_ws.Nomination):
                team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
                self._flash(f"[bold]NOMINATION[/bold] {team} is on the clock")
                if event.team_id == config.MY_TEAM_ID:
                    self._raise_turn_alert()
                else:
                    self._clear_turn_alert()
            elif isinstance(event, draft_ws.Bid):
                team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
                self._last_bid_team = team
                self._last_bid_team_id = event.team_id
                name, _ = self.resolver.resolve(event.player_id)
                self._flash(f"{team:<7} ${event.amount}  [dim]{name}[/dim]")
            elif isinstance(event, draft_ws.Clock) and event.state == 2:
                self.status.clock_s = event.remaining_ms // 1000
                self._last_bid_team = config.TEAMS.get(event.high_bid_team, self._last_bid_team)
                self._last_bid_team_id = event.high_bid_team
                milestone = self.ws.milestone(event)
                if milestone:
                    self._flash(f"[yellow]{milestone}s left[/yellow], "
                                f"high bid ${event.high_bid_amount}")
            elif isinstance(event, draft_ws.Sold):
                team = config.TEAMS.get(event.team_id, f"TEAM{event.team_id}")
                name, position = self.resolver.resolve(event.player_id)
                if name.lower() in taken:
                    existing = next(p for p in self.state.purchases
                                    if p.player.lower() == name.lower())
                    if (existing.team == draft_state.normalize_team(team)
                            and existing.price == event.price):
                        self._flash(f"[dim]SOLD {name} already recorded by hand, "
                                    f"skipping duplicate.[/dim]")
                    else:
                        message = (
                            f"SOLD {name} to {team} for ${event.price}, but "
                            f"data/draft-state.json already has {existing.team} "
                            f"for ${existing.price} on the same player -- your "
                            f"roster/budget may now be wrong. Check the file by hand.")
                        self.banner.show(message, alert=True)
                        self._flash(f"[red]{message}[/red]")
                elif self.state.record_pick(name, position, event.price, team,
                                            espn_pick_id=event.player_id):
                    taken.add(name.lower())
                    self.call_later(self._reload_nominations)
                    match = self.lookup.get(name.lower())
                    note = (f" (sheet ${match.value}, {match.value - event.price:+d})"
                            if match else "")
                    self._flash(f"[green]SOLD[/green] {name} ${event.price} "
                                f"-> {team}{note}")
                    self._clear_turn_alert()
            elif isinstance(event, draft_ws.WsError):
                self._flash(f"[yellow]unparsed frame:[/yellow] {event.raw!r} "
                            f"({event.reason})")
            elif isinstance(event, draft_ws.Init):
                init = draft_ws.parse_init_state(event.blob)
                if init is None:
                    message = ("INIT frame could not be decoded -- roster may be "
                                "stale until the next successful reconnect.")
                    self.banner.show(message, alert=True)
                    self._flash(f"[red]{message}[/red]")
                else:
                    self._backup_state_once()
                    report = auction.reconcile_init(self.state, init, self.resolver)
                    taken = {name.lower() for name in self.state.taken()}
                    if report.corrected or report.removed:
                        message = (
                            f"Reconciled with the server: {len(report.added)} added, "
                            f"{len(report.corrected)} corrected, {len(report.removed)} "
                            "removed -- local state and the server had diverged. "
                            "Check your roster.")
                        self.banner.show(message, alert=True)
                        self._flash(f"[red]{message}[/red]")
                    elif report.added:
                        message = (f"Reconciled with the server: added "
                                   f"{len(report.added)} sale(s) recorded while "
                                   "disconnected.")
                        self.banner.show(message)
                        self._flash(f"[green]{message}[/green]")
                    if report.added or report.removed:
                        self.call_later(self._reload_nominations)

        self._sync_pointer()
        self._refresh_panels()

    def _check_bid_watchdog(self) -> None:
        if self._pending_bid is None:
            return
        player_id, amount, sent_at = self._pending_bid
        pointer = self.ws.pointer
        if pointer.player_id != player_id:
            self._pending_bid = None                    # nomination moved on either way
            return
        if pointer.high_bid >= amount and self._i_hold_the_high():
            self._pending_bid = None                     # confirmed: our bid landed
            return
        if time.monotonic() - sent_at > self.BID_WATCHDOG_TIMEOUT_S:
            name, _ = self.resolver.resolve(player_id)
            elapsed = time.monotonic() - sent_at
            message = (
                f"Bid ${amount} on {name} was sent {elapsed:.0f}s ago but the server "
                "hasn't confirmed it as the high bid -- check ESPN's own UI directly.")
            self.banner.show(message, alert=True)
            self._flash(f"[red]{message} (unconfirmed)[/red]")
            self._pending_bid = None                     # alert once, don't spam every poll

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

    def _i_hold_the_high(self) -> bool:
        """Whether the most recent bid on the active nomination is ours, by
        ESPN team id rather than the display label -- state.my_team can
        diverge from config.TEAMS[config.MY_TEAM_ID] (e.g. --my-team), and
        comparing labels silently breaks both the self-bid guard and the
        bid watchdog."""
        return self._last_bid_team_id == config.MY_TEAM_ID

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
        self._start_bid([])

    def _start_bid(self, args: list[str]) -> None:
        self._guarded_drain()                         # fold in anything already arrived, crash-safe
        pointer = self.ws.pointer                     # single atomic snapshot
        if pointer.player_id is None:
            self._flash("[yellow]No active nomination to bid on.[/yellow]")
            return
        player_id = pointer.player_id
        name, _ = self.resolver.resolve(player_id)
        match = self.lookup.get(name.lower())
        plan = auction.evaluate_bid(
            args, pointer.high_bid, self.state.max_bid(self.state.my_team),
            self._adjusted(match) or None,
            already_high=self._i_hold_the_high())

        if isinstance(plan, auction.BidRefused):
            self._flash(f"[red]Refused:[/red] {plan.reason}")
            return
        if isinstance(plan, auction.BidNeedsConfirmation):
            def answered(confirmed: bool | None) -> None:
                if confirmed:
                    self._send_bid(player_id, plan.amount, name)
                else:
                    self._flash("Cancelled.")

            self.push_screen(ConfirmBidScreen(plan.amount, name, plan.reason), answered)
            return
        self._send_bid(player_id, plan.amount, name)

    def _send_bid(self, player_id: int, amount: int, name: str) -> None:
        # The one deliberate fresh re-read: the nomination can move on while a
        # confirmation modal is open, and sending then buys the wrong player.
        if self.ws.pointer.player_id != player_id:
            self._flash(f"[red]Refused:[/red] the nomination changed while you were "
                        f"deciding (was {name}) -- bid not sent, press b again if you "
                        f"still want in.")
            return
        try:
            self.ws.client.send_bid(player_id, amount)
        except RuntimeError as exc:
            self._flash(f"[red]Not sent:[/red] {exc} -- bid in ESPN's own UI if urgent.")
        else:
            self._pending_bid = (player_id, amount, time.monotonic())
            self._flash(f"[green]Sent bid ${amount} on {name}.[/green]")

    async def _reload_nominations(self) -> None:
        """Rebuild the list from data/nomination-list.txt, filtered to players
        who are still available. clear() defers its removals, so it has to be
        awaited before the new rows go in.

        This runs after every recorded sale, and clear() always resets the
        highlight to None -- so the previously highlighted player's name is
        captured first and restored afterward, rather than always landing
        back on row 0."""
        previous = self.nominations.highlighted_child
        previous_name = previous.player_name if previous else None

        await self.nominations.clear()
        taken = self.state.taken()
        for name in self.nomination_names:
            if name in taken:
                continue
            match = self.lookup.get(name.lower())
            await self.nominations.append(NominationRow(
                name,
                match.position if match else "?",
                f"T{match.tier}" if match else "-",
                f"${match.value}" if match else "-",
                f"~${self._adjusted(match)}" if match else "-",
            ))

        # ListView.append() doesn't highlight anything on its own, and a
        # freshly-rebuilt list needs something highlighted for arrow keys
        # (and an immediate "n") to act on. Prefer restoring the player who
        # was highlighted before the reload; fall back to row 0 if they're
        # no longer in the list (e.g. they were the one just taken).
        if previous_name is not None:
            for i, row in enumerate(self.nominations.children):
                if row.player_name == previous_name:
                    self.nominations.index = i
                    break
            else:
                if self.nominations.children:
                    self.nominations.index = 0
        elif self.nominations.children:
            self.nominations.index = 0

    def action_nominate(self) -> None:
        if self.ws.pointer.nominating_team != config.MY_TEAM_ID:
            self._flash("[yellow]It is not your nomination turn.[/yellow]")
            return
        row = self.nominations.highlighted_child
        if row is None:
            self._flash("[yellow]Nothing highlighted to nominate.[/yellow]")
            return
        match = self.lookup.get(row.player_name.lower())
        if not match or match.espn_id is None:
            self._flash(f"[red]Unknown or unresolvable player: "
                        f"{row.player_name}[/red]")
            return
        try:
            self.ws.client.send_nomination(match.espn_id, 1)
        except RuntimeError as exc:
            self._flash(f"[red]Not sent:[/red] {exc} -- nominate in ESPN's own UI "
                        "if urgent.")
        else:
            self._flash(f"[green]Nominated {match.name} at $1.[/green]")

    def _backup_state_once(self) -> None:
        """Back up draft-state.json before the first INIT reconcile of this
        session -- reconcile can delete a local purchase the server doesn't
        have, and that must be recoverable by hand if the decode was ever
        wrong. Same convention as load_state's --fresh backup."""
        if self._init_backed_up:
            return
        self._init_backed_up = True
        path = self.state.state_path
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_text(path.read_text())

    def _raise_turn_alert(self) -> None:
        """An idle nomination turn is an unattended-purchase risk, so this
        takes the border, the banner, and the bell all at once."""
        self.banner.show("YOUR TURN TO NOMINATE -- highlight a player and press n")
        self.screen.add_class("my-turn")
        self.bell()
        # Don't steal focus from an open command input: the input stays
        # displayed but stops receiving keystrokes, and the next `n` keypress
        # meant for it fires the nominate hotkey instead.
        if not self.command.has_focus:
            self.nominations.focus()

    def _clear_turn_alert(self) -> None:
        self.screen.remove_class("my-turn")
        # A watchdog/reconnect alert or a LIVE FEED ERROR also shows on this
        # banner and must survive the next Nomination or Sold event -- those
        # fire constantly during a live draft and would otherwise wipe an
        # alert before it's been seen.
        if not self.banner.has_class("alert"):
            self.banner.hide()

    def action_command(self) -> None:
        self.command.display = True
        self.command.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        event.input.display = False
        self.nominations.focus()
        if line:
            self._run_command(line)

    def _run_command(self, line: str) -> None:
        """The same verbs the REPL dispatches, for everything not worth a
        hotkey. Tables come from auction.py's builders so both consoles show
        exactly the same numbers."""
        cmd = line.split()
        head = cmd[0].lower()

        if head in ("quit", "exit", "q"):
            self.exit()
        elif head == "b":
            self._start_bid(cmd[1:])
        elif head == "me":
            table, footer = auction.me_table(self.state, self.vals)
            self.bidlog.write(table)
            self._flash(footer)
        elif head == "teams":
            self.bidlog.write(auction.teams_table(self.state))
        elif head == "market":
            self.bidlog.write(auction.market_table(self.state, self.vals))
            self._flash(f"Other teams still hold [bold]"
                        f"${self.state.dollars_remaining_in_room()}[/bold] combined.")
        elif head == "need":
            for pos, count in self.state.needs(self.state.my_team).items():
                if count > 0:
                    self.bidlog.write(auction.best_table(self.state, self.vals, pos, 6))
        elif head == "best":
            pos = cmd[1] if len(cmd) > 1 and not cmd[1].isdigit() else None
            limit = next((int(c) for c in cmd[1:] if c.isdigit()), 15)
            self.bidlog.write(auction.best_table(self.state, self.vals, pos, limit))
        elif head == "undo":
            removed = self.state.undo()
            self._flash(f"Removed: {removed}" if removed else "Nothing to undo.")
            self._refresh_panels()
            self.call_later(self._reload_nominations)
        else:
            self._flash("[yellow]Unrecognized.[/yellow] Use: b/undo/market/teams/"
                        "best/need/me/quit")


def run_ws_console(ws, state: draft_state.DraftState,
                   resolver: draft_sync.PlayerResolver,
                   vals: list[values.Valuation], nomination_list: list[str]) -> None:
    TextualWsApp(ws, state, resolver, vals, nomination_list).run()
