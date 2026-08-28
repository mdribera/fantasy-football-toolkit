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
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, RichLog, Static

import auction
from ff import config, draft_state, draft_sync, draft_ws, values

POLL_INTERVAL_S = 0.3  # matches the cadence of the printer thread it replaces


class Banner(Static):
    """Full-width alert line. Hidden until something needs to be impossible
    to miss: your nomination turn, or a watchdog/reconnect alert.

    Alerts queue rather than silently overwrite each other: whatever was
    showing when a new one arrives waits behind it instead of vanishing.
    `escape` (see TextualWsApp.action_dismiss_banner) dismisses whatever is
    showing and reveals the next, oldest first, so two alerts landing close
    together during a live draft don't cost Mark the first one."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._current: tuple[str, bool, bool] | None = None  # message, alert, disconnect
        self._queue: list[tuple[str, bool, bool]] = []

    def show(self, message: str, alert: bool = False, disconnect: bool = False) -> None:
        if self.display and self._current is not None:
            self._queue.append(self._current)
        self._current = (message, alert, disconnect)
        self.display = True
        self._render_current()

    def hide(self) -> None:
        self.display = False
        self._current = None
        self._queue.clear()

    def dismiss(self) -> None:
        """Drop whatever is showing and reveal the next queued alert, oldest
        first, or hide entirely once nothing is left."""
        if not self.display:
            return
        if self._queue:
            self._current = self._queue.pop(0)
            self._render_current()
        else:
            self.hide()

    def clear_disconnect(self) -> None:
        """Drop the disconnect alert specifically, wherever it is -- showing
        now, or still waiting behind something else -- so a healed
        connection can't leave a stale "reconnecting" message to surface
        later once the banner in front of it gets dismissed. Any other alert
        (a watchdog trip, a bid confirmation failure, a reconcile mismatch)
        that has since taken the banner is left untouched."""
        self._queue = [item for item in self._queue if not item[2]]
        if self._current is not None and self._current[2]:
            self.dismiss()

    def _render_current(self) -> None:
        message, alert, _ = self._current
        text = message + (f"  (+{len(self._queue)} more, esc to dismiss)"
                          if self._queue else "  (esc to dismiss)")
        self.update(Text(text))
        self.set_class(alert, "alert")


class StatusPanel(Static):
    """What is happening right now, always on screen: who's up, what the
    market says about them, and whether you should bid. Combines what used
    to be two separate panels -- the live bid state and the per-player
    analysis are the same read, not two."""

    nominee = reactive("")
    high_bid = reactive(0)
    high_bidder = reactive("")
    clock_s = reactive(0)
    sheet_value = reactive(0)
    adjusted_value = reactive(None)
    espn_avg = reactive(None)
    edge = reactive(None)
    tier = reactive(0)
    bye = reactive(None)
    max_bid_amount = reactive(0)
    tier_line = reactive("")
    verdict_line = reactive("")
    bye_line = reactive("")

    def render(self) -> Text:
        if not self.nominee:
            return Text.from_markup("[dim]No active nomination.[/dim]")
        clock = f"{self.clock_s}s" if self.clock_s else "-"
        clock_style = "bold red" if 0 < self.clock_s <= 5 else "yellow"
        header = f"[bold]{self.nominee}[/bold]"
        if self.tier:
            header += f"  T{self.tier}"
        if self.bye:
            header += f"  bye {self.bye}"
        espn_avg = f"${self.espn_avg:.0f}" if self.espn_avg is not None else "-"
        adjusted = f"${self.adjusted_value}" if self.adjusted_value is not None else "-"
        edge_style = ("green" if self.edge and self.edge > 0
                     else "red" if self.edge and self.edge < 0 else "dim")
        edge = f"{self.edge:+d}" if self.edge is not None else "-"
        lines = [
            header,
            f"High: [bold]${self.high_bid}[/bold] ({self.high_bidder or '-'})   "
            f"Clock: [{clock_style}]{clock}[/{clock_style}]",
            f"Sheet ${self.sheet_value} · Adjusted {adjusted} · "
            f"ESPN {espn_avg} · Edge [{edge_style}]{edge}[/{edge_style}] · "
            f"max bid ${self.max_bid_amount}",
        ]
        lines += [line for line in (self.verdict_line, self.tier_line, self.bye_line) if line]
        return Text.from_markup("\n".join(lines))


class BidLog(RichLog):
    """One line per Bid event, scoped to the current nomination and cleared
    when the pointer moves to a new player, plus the console's own bid
    replies (sent, refused, cancelled) -- what the auction itself is doing.
    Typed `:` command replies go to OutputLog instead, so a table you asked
    for doesn't scroll away behind the next live bid."""

    can_focus = False


