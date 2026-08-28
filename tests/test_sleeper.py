"""Unit test for sleeper.season_projections (T38).

Uses a canned fixture standing in for a Sleeper API response -- no live
network call belongs in the test suite.
"""

from __future__ import annotations

from ff import sleeper

QB_FIXTURE = [
    {
        "player": {
            "first_name": "Josh",
            "last_name": "Allen",
            "position": "QB",
            "team": "BUF",
        },
        "team": "BUF",
        "stats": {
            "pass_yd": 3650.0,
            "pass_td": 27.0,
            "pass_int": 10.0,
            "pass_2pt": 1.0,
            "rush_yd": 535.0,
            "rush_td": 11.0,
            "rush_2pt": 1.0,
            "fum_lost": 3.0,
            "adp_2qb": 3.0,
            "gp": 18.0,   # not in PROJECTION_STAT_MAP -- should be dropped
        },
    },
    {
        # Deep-bench QB with no scoreable projection, just ADP/gp -- should
        # still produce a row with an empty stats dict, not be skipped.
        "player": {
            "first_name": "Jake",
            "last_name": "Haener",
            "position": "QB",
            "team": "NO",
        },
        "team": "NO",
        "stats": {
            "adp_2qb": 545.6,
            "gp": 18.0,
        },
    },
]

RB_FIXTURE = [
    {
        "player": {
            "first_name": "Jahmyr",
            "last_name": "Gibbs",
            "position": "RB",
            "team": "DET",
        },
        "team": "DET",
        "stats": {
            "rush_yd": 1251.0,
            "rush_td": 12.0,
            "rush_2pt": 1.0,
            "rec": 63.0,
            "rec_yd": 533.0,
            "rec_td": 3.0,
            "fum_lost": 1.0,
            "adp_2qb": 1.3,
        },
    },
]


def _fake_position_projections(fixtures):
    def fake(season, position, force_refresh=False):
        return fixtures.get(position, [])
    return fake


def test_season_projections_maps_stat_keys_to_scoring_names(monkeypatch):
    monkeypatch.setattr(
        sleeper, "_position_projections", _fake_position_projections({"QB": QB_FIXTURE})
    )

    rows = sleeper.season_projections(2026, positions=("QB",))

    assert len(rows) == 2
    allen = next(r for r in rows if r["name"] == "Josh Allen")
    assert allen["position"] == "QB"
    assert allen["pro_team"] == "BUF"
    assert allen["adp_2qb"] == 3.0
    assert allen["stats"] == {
        "passing_yards": 3650.0,
        "passing_tds": 27.0,
        "interceptions": 10.0,
        "passing_2pt": 1.0,
        "rushing_yards": 535.0,
        "rushing_tds": 11.0,
        "rushing_2pt": 1.0,
        "fumbles_lost": 3.0,
    }


def test_season_projections_keeps_rows_with_no_scoreable_stats(monkeypatch):
    monkeypatch.setattr(
        sleeper, "_position_projections", _fake_position_projections({"QB": QB_FIXTURE})
    )

    rows = sleeper.season_projections(2026, positions=("QB",))

    haener = next(r for r in rows if r["name"] == "Jake Haener")
    assert haener["stats"] == {}
    assert haener["adp_2qb"] == 545.6


def test_season_projections_fetches_multiple_positions(monkeypatch):
    monkeypatch.setattr(
        sleeper,
        "_position_projections",
        _fake_position_projections({"QB": QB_FIXTURE, "RB": RB_FIXTURE}),
    )

    rows = sleeper.season_projections(2026, positions=("QB", "RB"))

    assert {r["name"] for r in rows} == {"Josh Allen", "Jake Haener", "Jahmyr Gibbs"}
    gibbs = next(r for r in rows if r["name"] == "Jahmyr Gibbs")
    assert gibbs["stats"]["receptions"] == 63.0
    assert gibbs["stats"]["receiving_yards"] == 533.0
    assert gibbs["stats"]["receiving_tds"] == 3.0


def test_season_projections_scores_under_league_rules(monkeypatch):
    """Mapped stats plug directly into scoring.score_offense (the whole
    point of matching its field names)."""
    from ff import scoring

    monkeypatch.setattr(
        sleeper, "_position_projections", _fake_position_projections({"QB": QB_FIXTURE})
    )

    rows = sleeper.season_projections(2026, positions=("QB",))
    allen = next(r for r in rows if r["name"] == "Josh Allen")
    points = scoring.score_offense(allen["stats"])

    # 3650*0.04 + 27*4 + 10*-2 + 1*2 + 535*0.1 + 11*6 + 1*2 + 3*-2
    assert points == 146.0 + 108.0 - 20.0 + 2.0 + 53.5 + 66.0 + 2.0 - 6.0


def test_position_projections_caches_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(sleeper, "CACHE_DIR", tmp_path)
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return QB_FIXTURE

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse()

    monkeypatch.setattr(sleeper.requests, "get", fake_get)

    first = sleeper._position_projections(2026, "QB")
    second = sleeper._position_projections(2026, "QB")

    assert first == QB_FIXTURE
    assert second == QB_FIXTURE
    assert len(calls) == 1   # second call served from cache, no re-fetch
    assert (tmp_path / "sleeper_projections_2026_QB.json").exists()

    url, params = calls[0]
    assert url.endswith("/2026")
    # "position[]" is Sleeper's actual key; a plain "position" silently
    # returns every position instead of the one asked for.
    assert params["position[]"] == "QB"
    assert params["season_type"] == "regular"
