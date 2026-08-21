"""Tests for the signature pipeline.

Run with either:
    python -m unittest test_build -v
    python -m pytest test_build.py -v

The validators get mutation tests as well as happy-path ones. A check that only
ever sees good input proves nothing; each one here is also shown a deliberately
broken signature and required to catch it.

No test touches the network. The API-shape tests feed fetch.py fabricated
payloads directly.
"""
import json
import pathlib
import re
import unittest
from unittest import mock

import build
import fetch

HERE = pathlib.Path(__file__).parent

# The attribute set that produced the polygon recorded in EXPECTED_RADAR.
ATTRIBUTES = {
    "screening": 5, "gettingOpen": 12, "passing": 14, "puckhandling": 14,
    "shootingAccuracy": 13, "shootingRange": 11, "offensiveRead": 14, "checking": 10,
    "hitting": 5, "positioning": 14, "stickchecking": 14, "shotBlocking": 11,
    "faceoffs": 5, "defensiveRead": 14, "acceleration": 15, "agility": 15,
    "balance": 13, "speed": 15, "stamina": 15, "strength": 14, "fighting": 5,
    "aggression": 5, "bravery": 10, "determination": 15, "teamPlayer": 15,
    "leadership": 15, "temperament": 15, "professionalism": 15,
}

EXPECTED_RADAR = "338.0,37.5 370.7,59.1 370.7,96.9 338.0,101.0 308.8,94.9 302.9,57.8"


def load_template():
    return (HERE / "sig.template.svg").read_text(encoding="utf-8")


def load_data():
    return json.loads((HERE / "data.json").read_text(encoding="utf-8"))


def render_current():
    return build.build(load_template(), load_data())


class RoundingTests(unittest.TestCase):
    def test_rounds_half_away_from_zero(self):
        # PHYSICAL 8.5 puts a radar vertex at exactly 100.95. Banker's rounding
        # would give 100.9 and silently shift the polygon off the design.
        self.assertEqual(build.round1(100.95), 101.0)
        self.assertEqual(build.round1(57.75), 57.8)

    def test_formats_to_one_decimal(self):
        self.assertEqual(build.fmt1(253.89), "253.9")
        self.assertEqual(build.fmt1(338), "338.0")


class RadarTests(unittest.TestCase):
    def test_reproduces_the_known_polygon(self):
        points = build.radar_points(ATTRIBUTES)
        rendered = " ".join(f"{build.fmt1(x)},{build.fmt1(y)}" for x, y in points)
        self.assertEqual(rendered, EXPECTED_RADAR)

    def test_full_ratings_reach_the_outer_ring(self):
        maxed = {name: 20 for name in ATTRIBUTES}
        top_x, top_y = build.radar_points(maxed)[0]
        self.assertAlmostEqual(top_x, 338.0, places=4)
        self.assertAlmostEqual(top_y, 78.0 - 54.0, places=4)

    def test_missing_attribute_is_named(self):
        incomplete = {k: v for k, v in ATTRIBUTES.items() if k != "agility"}
        with self.assertRaises(build.BuildError) as caught:
            build.radar_points(incomplete)
        self.assertIn("agility", str(caught.exception))