class OutputLog(RichLog):
    """Replies to typed `:` commands -- the :me/:teams/:market/:best/:need
    tables and the board's search/filter/sort status lines -- kept off the
    bid log so a result you asked for survives the next bid or sale."""

    can_focus = False


class TeamList(DataTable):
    """Every team in the league, selectable -- highlighting a row points
    RosterPanel and RosterTable at that team instead of always showing ours.
    Rebuilt on the same cadence as NominationTable, and for the same reason:
    the Left column has to stay live as budgets move."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("Team", "Left")


class RosterPanel(Static):
    """Budget and starting-slot summary for whichever team is selected in
    TeamList -- two logical lines, which may wrap within the panel's width
    as the slot hints grow, but never grow in *count*. The player list lives
    in the sibling RosterTable instead: an ever-growing list of names is
    exactly the shape of content a plain Static's "auto" height doesn't
    reliably keep up with once it's already mounted, which is what clipped
    the roster panel before (see T20)."""

    budget_left = reactive(config.SALARY_CAP)
    spots_left = reactive(config.ROSTER_SIZE)
    max_bid_amount = reactive(0)
    slots = reactive(())    # tuple[tuple[str, int, int, int], ...] pos, have, need, target
    bye_clash = reactive(None)   # bye week two rostered QBs share, or None

    def _slot_label(self, pos: str, have: int, need: int, target: int) -> str:
        color = "green" if have >= need else "yellow"
        label = f"{pos} {have}/{need}"
        # A starting requirement met is not the same as a full bench -- QB
        # especially, where the third quarterback exists for byes and the
        # in-season waiver wire is empty, so this can't wait until the
        # position "needs" attention the way needs() alone would report.
        if have >= need and have < target:
            hint = "3rd for byes" if pos == "QB" else f"want {target}"
            label += f" ({hint})"
        if pos == "QB" and self.bye_clash is not None:
            color = "red"
            label += f" bye clash wk{self.bye_clash}!"
        return f"[{color}]{label}[/]"

    def render(self) -> Text:
        lines = [
            f"[bold]Budget: ${self.budget_left}[/bold] / ${config.SALARY_CAP}   "
            f"{self.spots_left} spots   max bid ${self.max_bid_amount}",
            "  ".join(
                self._slot_label(pos, have, need, target)
                for pos, have, need, target in self.slots
            ) or "[dim]no starters required[/dim]",
        ]
        return Text.from_markup("\n".join(lines))


class RosterTable(DataTable):
    """The selected team's drafted players, one row each -- a DataTable so
    the list scrolls and virtualizes like NominationTable instead of a
    Static's fixed "auto" height silently clipping it once the roster
    grows."""

    can_focus = False

    def on_mount(self) -> None:
        self.cursor_type = "none"
        self.add_columns("Player", "Pos", "Paid")


class NominationTable(DataTable):
    """The full board of available players -- arrow-navigable, one row per
    priced player who hasn't been sold. `n` nominates whatever row the
    cursor sits on. A DataTable virtualizes rendering, so this stays cheap
    at the full ~600-player board where a widget-per-row ListView would not.
    """

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("*", "Player", "Pos", "Need", "Tier", "Bye", "Sheet", "Adj", "ESPN", "Edge")


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
        Binding("space", "star", "star"),
        Binding("slash", "search", "search"),
        Binding("colon", "command", "command"),
        Binding("escape", "dismiss_banner", "dismiss"),
        Binding("q", "shutdown", "quit"),
    ]

    def __init__(self, ws, state: draft_state.DraftState,
                 resolver: draft_sync.PlayerResolver,
                 vals: list[values.Valuation], nomination_list: list[str],
                 nomination_list_path=None):
        super().__init__()
        self.ws = ws
        self.state = state
        self.resolver = resolver
        self.vals = vals
        self.starred: set[str] = set(nomination_list)
        self.nomination_list_path = nomination_list_path
        self.lookup = {v.name.lower(): v for v in vals}
        self._log_player_id: int | None = None
        self._last_bid_team = ""
        self._last_bid_team_id: int | None = None
        self._pending_bid: tuple[int, int, float] | None = None
        self._init_backed_up = False  # back up draft-state.json once, before
                                       # the first INIT reconcile may prune it
        self._nomination_list_backed_up = False
        self._board_rows: list[auction.BoardRow] = []
        self._board_query: str | None = None
        self._board_position: str | None = None
        self._board_starred_only = False
        self._board_sort = "rank"
        self.selected_team = state.my_team
        self._team_rows: list[str] = []

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield StatusPanel(id="status")
        with Horizontal(id="middle"):
            with Vertical(id="feed"):
                yield BidLog(id="bidlog", markup=True, min_width=30, wrap=True)
                yield OutputLog(id="output", markup=True, min_width=30, wrap=True)
            with Horizontal(id="roster"):
                yield TeamList(id="team-list")
                with Vertical(id="roster-pane"):
                    yield RosterPanel(id="roster-header")
                    yield RosterTable(id="roster-table")
        yield NominationTable(id="nominations")
        yield Input(id="command", placeholder="/name | pos QB | sort rec | star | "
                    "b 45 | team 4 | undo | market | teams | best RB | need | me | quit")
        yield Footer()

    async def on_mount(self) -> None:
        self.banner = self.query_one("#banner", Banner)
        self.status = self.query_one("#status", StatusPanel)
        self.bidlog = self.query_one("#bidlog", BidLog)
        self.output = self.query_one("#output", OutputLog)
        self.team_list = self.query_one("#team-list", TeamList)
        self.roster_box = self.query_one("#roster", Horizontal)
        self.roster = self.query_one("#roster-header", RosterPanel)
        self.roster_table = self.query_one("#roster-table", RosterTable)
        self.nominations = self.query_one("#nominations", NominationTable)
        self.command = self.query_one("#command", Input)

        self.bidlog.border_title = "Bid log (this nomination)"
        self.output.border_title = "Output (:me :teams :market :best :need)"
        self._update_roster_title()
        self.nominations.border_title = (
            "Board (up/down, n to nominate, space to star, / to search) -- "
            "star / name / pos / tier / bye / sheet / adjusted / espn avg / edge")
        self.status.border_title = (
            "NOW -- Sheet: your model's price; Adjusted: Sheet adjusted for "
            "how the room is actually paying; ESPN: ESPN's own average "
            "auction value (1QB format, a market anchor not a price); Edge: "
            "Sheet minus Adjusted, positive is a bargain")

        self._reload_board()
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
            disconnect = alert.startswith(draft_ws.DISCONNECT_ALERT_PREFIX)
            self.banner.show(alert, alert=True, disconnect=disconnect)
            self._flash(f"[red]{alert}[/red]")

        if self.ws.connected:
            self.banner.clear_disconnect()

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
                self._note_bid_confirmation(event.player_id, event.team_id, event.amount)
                name, _ = self.resolver.resolve(event.player_id)
                self._flash(f"{team:<7} ${event.amount}  [dim]{name}[/dim]")
            elif isinstance(event, draft_ws.Clock) and event.state == 2:
                self.status.clock_s = event.remaining_ms // 1000
                self._last_bid_team = config.TEAMS.get(event.high_bid_team, self._last_bid_team)
                self._last_bid_team_id = event.high_bid_team
                self._note_bid_confirmation(event.player_id, event.high_bid_team,
                                            event.high_bid_amount)
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
                    self._reload_board()
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
                        self._reload_board()

        self._sync_pointer()
        self._refresh_panels()

    def _note_bid_confirmation(self, player_id: int, team_id: int, amount: int) -> None:
        """Clear a pending bid the moment it lands, independent of whether
        it's still the high bid by the time this frame is processed.
        Without this, `_check_bid_watchdog` only ever looks at the *current*
        high bid -- so a bid that landed and was outbid a moment later inside
        the watchdog window reads identically to one that never landed at
        all, and the watchdog fires a false 'unconfirmed' alert on a bid
        Mark actually won for a beat."""
        if (self._pending_bid and self._pending_bid[0] == player_id
                and team_id == config.MY_TEAM_ID and amount >= self._pending_bid[1]):
            self._pending_bid = None

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
        self.status.espn_avg = match.espn_avg if match else None
        self.status.edge = (
            match.value - self.status.adjusted_value
            if match and self.status.adjusted_value is not None else None
        )
        self.status.tier = match.tier if match else 0
        self.status.bye = match.bye if match else None
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

    def _adjusted(self, match: values.Valuation | None) -> int | None:
        """Market-adjusted price: the forward-looking rate (dollars left in
        the room over sheet value of what's left to buy with them), tilted
        by this position's own market read -- not the backward-looking rate,
        which marks remaining players up at exactly the moment depleted
        budgets mean they'll actually clear under sheet.

        None when there's no player to price, or no market read for it at
        all (the endgame no-read case -- see DraftState.forward_inflation)."""
        if not match:
            return None
        by_position = self.state.forward_inflation_by_position(self.vals)
        rate = by_position.get(match.position, self.state.forward_inflation(self.vals))
        if rate is None:
            return None
        return max(1, round(match.value * rate))

    def _flash(self, message: str) -> None:
        self.bidlog.write(message)

    def _output(self, renderable, *, clear: bool = True) -> None:
        """Write a `:` command's reply to OutputLog -- clearing first by
        default, since a fresh :me/:teams/:market table replaces rather than
        piles onto whatever was there before. A caller writing several
        related lines (a table plus its footer, :need's per-position tables)
        passes clear=False on every write after the first."""
        if clear:
            self.output.clear()
        self.output.write(renderable)

    def _refresh_panels(self) -> None:
        self._refresh_teams()
        self._refresh_roster()
        self._refresh_analysis()

    def _update_roster_title(self) -> None:
        self.roster_box.border_title = f"{self.selected_team} -- tab to focus, up/down to select"

    def _refresh_teams(self) -> None:
        """Rebuild the team list every refresh, same convention _reload_board
        uses for the nomination board, so the Left column stays live without
        losing whichever team is highlighted."""
        teams = self.state.all_teams()
        previous = self.selected_team
        self.team_list.clear()
        for team in teams:
            label = Text(team, style="bold" if team == self.state.my_team else "")
            self.team_list.add_row(label, f"${self.state.budget_left(team)}")
        self._team_rows = teams
        if not teams:
            return
        if previous in teams:
            self.team_list.move_cursor(row=teams.index(previous))
        else:
            self.selected_team = teams[0]
            self.team_list.move_cursor(row=0)
            self._update_roster_title()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is not self.team_list:
            return
        if not self._team_rows or event.cursor_row >= len(self._team_rows):
            return
        team = self._team_rows[event.cursor_row]
        if team == self.selected_team:
            return
        self.selected_team = team
        self._update_roster_title()
        self._refresh_roster()

    def _select_team(self, arg: str | None) -> None:
        teams = self.state.all_teams()
        if arg is None:
            target = self.state.my_team
        elif arg.isdigit() and int(arg) in config.TEAMS:
            target = config.TEAMS[int(arg)]
        else:
            target = next((t for t in teams if t.lower() == arg.lower()), arg.upper())
        if target not in teams:
            self._output(f"[yellow]Unknown team: {arg}[/yellow]", clear=False)
            return
        self.selected_team = target
        if target in self._team_rows:
            self.team_list.move_cursor(row=self._team_rows.index(target))
        self._update_roster_title()
        self._refresh_roster()
        self._output(f"Now viewing {target}.", clear=False)

    def _refresh_roster(self) -> None:
        team = self.selected_team
        counts = self.state.position_counts(team)
        self.roster.budget_left = self.state.budget_left(team)
        self.roster.spots_left = self.state.spots_left(team)
        self.roster.max_bid_amount = self.state.max_bid(team)
        targets = config.ROSTER_TARGETS
        self.roster.slots = tuple(
            (pos, counts.get(pos, 0), required, targets.get(pos, required))
            for pos, required in config.STARTERS.items()
            if pos != "FLEX"
        )
        self.roster.bye_clash = auction.rostered_qb_bye_clash(self.state, team, self.vals)
        self.roster_table.clear()
        for p in self.state.purchases:
            if p.team == team:
                self.roster_table.add_row(p.player, p.position, f"${p.price}")

    def _refresh_analysis(self) -> None:
        pointer = self.ws.pointer
        if pointer.player_id is None:
            self.status.tier_line = ""
            self.status.verdict_line = ""
            self.status.bye_line = ""
            return

        name, _ = self.resolver.resolve(pointer.player_id)
        match = self.lookup.get(name.lower())
        taken = self.state.taken()

        if match:
            equivalent = auction.next_equivalent(
                self.vals, taken, match.position, match.tier, match.name)
            if equivalent is None:
                self.status.tier_line = (
                    f"[bold red]Tier {match.tier} {match.position} -- nothing "
                    f"equivalent left.[/bold red]")
            elif equivalent.tier == match.tier:
                self.status.tier_line = (
                    f"Tier {match.tier} {match.position} -- next: "
                    f"{equivalent.name} (${equivalent.value})")
            else:
                self.status.tier_line = (
                    f"[yellow]Tier {match.tier} {match.position} -- last one. "
                    f"Next tier: {equivalent.name} (${equivalent.value})[/yellow]")

            adjusted = self._adjusted(match)
            verdict = auction.bid_verdict(pointer.high_bid, adjusted)
            if adjusted is None:
                self.status.verdict_line = (
                    f"Verdict: [{verdict.style}]{verdict.label}[/{verdict.style}]")
            else:
                diff = pointer.high_bid - adjusted
                sign = "+" if diff >= 0 else "-"
                self.status.verdict_line = (
                    f"Verdict: [{verdict.style}]{verdict.label}[/{verdict.style}] "
                    f"by {sign}${abs(diff)} at ${pointer.high_bid}")

            if auction.qb_bye_would_clash(self.state, self.state.my_team, self.vals, match):
                self.status.bye_line = (
                    f"[bold red]Bye clash: you already have a QB on week "
                    f"{match.bye}.[/bold red]")
            else:
                self.status.bye_line = ""
        else:
            self.status.tier_line = f"[dim]{name} is not on your board.[/dim]"
            self.status.verdict_line = ""
            self.status.bye_line = ""

    def action_shutdown(self) -> None:
        self.exit()

    def action_dismiss_banner(self) -> None:
        # A busy command line owns escape for its own purposes (clearing
        # its text, in Textual's own Input widget); stealing it here would
        # eat a keystroke Mark meant for what he's typing.
        if self.command.has_focus:
            return
        self.banner.dismiss()

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

    def _reload_board(self) -> None:
        """Rebuild the board from the full priced player pool, filtered to
        who's still available and to the active search/position/star filter,
        then sorted. clear() resets the cursor to row 0, so the previously
        highlighted player's name is captured first and restored afterward
        -- same convention the old list-based reload used, just against a
        plain Python list instead of scraping mounted widgets."""
        previous_name = (self._board_rows[self.nominations.cursor_row].name
                          if self._board_rows else None)

        inflation = self.state.forward_inflation_by_position(self.vals)
        self._board_rows = auction.nomination_board(
            self.vals, self.state, inflation,
            starred=self.starred, query=self._board_query,
            position=self._board_position, starred_only=self._board_starred_only,
            sort=self._board_sort,
        )

        needs = self.state.needs(self.state.my_team)
        targets = self.state.targets(self.state.my_team)
        self.nominations.clear()
        for row in self._board_rows:
            self.nominations.add_row(*self._board_cells(row, needs, targets))

        if not self._board_rows:
            return
        if previous_name is not None:
            for i, row in enumerate(self._board_rows):
                if row.name == previous_name:
                    self.nominations.move_cursor(row=i)
                    return
        self.nominations.move_cursor(row=0)

    def _need_marker(self, position: str, needs: dict, targets: dict) -> Text:
        """!! for an unfilled starting slot, . for unfilled bench depth
        (config.ROSTER_TARGETS -- most consequential at QB, where the third
        exists for byes and the in-season waiver wire is empty), blank once
        the position is fully covered."""
        if needs.get(position, 0) > 0:
            return Text("!!", style="bold red")
        if targets.get(position, 0) > 0:
            return Text(".", style="yellow")
        return Text("")

    def _board_cells(self, row: "auction.BoardRow", needs: dict, targets: dict) -> tuple:
        v = row.valuation
        edge_style = ("green" if row.edge and row.edge > 0
                     else "red" if row.edge and row.edge < 0 else "dim")
        edge_cell = (Text.from_markup(f"[{edge_style}]{row.edge:+d}[/{edge_style}]")
                    if row.edge is not None else Text("-", style="dim"))
        return (
            "*" if row.starred else "",
            v.name,
            v.position,
            self._need_marker(v.position, needs, targets),
            f"T{v.tier}",
            str(v.bye) if v.bye else "-",
            f"${v.value}",
            f"${row.adjusted}" if row.adjusted is not None else "-",
            f"${v.espn_avg:.0f}" if v.espn_avg is not None else "-",
            edge_cell,
        )

    def action_nominate(self) -> None:
        if self.ws.pointer.nominating_team != config.MY_TEAM_ID:
            self._flash("[yellow]It is not your nomination turn.[/yellow]")
            return
        if not self._board_rows:
            self._flash("[yellow]Nothing highlighted to nominate.[/yellow]")
            return
        match = self._board_rows[self.nominations.cursor_row].valuation
        if match.espn_id is None:
            self._flash(f"[red]Unknown or unresolvable player: {match.name}[/red]")
            return
        try:
            self.ws.client.send_nomination(match.espn_id, 1)
        except RuntimeError as exc:
            self._flash(f"[red]Not sent:[/red] {exc} -- nominate in ESPN's own UI "
                        "if urgent.")
        else:
            self._flash(f"[green]Nominated {match.name} at $1.[/green]")

    def action_star(self) -> None:
        if not self._board_rows:
            return
        name = self._board_rows[self.nominations.cursor_row].name
        if name in self.starred:
            self.starred.discard(name)
        else:
            self.starred.add(name)
        self._save_starred()
        self._reload_board()

    def action_search(self) -> None:
        self.command.display = True
        self.command.value = "/"
        self.command.cursor_position = len(self.command.value)
        self.command.focus()

    def _save_starred(self) -> None:
        if self.nomination_list_path is None:
            return
        self._backup_nomination_list_once()
        auction.save_nomination_list(self.nomination_list_path, sorted(self.starred))

    def _backup_nomination_list_once(self) -> None:
        """Back up data/nomination-list.txt before the first write of this
        session, same convention as _backup_state_once below."""
        if self._nomination_list_backed_up:
            return
        self._nomination_list_backed_up = True
        path = self.nomination_list_path
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_text(path.read_text())

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
        # alert before it's been seen. dismiss() rather than a flat hide():
        # if an alert got queued behind the turn banner (superseded, not
        # dismissed), it must be revealed now rather than destroyed.
        if not self.banner.has_class("alert"):
            self.banner.dismiss()

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
        hotkey, plus the board's own search/filter/sort verbs. Tables come
        from auction.py's builders so both consoles show exactly the same
        numbers. Anything typed here replies into OutputLog; only what the
        auction itself does (a sent bid, a live sale) goes to the bid log."""
        if line.startswith("/"):
            self._board_query = line[1:].strip() or None
            self._reload_board()
            self._output(f"Search: {self._board_query or '(cleared)'}", clear=False)
            return

        cmd = line.split()
        head = cmd[0].lower()

        if head in ("quit", "exit", "q"):
            self.exit()
        elif head == "search":
            self._board_query = " ".join(cmd[1:]).strip() or None
            self._reload_board()
            self._output(f"Search: {self._board_query or '(cleared)'}", clear=False)
        elif head == "pos":
            arg = cmd[1].upper() if len(cmd) > 1 else "ALL"
            self._board_position = None if arg == "ALL" else arg
            self._reload_board()
            self._output(f"Position filter: {self._board_position or 'all'}", clear=False)
        elif head == "sort":
            key = cmd[1].lower() if len(cmd) > 1 else "rank"
            if key not in auction.NOMINATION_BOARD_SORTS:
                self._output(f"[yellow]Unknown sort key. Use one of: "
                            f"{', '.join(auction.NOMINATION_BOARD_SORTS)}[/yellow]", clear=False)
            else:
                self._board_sort = key
                self._reload_board()
                self._output(f"Sorted by {key}.", clear=False)
        elif head == "star":
            arg = cmd[1].lower() if len(cmd) > 1 else "only"
            self._board_starred_only = (arg != "all")
            self._reload_board()
            self._output("Showing starred only." if self._board_starred_only
                        else "Showing the full board.", clear=False)
        elif head == "clear":
            self._board_query = None
            self._board_position = None
            self._board_starred_only = False
            self._reload_board()
            self._output("Filters cleared.", clear=False)
        elif head == "b":
            self._start_bid(cmd[1:])
        elif head == "team":
            self._select_team(cmd[1] if len(cmd) > 1 else None)
        elif head == "me":
            table, footer = auction.me_table(self.state, self.vals)
            self._output(table)
            self._output(footer, clear=False)
        elif head == "teams":
            self._output(auction.teams_table(self.state))
        elif head == "market":
            self._output(auction.market_table(self.state, self.vals))
            self._output(f"Other teams still hold [bold]"
                        f"${self.state.dollars_remaining_in_room()}[/bold] combined.",
                        clear=False)
        elif head == "need":
            first = True
            for pos, count in self.state.needs(self.state.my_team).items():
                if count > 0:
                    self._output(auction.best_table(self.state, self.vals, pos, 6), clear=first)
                    first = False
        elif head == "best":
            pos = cmd[1] if len(cmd) > 1 and not cmd[1].isdigit() else None
            limit = next((int(c) for c in cmd[1:] if c.isdigit()), 15)
            self._output(auction.best_table(self.state, self.vals, pos, limit))
        elif head == "undo":
            removed = self.state.undo()
            self._output(f"Removed: {removed}" if removed else "Nothing to undo.", clear=False)
            self._refresh_panels()
            self._reload_board()
        else:
            self._output("[yellow]Unrecognized.[/yellow] Use: /name, pos, sort, star, "
                        "clear, team, b/undo/market/teams/best/need/me/quit", clear=False)


def run_ws_console(ws, state: draft_state.DraftState,
                   resolver: draft_sync.PlayerResolver,
                   vals: list[values.Valuation], nomination_list: list[str],
                   nomination_list_path=None) -> None:
    TextualWsApp(ws, state, resolver, vals, nomination_list, nomination_list_path).run()
