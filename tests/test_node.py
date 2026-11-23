"""The retarget projection's precision, and what it gates.

The projection is a pace estimate over the blocks found so far, so its error
shrinks by a factor of four across a difficulty period. Reported as a bare
level it is not comparable to the same level taken at a different point in that
period — which is the qualifier rule this project is built on, applied to the
one figure most likely to lead the page.
"""
from __future__ import annotations

import json
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


class TestColourAssertsASignThatIsReal:
    """Green and red are claims about direction, so they need a direction the
    number actually carries. `price.change_tone` makes the same claim against
    a fixed percentage; this one cannot, because the projection's error moves
    by a factor of four across a period."""

    def test_no_projection_is_not_a_projected_fall(self):
        """`n/a` used to fall to the `else` of a sign test and render red,
        which reads as a projected drop rather than as no projection."""
        d = _data(projection_pct=None, projection_sigma_pct=None)
        page = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Next Retarget")
        assert page.value == "n/a" and page.tone is None

    def test_a_sub_sigma_reading_is_left_uncoloured(self):
        assert node.retarget_tone(_rt(projection_pct=0.5)) is None
        assert node.retarget_tone(_rt(projection_pct=-0.5)) is None

    def test_a_reading_past_one_sigma_keeps_its_colour(self):
        assert node.retarget_tone(_rt(projection_pct=8.0)) == "up"
        assert node.retarget_tone(_rt(projection_pct=-8.0)) == "down"

    def test_the_dead_band_widens_early_in_the_period(self):
        """The same level, coloured late and neutral early — which is the
        whole reason a fixed percentage could not do this job."""
        late = _rt(projection_pct=4.0, blocks_elapsed=2016,
                   projection_sigma_pct=None)
        early = _rt(projection_pct=4.0, blocks_elapsed=323,
                    projection_sigma_pct=None)
        assert node.retarget_tone(late) == "up"
        assert node.retarget_tone(early) is None

    def test_colour_is_a_lower_bar_than_the_strip(self):
        """Between 1σ and 2σ the card commits to a direction and the strip
        stays quiet. At 2σ the colour would say nothing the strip had not."""
        rt = _rt(projection_pct=6.09, blocks_elapsed=327,
                 projection_sigma_pct=None)
        assert node.retarget_tone(rt) == "up"
        assert node.notable(_data(**{k: v for k, v in rt.items()
                                     if k != "blocks_left"})) == []

    def test_no_band_means_no_colour(self):
        """Consistent with the strip: without the band there is nothing to
        establish the sign against."""
        assert node.retarget_tone(_rt(projection_sigma_pct=None,
                                      blocks_elapsed=None)) is None

    def test_the_colour_reaches_the_markup(self):
        from btc_dashboard import html as page
        panel = node.html_panels(_data(projection_pct=8.0))[0]
        rows = page._rows(panel.metrics)
        assert 'class="value up"' in rows


def _fake_cli(fee, netinfo=lambda: {"version": 320100,
                                     "subversion": "/Satoshi:32.1.0/"}):
    """A node whose fee calls answer with `fee(args)`; everything else healthy."""
    calls = []

    def cli(cfg, *args):
        calls.append(args)
        cmd = args[0]
        if cmd == "getblockcount":
            return 977119
        if cmd == "getnetworkhashps":
            return 1.0e21
        if cmd == "getdifficulty":
            return 1.348e14
        if cmd == "getmempoolinfo":
            return {"size": 68849, "bytes": 30928268}
        if cmd == "getblockhash":
            return f"h{args[1]}"
        if cmd == "getblockheader":
            return {"time": 1_795_000_000 + int(args[1][1:]) * 600}
        if cmd == "estimatesmartfee":
            return fee(args)
        if cmd == "getnetworkinfo":
            return netinfo()
        raise AssertionError(f"unexpected call {args}")

    return cli, calls