class BarGeometryTests(unittest.TestCase):
    def test_derives_widths_from_tpe(self):
        bar = build.bar_geometry(819, 425)
        self.assertEqual(bar["TPE_TOTAL_PX"], "253.9")
        self.assertEqual(bar["TPE_APPLIED_PX"], "131.8")
        self.assertEqual(bar["TPE_HEAD_X"], "253.9")

    def test_labels_sit_against_their_zone_edges(self):
        bar = build.bar_geometry(819, 425)
        self.assertEqual(bar["TPE_APPLIED_LABEL"], "425")
        self.assertEqual(bar["TPE_APPLIED_LABEL_X"], "125.8")  # 131.8 - 6 padding
        self.assertEqual(bar["TPE_TOTAL_LABEL"], "819 TPE")
        self.assertEqual(bar["TPE_TOTAL_LABEL_X"], "259.9")  # 253.9 + 6 padding
        self.assertEqual(bar["TPE_SCALE_LABEL"], "2000")

    def test_head_tracks_the_total_not_the_applied(self):
        self.assertEqual(build.bar_geometry(1000, 100)["TPE_HEAD_X"], "310.0")

    def test_narrow_applied_zone_evicts_its_label(self):
        # A new player with 40 applied TPE has a 12px solid zone, too small to
        # hold "40" without overrunning. It moves out to the head instead.
        bar = build.bar_geometry(300, 40)
        self.assertEqual(bar["TPE_APPLIED_LABEL"], "")
        self.assertIn("40 APPLIED", bar["TPE_TOTAL_LABEL"])
        self.assertIn("300 TPE", bar["TPE_TOTAL_LABEL"])

    def test_long_bar_drops_the_scale_marker_first(self):
        self.assertEqual(build.bar_geometry(1900, 1800)["TPE_SCALE_LABEL"], "")

    def test_rejects_applied_above_total(self):
        with self.assertRaises(build.BuildError):
            build.bar_geometry(400, 500)

    def test_rejects_total_past_the_bar_maximum(self):
        with self.assertRaises(build.BuildError):
            build.bar_geometry(2400, 1000)


class FormattingTests(unittest.TestCase):
    def test_parses_portal_height_form(self):
        self.assertEqual(build.format_height("6ft 1in"), "6'1\"")
        self.assertEqual(build.format_height("5ft 11in"), "5'11\"")

    def test_rejects_unfamiliar_height(self):
        with self.assertRaises(build.BuildError):
            build.format_height("185cm")

    def test_averages_time_on_ice_per_game(self):
        self.assertEqual(build.format_toi(96107, 66), "24:16")
        self.assertEqual(build.format_toi(9151, 66), "2:19")

    def test_pads_seconds(self):
        self.assertEqual(build.format_toi(1205, 5), "4:01")

    def test_rejects_zero_games(self):
        with self.assertRaises(build.BuildError):
            build.format_toi(1000, 0)

    def test_signs_plus_minus(self):
        regular = load_data()["stats"]["regular"]
        self.assertEqual(build.counting_tokens(dict(regular, plusMinus=-7), "ST")["ST_PM"], "-7")
        self.assertEqual(build.counting_tokens(dict(regular, plusMinus=21), "ST")["ST_PM"], "+21")

    def test_shooting_percentage_survives_zero_shots(self):
        regular = dict(load_data()["stats"]["regular"], shotsOnGoal=0, goals=0)
        self.assertEqual(build.counting_tokens(regular, "ST")["ST_SHPCT"], "0.0")

    def test_takeaway_differential_is_signed(self):
        regular = load_data()["stats"]["regular"]
        tokens = build.counting_tokens(dict(regular, takeaways=81, giveaways=22), "ST")
        self.assertEqual(tokens["ST_TKGV"], "+59")

    def test_takeaway_differential_goes_negative(self):
        regular = load_data()["stats"]["regular"]
        tokens = build.counting_tokens(dict(regular, takeaways=9, giveaways=21), "ST")
        self.assertEqual(tokens["ST_TKGV"], "-12")

    def test_takeaways_and_differential_recover_giveaways(self):
        """The card drops raw giveaways only because these two reconstruct them."""
        regular = load_data()["stats"]["regular"]
        tokens = build.counting_tokens(regular, "ST")
        self.assertEqual(
            int(tokens["ST_TK"]) - int(tokens["ST_TKGV"]), regular["giveaways"]
        )

    def test_prefix_selects_the_phase(self):
        regular = load_data()["stats"]["regular"]
        self.assertIn("PO_GP", build.counting_tokens(regular, "PO"))
        self.assertIn("ST_GP", build.counting_tokens(regular, "ST"))


