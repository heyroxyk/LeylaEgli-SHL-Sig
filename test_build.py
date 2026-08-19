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
        stats = dict(load_data()["stats"], plusMinus=-7)
        self.assertEqual(build.stat_tokens(stats)["ST_PM"], "-7")
        stats = dict(load_data()["stats"], plusMinus=21)
        self.assertEqual(build.stat_tokens(stats)["ST_PM"], "+21")

    def test_shooting_percentage_survives_zero_shots(self):
        stats = dict(load_data()["stats"], shotsOnGoal=0, goals=0)
        self.assertEqual(build.stat_tokens(stats)["ST_SHPCT"], "0.0")


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

    def test_rejects_an_unknown_team_id(self):
        teams = [{"id": 4, "name": "Anchorage Armada", "abbreviation": "ANC"}]
        with mock.patch.object(fetch, "get_json", return_value=teams):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_team(7, 1)
        self.assertIn("no team with id 7", str(caught.exception))

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

    def test_takes_the_newest_played_season(self):
        payload = [self.stats_payload(season=88), self.stats_payload(season=89, goals=11)]
        with mock.patch.object(fetch, "get_json", return_value=payload):
            stats = fetch.fetch_stats(3192, 1)
        self.assertEqual(stats["season"], 89)

    def test_skips_a_season_with_no_games_yet(self):
        # At a rollover the index publishes the new season before anything is
        # simmed. Last season's real line beats a rail full of zeroes.
        payload = [self.stats_payload(season=89), self.stats_payload(season=90, gamesPlayed=0)]
        with mock.patch.object(fetch, "get_json", return_value=payload):
            stats = fetch.fetch_stats(3192, 1)
        self.assertEqual(stats["season"], 89)

    def test_rejects_missing_advanced_stats(self):
        payload = [self.stats_payload(advancedStats={"CFPct": 55.3})]
        with mock.patch.object(fetch, "get_json", return_value=payload):
            with self.assertRaises(fetch.ShapeError) as caught:
                fetch.fetch_stats(3192, 1)
        self.assertIn("PDO", str(caught.exception))

    def test_rejects_an_empty_stats_response(self):
        with mock.patch.object(fetch, "get_json", return_value=[]):
            with self.assertRaises(fetch.ShapeError):
                fetch.fetch_stats(3192, 1)


class DataFileTests(unittest.TestCase):
    def test_data_json_is_byte_stable(self):
        """An unchanged API must produce an unchanged file, or the cron commits daily."""
        raw = (HERE / "data.json").read_text(encoding="utf-8")
        rewritten = json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"
        self.assertEqual(raw, rewritten)

    def test_data_json_carries_every_attribute(self):
        self.assertEqual(set(load_data()["attributes"]), set(fetch.ATTRIBUTES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
