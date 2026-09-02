"""Unit tests for ff.projections: outside-source parsers and the name key."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ff import projections, scoring


def test_normalize_name_folds_punctuation_accents_and_suffixes():
    assert projections.normalize_name("Amon-Ra St. Brown") == "amonra st brown"
    assert projections.normalize_name("Travis Etienne Jr.") == "travis etienne"
    assert projections.normalize_name("Kenneth Walker III") == "kenneth walker"
    assert projections.normalize_name("Ja'Marr Chase") == "jamarr chase"
    assert projections.normalize_name("José Ramírez") == "jose ramirez"


CBS_QB_PAGE = """
<table><tr><th>Player</th></tr>
<tr><td><span>J. Allen</span> <span>QB</span> <span>BUF</span>
        <span>Josh Allen</span> <span>QB</span> <span>BUF</span></td>
<td>17</td><td>489</td><td>334</td><td>3,704</td><td>217.9</td><td>30</td><td>13</td>
<td>99.9</td><td>125</td><td>610</td><td>4.9</td><td>10</td><td>4</td><td>412.3</td><td>24.3</td></tr>
</table>
"""


def test_cbs_qb_row_maps_columns_and_scores_under_league_rules(monkeypatch):
    monkeypatch.setattr(projections, "_fetch", lambda *a, **k: CBS_QB_PAGE)
    rows = projections.cbs_projections(2026, positions=("QB",))
    assert len(rows) == 1
    row = rows[0]
    assert (row["name"], row["position"], row["pro_team"]) == ("Josh Allen", "QB", "BUF")
    assert row["stats"] == {
        "passing_yards": 3704.0, "passing_tds": 30.0, "interceptions": 13.0,
        "rushing_yards": 610.0, "rushing_tds": 10.0, "fumbles_lost": 4.0,
    }
    assert row["league_points"] == scoring.score_offense(row["stats"])
    assert row["source_points"] == 412.3


FFTODAY_RB_PAGE = """
<TABLE>
<TR><TD class="smallbody">&nbsp;</TD>
<TD class="smallbody">&nbsp;<A HREF="/stats/players/1/Jahmyr_Gibbs?LeagueID=1">Jahmyr Gibbs</A>  </TD>
<TD>DET</TD><TD>6</TD><TD>279</TD><TD>1,422</TD><TD>12</TD><TD>72</TD><TD>572</TD><TD>4</TD><TD>295.4</TD>
</TR>
<TR><TD><img src="x.gif"></TD><TD>not a player row</TD></TR>
</TABLE>
"""


def test_fftoday_rb_row_skips_icon_cell_and_maps_columns(monkeypatch):
    pages = iter([FFTODAY_RB_PAGE, "<TABLE></TABLE>"])
    monkeypatch.setattr(projections, "_fetch", lambda *a, **k: next(pages))
    rows = projections.fftoday_projections(2026, positions=("RB",))
    assert len(rows) == 1
    row = rows[0]
    assert (row["name"], row["pro_team"]) == ("Jahmyr Gibbs", "DET")
    assert row["stats"] == {
        "rushing_yards": 1422.0, "rushing_tds": 12.0, "receptions": 72.0,
        "receiving_yards": 572.0, "receiving_tds": 4.0,
    }
    assert row["source_points"] == 295.4


def test_fftoday_stops_paging_at_first_empty_page(monkeypatch):
    calls = []

    def fake_fetch(url, *a, **k):
        calls.append(url)
        return FFTODAY_RB_PAGE if len(calls) == 1 else "<TABLE></TABLE>"

    monkeypatch.setattr(projections, "_fetch", fake_fetch)
    projections.fftoday_projections(2026, positions=("RB",), pages=5)
    assert len(calls) == 2