class CycleTimingTests(unittest.TestCase):
    def test_six_cards_keep_the_original_loop(self):
        timings = build.cycle_timings(6)
        self.assertEqual(timings["CYCLE_SECONDS"], "21")
        self.assertEqual(timings["CYCLE_IN"], "2.5")
        self.assertEqual(timings["CYCLE_OUT"], "16.667")

    def test_ten_cards_lengthen_the_loop_rather_than_the_pace(self):
        timings = build.cycle_timings(10)
        self.assertEqual(timings["CYCLE_SECONDS"], "35")
        self.assertEqual(timings["CYCLE_OUT"], "10")

    def test_dwell_is_constant_whatever_the_card_count(self):
        """Adding cards must never speed the rotation up; a card that flashes past is unreadable."""
        for count in range(1, 13):
            seconds = float(build.cycle_timings(count)["CYCLE_SECONDS"])
            self.assertAlmostEqual(seconds / count, build.CARD_DWELL_SECONDS, places=6)

    def test_each_card_gets_a_distinct_slot(self):
        delays = build.cycle_timings(10)["CYCLE_DELAYS"]
        found = re.findall(r"\.c(\d+)\{animation-delay:(-[\d.]+)s\}", delays)
        self.assertEqual([c for c, _ in found], [str(i) for i in range(2, 11)])
        self.assertEqual(len(set(d for _, d in found)), 9)

    def test_last_card_trails_by_one_dwell(self):
        delays = build.cycle_timings(10)["CYCLE_DELAYS"]
        self.assertIn(".c10{animation-delay:-3.5s}", delays)

    def test_rejects_an_empty_rail(self):
        with self.assertRaises(build.BuildError):
            build.cycle_timings(0)


class PlayoffCardTests(unittest.TestCase):
    def setUp(self):
        self.template = load_template()
        self.data = load_data()

    def without_playoffs(self):
        stats = {k: v for k, v in self.data["stats"].items() if k != "playoffs"}
        return {**self.data, "stats": stats}

    def test_data_currently_has_a_playoff_run(self):
        self.assertIn("playoffs", self.data["stats"])

    def test_playoff_run_adds_four_cards(self):
        svg = build.build(self.template, self.data)
        self.assertEqual(build.count_cards(svg), 10)
        self.assertIn("PO GP", svg)
        self.assertIn("animation: cycle 35s", svg)

    def test_no_playoff_run_leaves_six_cards(self):
        svg = build.build(self.template, self.without_playoffs())
        self.assertEqual(build.count_cards(svg), 6)
        self.assertNotIn("PO GP", svg)
        self.assertIn("animation: cycle 21s", svg)

    def test_regular_season_cards_are_identical_either_way(self):
        """Adding the playoff block must not disturb the six cards already there."""
        with_po = build.build(self.template, self.data)
        without = build.build(self.template, self.without_playoffs())
        pattern = r'<g class="card c[1-6]">.*?</g>'
        self.assertEqual(
            re.findall(pattern, with_po, re.DOTALL), re.findall(pattern, without, re.DOTALL)
        )

    def test_possession_and_rate_metrics_stay_regular_season_only(self):
        """PDO over four games is luck, not talent. It must never appear as a PO card."""
        svg = build.build(self.template, self.data)
        for metric in ("PO PDO", "PO CF%", "PO FF%", "PO GF/60", "PO SF/60"):
            self.assertNotIn(metric, svg)

    def test_build_markers_do_not_reach_the_output(self):
        for payload in (self.data, self.without_playoffs()):
            self.assertNotIn("PLAYOFF_CARDS", build.build(self.template, payload))

    def test_rejects_a_template_with_no_playoff_block(self):
        with self.assertRaises(build.BuildError):
            build.prepare_template("<svg></svg>", True)

    def test_rejects_a_gap_in_the_card_numbering(self):
        with self.assertRaises(build.BuildError):
            build.count_cards('<g class="card c1"></g><g class="card c3"></g>')


