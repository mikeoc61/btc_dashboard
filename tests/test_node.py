"""The retarget projection's precision, and what it gates.

The projection is a pace estimate over the blocks found so far, so its error
shrinks by a factor of four across a difficulty period. Reported as a bare
level it is not comparable to the same level taken at a different point in that
period — which is the qualifier rule this project is built on, applied to the
one figure most likely to lead the page.
"""
from __future__ import annotations

import math

import pytest

from btc_dashboard.sources import node


def _rt(**kw):
    base = {"blocks_left": 1693, "blocks_elapsed": 323, "eta_days": 11.1,
            "projection_pct": 5.72, "projection_sigma_pct": 5.56}
    base.update(kw)
    return base


def _data(**kw):
    return {"height": 965987, "hash_rate_ehs": 932.0, "hash_rate_7d_pct": 1.55,
            "difficulty_t": 127.45, "retarget": _rt(**kw),
            "mempool": {"tx": 33877, "vmb": 11.8},
            "fees_sat_vb": {"fast": 2.1, "hour": 1.0, "day": 0.6}}


class TestProjectionSigma:
    """Block discovery is Poisson: n blocks take a time with relative standard
    deviation 1/sqrt(n), and the projection inherits it."""

    @pytest.mark.parametrize("n", [144, 323, 1008, 2016])
    def test_it_is_the_poisson_error_on_the_block_count(self, n):
        assert node.projection_sigma(n) == pytest.approx(100 / math.sqrt(n), abs=0.01)

    def test_it_shrinks_across_a_period(self):
        """The whole reason a fixed threshold could not work: the same level
        means four times as much at the end of a period as at the start."""
        floor = node.projection_sigma(node.MIN_BLOCKS_FOR_PROJ)
        full = node.projection_sigma(node.RETARGET_INTERVAL)
        assert floor > 8.0 and full < 2.5
        assert floor / full == pytest.approx(math.sqrt(14), abs=0.01)

    @pytest.mark.parametrize("bad", [None, 0, -5, "323", 12.5])
    def test_an_unusable_count_yields_no_band(self, bad):
        assert node.projection_sigma(bad) is None


class TestTheBandReachesEveryConsumer:
    """A qualifier carried in the snapshot and dropped by a renderer is the
    same defect as never computing it."""

    def test_all_three_presentations_state_it(self):
        d = _data()
        page = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Next Retarget")
        for where, text in (
                ("terminal", next(l for l in node.render_lines(d) if "proj" in l)),
                ("analyst", next(l for l in node.context_lines(d) if "retarget" in l)),
                ("page", page.note)):
            assert "5.6%" in text, f"{where} must state the band"
            assert "323" in text, f"{where} must state the count behind it"

    def test_the_analyst_is_told_how_many_errors_out_the_reading_is(self):
        """A model handed the level and the band still has to divide. Told
        '1.0 s.e. from flat' it cannot narrate the reading as a hashrate
        story without contradicting the sentence it was given."""
        line = next(l for l in node.context_lines(_data()) if "retarget" in l)
        assert "1.0 s.e. from flat" in line

    def test_a_snapshot_without_the_band_recovers_it_from_the_count(self):
        """`blocks_elapsed` predates the band in the schema, so an ingested
        snapshot written before it still holds what the band is computed
        from."""
        d = _data(projection_sigma_pct=None)
        page = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Next Retarget")
        assert "5.6%" in page.note

    def test_neither_band_nor_count_leaves_the_level_alone(self):
        """Silence, not a guess. The note still carries the period figures."""
        d = _data(projection_sigma_pct=None, blocks_elapsed=None)
        page = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Next Retarget")
        assert "±" not in page.note and "1,693 blks" in page.note

    def test_too_early_to_project_still_says_so(self):
        d = _data(projection_pct=None, projection_sigma_pct=None)
        page = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Next Retarget")
        assert page.value == "n/a" and "too early to project" in page.note


class TestTheNotableGateIsInSigmaNotPercent:
    """The regression this replaces: on 7 Sep 2026 the page led with
    '+5.7%' estimated off 323 blocks, where one standard error is 5.6%."""

    def test_a_one_sigma_reading_does_not_lead_the_page(self):
        assert node.notable(_data()) == []

    def test_the_same_level_late_in_the_period_does(self):
        out = node.notable(_data(blocks_elapsed=1700, blocks_left=316,
                                 projection_sigma_pct=None))
        assert len(out) == 1 and "+5.7%" in out[0]

    def test_the_old_fixed_threshold_no_longer_governs_either_end(self):
        """It fired below its own noise early, and stayed silent on readings
        that were unambiguous late."""
        loud_but_noisy = _data(blocks_elapsed=200, projection_sigma_pct=None,
                               projection_pct=6.5)
        quiet_but_real = _data(blocks_elapsed=2016, projection_sigma_pct=None,
                               projection_pct=4.6)
        assert node.notable(loud_but_noisy) == []
        assert len(node.notable(quiet_but_real)) == 1

    def test_the_strip_entry_carries_the_band(self):
        """Every other entry on the strip states the window it was ranked
        against; this one states the precision it was estimated at."""
        out = node.notable(_data(blocks_elapsed=1700, blocks_left=316,
                                 projection_sigma_pct=None))
        assert "±2.4%" in out[0] and "1,700 blks in" in out[0]

    def test_no_band_means_no_entry(self):
        """Rather than falling back to the fixed threshold this replaces —
        that gate is the thing being fixed, and an entry that silently used
        it would be the bug wearing the new code's name."""
        assert node.notable(_data(projection_sigma_pct=None,
                                  blocks_elapsed=None)) == []

    def test_it_fires_on_either_sign(self):
        out = node.notable(_data(blocks_elapsed=1700, blocks_left=316,
                                 projection_sigma_pct=None, projection_pct=-5.72))
        assert len(out) == 1 and "-5.7%" in out[0]