class TestFeeEstimates:
    """Core 32 changed what `estimatesmartfee` returns by default, and on the
    upgrade discarded the fee history it had. Both reached the page as a
    figure that changed meaning, or as `n/a` with no reason."""

    def test_every_fee_call_pins_the_block_policy_estimator(self, monkeypatch):
        """Under 32's `auto` the three figures could each be on a different
        basis, and change basis between ticks while the mempool estimator
        warms up."""
        cli, calls = _fake_cli(lambda a: {"feerate": 0.00002, "blocks": int(a[1])})
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        fee_calls = [a for a in calls if a[0] == "estimatesmartfee"]
        assert [a[1] for a in fee_calls] == ["2", "6", "144"]
        for a in fee_calls:
            assert a[2] == "economical"
            assert json.loads(a[3]) == {"fee_rate_estimator": "block_policy"}
        assert res.data["fees_sat_vb"] == {"fast": 2.0, "hour": 2.0, "day": 2.0}
        assert res.data["fees_error"] is None

    def test_a_rejected_fee_call_costs_that_fee_not_the_block(self, monkeypatch):
        """A pre-32 node rejects the options argument outright. That used to
        reach `collect()` and take hashrate, difficulty and mempool with it."""
        def fee(a):
            if a[1] == "2":
                raise node.NodeError("error code: -8 unknown option")
            return {"feerate": 0.00001, "blocks": int(a[1])}
        cli, _ = _fake_cli(fee)
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        assert res.available
        assert res.data["hash_rate_ehs"] == 1000.0
        assert res.data["fees_sat_vb"] == {"fast": None, "hour": 1.0, "day": 1.0}
        assert "unknown option" in res.data["fees_error"]

    def test_the_nodes_reason_is_kept(self, monkeypatch):
        cli, _ = _fake_cli(lambda a: {"errors": ["Insufficient data or no feerate found"],
                                      "blocks": 1})
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        assert res.data["fees_sat_vb"] == {"fast": None, "hour": None, "day": None}
        assert res.data["fees_error"] == "Insufficient data or no feerate found"

    def test_a_wrong_typed_feerate_is_one_missing_value(self, monkeypatch):
        cli, _ = _fake_cli(lambda a: {"feerate": "0.0001", "blocks": 6} if a[1] == "6"
                           else {"feerate": 0.00001, "blocks": int(a[1])})
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        assert res.available and res.data["fees_sat_vb"]["hour"] is None

    def test_an_estimate_clamped_to_a_shorter_target_is_na(self, monkeypatch):
        """After the 32.1 upgrade the node had history for two blocks and
        answered all three requests at that target. The 1hr and 1d slots
        then held a 2-block rate under a longer label."""
        cli, _ = _fake_cli(lambda a: {"feerate": 0.00000121, "blocks": 2})
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        assert res.data["fees_sat_vb"] == {"fast": 0.1, "hour": None, "day": None}
        assert res.data["fees_error"] == "estimate only available for 2 blocks yet"
        term = next(l for l in node.render_lines(res.data) if l.startswith("fees"))
        assert "0.1/n/a/n/a" in term and "only available for 2 blocks" in term

    def test_an_estimate_at_a_longer_target_is_na_too(self, monkeypatch):
        """Same error the other way round: a 3-block rate is not the fast one."""
        cli, _ = _fake_cli(lambda a: {"feerate": 0.00001,
                                      "blocks": 3 if a[1] == "2" else int(a[1])})
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        assert res.data["fees_sat_vb"] == {"fast": None, "hour": 1.0, "day": 1.0}
        assert "3 blocks" in res.data["fees_error"]

    @pytest.mark.parametrize("blocks", [None, "6", 6.0, True])
    def test_a_missing_or_wrong_typed_target_is_a_mismatch(self, monkeypatch, blocks):
        """A rate whose horizon cannot be read cannot be put under one."""
        est = {"feerate": 0.00001}
        if blocks is not None:
            est["blocks"] = blocks
        cli, _ = _fake_cli(lambda a: est if a[1] == "6"
                           else {"feerate": 0.00001, "blocks": int(a[1])})
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        assert res.data["fees_sat_vb"]["hour"] is None
        assert res.data["fees_sat_vb"]["fast"] == 1.0

    def test_both_presentations_say_why_fees_are_missing(self):
        d = dict(_data(), fees_sat_vb={"fast": None, "hour": None, "day": None},
                 fees_error="Insufficient data or no feerate found")
        term = next(l for l in node.render_lines(d) if l.startswith("fees"))
        card = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Fee Estimates")
        for text in (term, card.note):
            assert "node: Insufficient data or no feerate found" in text

    def test_both_presentations_state_the_basis(self):
        d = _data()
        term = next(l for l in node.render_lines(d) if l.startswith("fees"))
        card = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Fee Estimates")
        assert "block policy" in term and "block policy" in card.note

    def test_no_reason_is_shown_when_every_fee_is_present(self):
        """A stale reason beside three good figures would read as a fault."""
        d = dict(_data(), fees_error="Insufficient data or no feerate found")
        term = next(l for l in node.render_lines(d) if l.startswith("fees"))
        assert "node:" not in term

    def test_the_reason_cannot_break_the_frame(self):
        """It is the node's text, or an ingested snapshot's: a newline would
        start a line at column 0 and ESC would reach the reader's terminal."""
        d = dict(_data(), fees_sat_vb={"fast": None, "hour": 1.0, "day": 0.5},
                 fees_error="bad\nNODE\n  forged\x1b[2J" + "x" * 500)
        term = next(l for l in node.render_lines(d) if l.startswith("fees"))
        card = next(m for m in node.html_panels(d)[0].metrics
                    if m.label == "Fee Estimates")
        for text in (term, card.note):
            assert "\n" not in text and "\x1b" not in text
            assert "(truncated)" in text