class ThreePlacesTests(unittest.TestCase):
    """Each bar width is written in three places. All three must agree."""

    def setUp(self):
        self.svg = render_current()

    def test_total_width_agrees_across_all_three(self):
        self.assertEqual(build._keyframe_width(self.svg, "fillTotal"), "253.9")
        self.assertEqual(build._reduced_motion_width(self.svg, "tpeTotal"), "253.9")
        self.assertEqual(build._rect_width(self.svg, "tpeTotal"), "253.9")

    def test_applied_width_agrees_across_all_three(self):
        self.assertEqual(build._keyframe_width(self.svg, "fillApp"), "131.8")
        self.assertEqual(build._reduced_motion_width(self.svg, "tpeApp"), "131.8")
        self.assertEqual(build._rect_width(self.svg, "tpeApp"), "131.8")

    def test_clean_build_reports_no_disagreement(self):
        self.assertEqual(build.check_bar_widths(self.svg), [])

    def test_catches_a_stale_rect_attribute(self):
        broken = self.svg.replace('class="tpeTotal" fill="var(--accent)" fill-opacity="0.42" x="0" y="152" width="253.9"',
                                  'class="tpeTotal" fill="var(--accent)" fill-opacity="0.42" x="0" y="152" width="199.0"')
        self.assertNotEqual(broken, self.svg, "mutation did not apply")
        errors = build.check_bar_widths(broken)
        self.assertTrue(any("tpeTotal" in e and "disagree" in e for e in errors), errors)

    def test_catches_a_stale_keyframe(self):
        broken = self.svg.replace("to{width:253.9px}", "to{width:199.0px}")
        self.assertNotEqual(broken, self.svg, "mutation did not apply")
        errors = build.check_bar_widths(broken)
        self.assertTrue(any("tpeTotal" in e and "disagree" in e for e in errors), errors)

    def test_catches_a_stale_reduced_motion_rule(self):
        broken = self.svg.replace(".tpeApp{animation:none; width:131.8px}",
                                  ".tpeApp{animation:none; width:99.0px}")
        self.assertNotEqual(broken, self.svg, "mutation did not apply")
        errors = build.check_bar_widths(broken)
        self.assertTrue(any("tpeApp" in e and "disagree" in e for e in errors), errors)

    def test_catches_a_deleted_reduced_motion_rule(self):
        broken = self.svg.replace(".tpeApp{animation:none; width:131.8px}", "")
        errors = build.check_bar_widths(broken)
        self.assertTrue(any("tpeApp" in e for e in errors), errors)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.template = load_template()
        self.svg = render_current()

    def test_a_clean_build_validates(self):
        self.assertEqual(build.validate(self.svg, self.template), [])

    def test_output_parses_as_xml(self):
        import xml.etree.ElementTree as ElementTree
        ElementTree.fromstring(self.svg)  # raises on failure

    def test_catches_malformed_xml(self):
        broken = self.svg.replace("</svg>", "")
        errors = build.validate(broken, self.template)
        self.assertTrue(any("well-formed XML" in e for e in errors), errors)

    def test_no_tokens_remain(self):
        self.assertNotIn("{{", self.svg)

    def test_catches_a_leftover_token(self):
        broken = self.svg.replace("<rect class=\"rail\"", "{{FORGOTTEN}}<rect class=\"rail\"")
        errors = build.validate(broken, self.template)
        self.assertTrue(any("unsubstituted tokens" in e for e in errors), errors)

    def test_render_refuses_a_token_nothing_supplies(self):
        with self.assertRaises(build.BuildError) as caught:
            build.render("<svg>{{NOT_A_REAL_TOKEN}}</svg>", {"NAME": "x"})
        self.assertIn("NOT_A_REAL_TOKEN", str(caught.exception))

    def test_theme_and_motion_blocks_survive(self):
        for required in (
            "@media (prefers-color-scheme: light)",
            "@media (prefers-reduced-motion: reduce)",
            "@keyframes fillTotal",
            "@keyframes fillApp",
            "@keyframes cycle",
            "@keyframes bloom",
            "@keyframes headIn",
        ):
            self.assertIn(required, self.svg)

    def test_catches_a_lost_light_theme_block(self):
        broken = self.svg.replace("@media (prefers-color-scheme: light)", "@media print")
        errors = build.validate(broken, self.template)
        self.assertTrue(any("light theme" in e for e in errors), errors)

    def test_catches_a_lost_reduced_motion_block(self):
        broken = self.svg.replace("@media (prefers-reduced-motion: reduce)", "@media print")
        errors = build.validate(broken, self.template)
        self.assertTrue(any("reduced motion" in e for e in errors), errors)

    def test_logo_survives(self):
        self.assertEqual(build.check_logo(self.svg), [])

    def test_catches_an_eaten_logo(self):
        import re
        broken = re.sub(r'\sd="[^"]+"', ' d="M0,0"', self.svg)
        errors = build.validate(broken, self.template)
        self.assertTrue(any("club mark was eaten" in e for e in errors), errors)

    def test_output_size_is_sane(self):
        size = len(self.svg.encode("utf-8"))
        self.assertGreater(size, build.MIN_OUTPUT_BYTES)
        reference = len(self.template.encode("utf-8"))
        self.assertLessEqual(abs(size - reference) / reference, build.SIZE_TOLERANCE)

    def test_catches_a_collapsed_output(self):
        errors = build.validate("<svg></svg>", self.template)
        self.assertTrue(any("below the" in e for e in errors), errors)

    def test_bar_is_cut_into_four_segments(self):
        """The 500-TPE dividers span the full bar, not just a base tick."""
        import re
        for x in (155, 310, 465):
            divider = re.search(
                rf'<line stroke="var\(--bg\)" x1="{x}" y1="(\d+)" x2="{x}" y2="(\d+)"', self.svg
            )
            self.assertIsNotNone(divider, f"no segment divider at x={x}")
            self.assertEqual((divider.group(1), divider.group(2)), ("152", "168"))

    def test_segment_dividers_are_drawn_beneath_the_labels(self):
        """Draw order is what keeps a divider from striking through a number."""
        last_divider = self.svg.rindex('<line stroke="var(--bg)" x1="465"')
        first_label = self.svg.index('<text class="tpeLbl')
        self.assertLess(last_divider, first_label)

    def test_bar_labels_stay_on_canvas(self):
        self.assertEqual(build.check_labels_fit(self.svg), [])

    def test_catches_a_label_running_off_canvas(self):
        broken = self.svg.replace('x="259.9" y="163">819 TPE<', 'x="600.0" y="163">819 TPE<')
        self.assertNotEqual(broken, self.svg, "mutation did not apply")
        errors = build.check_labels_fit(broken)
        self.assertTrue(any("past the" in e for e in errors), errors)

    def test_build_raises_rather_than_returning_bad_markup(self):
        gutted = self.template.replace("@media (prefers-color-scheme: light)", "@media print")
        with self.assertRaises(build.BuildError):
            build.build(gutted, load_data())


class CommittedOutputTests(unittest.TestCase):
    def test_leyla_svg_matches_a_fresh_build(self):
        """leyla.svg is generated. If this fails, someone hand-edited it."""
        committed = (HERE / "leyla.svg").read_text(encoding="utf-8")
        self.assertEqual(
            committed,
            render_current(),
            "leyla.svg differs from a build of the committed template and data. "
            "Re-run build.py; do not edit leyla.svg by hand.",
        )


class FetchShapeTests(unittest.TestCase):
    def player_payload(self, **overrides):
        record = {
            "name": "Leyla Egli", "position": "Right Defense", "handedness": "Right",
            "height": "6ft 1in", "weight": 178, "birthplace": "Zug, Switzerland",
            "jerseyNumber": 76, "draftSeason": 87, "totalTPE": 819, "appliedTPE": 425,
            "bankedTPE": 394, "currentLeague": "SMJHL", "currentTeamID": 7,
            "attributes": dict(ATTRIBUTES),
            "indexRecords": [{"leagueID": 1, "indexID": 3192, "startSeason": 86}],
        }
        record.update(overrides)
        return [record]

    def test_accepts_a_well_formed_payload(self):
        with mock.patch.object(fetch, "get_json", return_value=self.player_payload()):
            player = fetch.fetch_player()
        self.assertEqual(player["totalTPE"], 819)

    def test_rejects_more_than_one_record(self):
        doubled = self.player_payload() * 2
        with mock.patch.object(fetch, "get_json", return_value=doubled):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_player()
        self.assertIn("want exactly 1", str(caught.exception))

    def test_rejects_an_empty_response(self):
        with mock.patch.object(fetch, "get_json", return_value=[]):
            with self.assertRaises(fetch.ShapeError):
                fetch.fetch_player()

    def test_names_every_missing_field_at_once(self):
        payload = self.player_payload()
        del payload[0]["totalTPE"]
        del payload[0]["birthplace"]
        with mock.patch.object(fetch, "get_json", return_value=payload):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_player()
        message = str(caught.exception)
        self.assertIn("totalTPE", message)
        self.assertIn("birthplace", message)

    def test_rejects_a_string_where_a_number_belongs(self):
        with mock.patch.object(fetch, "get_json", return_value=self.player_payload(totalTPE="819")):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_player()
        self.assertIn("totalTPE", str(caught.exception))

    def test_rejects_a_dropped_attribute(self):
        payload = self.player_payload()
        del payload[0]["attributes"]["speed"]
        with mock.patch.object(fetch, "get_json", return_value=payload):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_player()
        self.assertIn("speed", str(caught.exception))

    def test_rejects_an_attribute_out_of_range(self):
        payload = self.player_payload()
        payload[0]["attributes"]["speed"] = 44
        with mock.patch.object(fetch, "get_json", return_value=payload):
            with self.assertRaises(fetch.ShapeError):
                fetch.fetch_player()

    def test_rejects_an_unknown_league(self):
        payload = self.player_payload(currentLeague="ECHL")
        with mock.patch.object(fetch, "get_json", return_value=payload):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_player()
        self.assertIn("ECHL", str(caught.exception))

    def test_finds_the_index_id_for_the_current_league(self):
        player = self.player_payload()[0]
        player["indexRecords"] = [
            {"leagueID": 3, "indexID": 1407}, {"leagueID": 1, "indexID": 3192},
        ]
        self.assertEqual(fetch.find_index_id(player, 1), 3192)

    def test_rejects_a_missing_index_record(self):
        player = self.player_payload()[0]
        with self.assertRaises(fetch.ShapeError):
            fetch.find_index_id(player, 0)

    def test_reads_the_single_team_endpoint(self):
        team = {"id": 7, "name": "Detroit Falcons", "abbreviation": "DET"}
        with mock.patch.object(fetch, "get_json", return_value=team):
            self.assertEqual(fetch.fetch_team(7, 1)["name"], "Detroit Falcons")

    def test_rejects_a_team_that_is_not_the_one_asked_for(self):
        team = {"id": 4, "name": "Anchorage Armada", "abbreviation": "ANC"}
        with mock.patch.object(fetch, "get_json", return_value=team):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_team(7, 1)
        self.assertIn("got 4", str(caught.exception))

    def stats_payload(self, **overrides):
        record = {
            "season": 89, "gamesPlayed": 66, "goals": 11, "assists": 51, "points": 62,
            "plusMinus": 21, "pim": 50, "hits": 129, "shotsBlocked": 86, "takeaways": 81,
            "giveaways": 22, "shotsOnGoal": 157, "timeOnIce": 96107, "ppPoints": 19,
            "shPoints": 3, "ppTimeOnIce": 9313, "shTimeOnIce": 9151,
            "advancedStats": {"CFPct": 55.3, "FFPct": 55.9, "PDO": 99.5,
                              "GF60": 4.9, "GA60": 3.9, "SF60": 38.3, "SA60": 29.8},
        }
        record.update(overrides)
        return record

    def fetch_with(self, regular, playoffs):
        """fetch_stats calls the index twice: regular season first, then playoffs."""
        with mock.patch.object(fetch, "get_json", side_effect=[regular, playoffs]):
            return fetch.fetch_stats(3192, 1)

    def test_takes_the_newest_played_season(self):
        stats = self.fetch_with(
            [self.stats_payload(season=88), self.stats_payload(season=89)], []
        )
        self.assertEqual(stats["season"], 89)
        self.assertNotIn("playoffs", stats)

    def test_playoffs_attach_once_the_run_begins(self):
        stats = self.fetch_with(
            [self.stats_payload(season=89)],
            [self.stats_payload(season=89, gamesPlayed=4, points=5)],
        )
        self.assertEqual(stats["season"], 89)
        self.assertEqual(stats["playoffs"]["gamesPlayed"], 4)
        self.assertEqual(stats["regular"]["gamesPlayed"], 66)

    def test_playoffs_with_no_games_do_not_attach(self):
        """Between the regular season ending and game one, the block must stay away."""
        stats = self.fetch_with(
            [self.stats_payload(season=89)], [self.stats_payload(season=89, gamesPlayed=0)]
        )
        self.assertNotIn("playoffs", stats)

    def test_playoffs_from_an_older_season_are_ignored(self):
        stats = self.fetch_with(
            [self.stats_payload(season=89)],
            [self.stats_payload(season=88, gamesPlayed=21)],
        )
        self.assertEqual(stats["season"], 89)
        self.assertNotIn("playoffs", stats)

    def test_a_run_survives_the_offseason(self):
        """Nothing newer has regular-season games, so S89's run stays on the sig."""
        stats = self.fetch_with(
            [self.stats_payload(season=89), self.stats_payload(season=90, gamesPlayed=0)],
            [self.stats_payload(season=89, gamesPlayed=4)],
        )
        self.assertEqual(stats["season"], 89)
        self.assertIn("playoffs", stats)

    def test_the_first_game_of_the_new_season_drops_the_run(self):
        stats = self.fetch_with(
            [self.stats_payload(season=89), self.stats_payload(season=90, gamesPlayed=1)],
            [self.stats_payload(season=89, gamesPlayed=4)],
        )
        self.assertEqual(stats["season"], 90)
        self.assertNotIn("playoffs", stats)

    def test_rejects_missing_advanced_stats(self):
        with self.assertRaises(fetch.ShapeError) as caught:
            self.fetch_with([self.stats_payload(advancedStats={"CFPct": 55.3})], [])
        self.assertIn("PDO", str(caught.exception))

    def test_rejects_an_empty_stats_response(self):
        with self.assertRaises(fetch.ShapeError):
            self.fetch_with([], [])

    def test_regular_and_playoffs_are_requested_separately(self):
        with mock.patch.object(
            fetch, "get_json", side_effect=[[self.stats_payload(season=89)], []]
        ) as called:
            fetch.fetch_stats(3192, 1)
        phases = [call.args[0].rsplit("type=", 1)[-1] for call in called.call_args_list]
        self.assertEqual(phases, [fetch.REGULAR, fetch.PLAYOFFS])

    def test_preseason_is_never_requested(self):
        """Seven exhibition games must never displace a real season."""
        with mock.patch.object(
            fetch, "get_json", side_effect=[[self.stats_payload(season=89)], []]
        ) as called:
            fetch.fetch_stats(3192, 1)
        self.assertNotIn("preseason", " ".join(call.args[0] for call in called.call_args_list))


class DataFileTests(unittest.TestCase):
    def test_data_json_is_byte_stable(self):
        """An unchanged API must produce an unchanged file, or the cron commits daily."""
        raw = (HERE / "data.json").read_text(encoding="utf-8")
        rewritten = json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"
        self.assertEqual(raw, rewritten)

    def test_data_json_carries_every_attribute(self):
        self.assertEqual(set(load_data()["attributes"]), set(fetch.ATTRIBUTES))

    def test_stats_are_stored_per_phase(self):
        stats = load_data()["stats"]
        self.assertIn("season", stats)
        self.assertGreaterEqual(set(stats["regular"]), set(fetch.PHASE_FIELDS))
        self.assertIn("advanced", stats["regular"])
        if "playoffs" in stats:
            self.assertGreaterEqual(set(stats["playoffs"]), set(fetch.PHASE_FIELDS))
            # Advanced metrics are regular-season only, by design.
            self.assertNotIn("advanced", stats["playoffs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