def _snap(data, available=True):
    return {"generated_at": "2026-11-22T23:59:00+00:00",
            "sources": {"node": {"available": available, "data": data,
                                 "error": None if available else "no node"}}}


def _ok_fee(a):
    return {"feerate": 0.00001, "blocks": int(a[1])}


class TestCoreVersion:
    """The node's version, in the header of the page and the terminal.

    Added after the 32.1 upgrade changed what the fee figures meant with
    nothing on the page saying a different node was behind them."""

    def test_it_is_collected_as_the_number(self, monkeypatch):
        cli, _ = _fake_cli(_ok_fee)
        monkeypatch.setattr(node, "_cli", cli)
        assert node.collect(None).data["core_version"] == 320100

    def test_a_failed_call_costs_the_version_not_the_block(self, monkeypatch):
        def broken():
            raise node.NodeError("getnetworkinfo refused")
        cli, _ = _fake_cli(_ok_fee, netinfo=broken)
        monkeypatch.setattr(node, "_cli", cli)
        res = node.collect(None)
        assert res.available and res.data["core_version"] is None
        assert res.data["hash_rate_ehs"] == 1000.0

    @pytest.mark.parametrize("v,label", [(320100, "(Core 32.1.0)"),
                                         (300500, "(Core 30.5.0)"),
                                         (310201, "(Core 31.2.1)")])
    def test_the_number_formats_as_major_minor_patch(self, v, label):
        assert node.version_label(_snap({"core_version": v})) == label

    @pytest.mark.parametrize("bad", [None, "32.1.0", "/Satoshi:32.1.0/\n", 32.1,
                                     True, 0, -1])
    def test_anything_but_a_positive_int_shows_nothing(self, bad):
        """An ingested snapshot can put text here; only digits may reach a header."""
        assert node.version_label(_snap({"core_version": bad})) == ""

    def test_no_node_shows_nothing(self):
        assert node.version_label(_snap({"core_version": 320100}, available=False)) == ""
        assert node.version_label({}) == ""


class TestCoreVersionInTheHeaders:
    def test_the_page_header_carries_it_in_a_live_span(self):
        from btc_dashboard import html
        page = html.render_html(_snap({"core_version": 320100}))
        assert ('<h1>BTC DASHBOARD <span class="meta" id="corever">'
                '(Core 32.1.0)</span></h1>') in page
        assert "corever" in html.LIVE_IDS

    def test_a_tick_carries_it_so_an_upgrade_reaches_an_open_tab(self):
        from btc_dashboard import html
        live = html.render_live(_snap({"core_version": 320100}))
        assert 'id="corever">(Core 32.1.0)</span>' in live

    def test_with_no_node_the_span_is_empty_but_present(self):
        """A later tick has to find it to fill it."""
        from btc_dashboard import html
        page = html.render_html(_snap({}, available=False))
        assert '<span class="meta" id="corever"></span>' in page

    def test_the_terminal_header_carries_it(self):
        from btc_dashboard import render
        out = render.render(_snap({"core_version": 320100}), color=False)
        assert out.splitlines()[0].startswith("BTC DASHBOARD (Core 32.1.0) — ")

    def test_the_terminal_header_is_unchanged_without_a_node(self):
        from btc_dashboard import render
        out = render.render(_snap({}, available=False), color=False)
        assert out.splitlines()[0].startswith("BTC DASHBOARD — ")
