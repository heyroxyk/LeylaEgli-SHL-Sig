"""Render sig.template.svg + data.json into leyla.svg.

Nothing here draws anything. The template is hand-authored and stays that way;
this only computes the numbers that change and substitutes them into tokens.

leyla.svg is written only after the rendered string passes every check in
validate(). A stale signature is harmless. A broken one appears on every post
Leyla has ever made, so the failure mode we optimise for is "refuse to write".
"""
import json
import math
import pathlib
import re
import sys
import xml.etree.ElementTree as ElementTree
from decimal import Decimal, ROUND_HALF_UP

HERE = pathlib.Path(__file__).parent
TEMPLATE_PATH = HERE / "sig.template.svg"
DATA_PATH = HERE / "data.json"
OUTPUT_PATH = HERE / "leyla.svg"

CANVAS_WIDTH = 620.0
BAR_MAX_TPE = 2000.0

RADAR_CENTRE_X = 338.0
RADAR_CENTRE_Y = 78.0
RADAR_MAX_RADIUS = 54.0
RADAR_SCALE_MAX = 20.0

# Clockwise from the top. Order here is the order the polygon points are emitted,
# so it must match the axis captions baked into the template.
RADAR_AXES = (
    ("SKATING", ("acceleration", "agility", "speed"), -90),
    ("SENSE", ("offensiveRead", "defensiveRead", "positioning"), -30),
    ("PUCK", ("passing", "puckhandling"), 30),
    ("PHYSICAL", ("hitting", "checking", "strength", "fighting"), 90),
    ("STICK", ("stickchecking", "shotBlocking"), 150),
    ("MENTAL", ("determination", "leadership", "temperament", "professionalism"), 210),
)

LABEL_PAD = 6.0  # breathing room between a bar label and the zone edge it sits against

MIN_OUTPUT_BYTES = 24000  # the logo alone is ~18KB; anything near this lost the mark
SIZE_TOLERANCE = 0.10
MIN_LOGO_PATH_BYTES = 17000


class BuildError(Exception):
    """The signature cannot be rendered, or was rendered wrong. Never write on this."""


def round1(value):
    """Round half away from zero, so 100.95 gives 101.0 rather than banker's 100.9."""
    return float(Decimal(repr(float(value))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def fmt1(value):
    return f"{round1(value):.1f}"


def text_width(text, size=9.0, tracking=1.0):
    """Rough advance width for the .tpeLbl face.

    Verdana Bold digits and caps run near 0.62em and the class adds 1px of
    tracking per character. Only used to decide whether a label fits its zone,
    so an approximation with headroom is enough.
    """
    return len(text) * (size * 0.62 + tracking)


def format_height(raw):
    match = re.fullmatch(r"\s*(\d+)\s*ft\s*(\d+)\s*in\s*", raw)
    if not match:
        raise BuildError(f"cannot parse height {raw!r}; expected a form like '6ft 1in'")
    return f"{match.group(1)}'{match.group(2)}\""


def format_toi(total_seconds, games):
    if games <= 0:
        raise BuildError("cannot average time on ice over zero games played")
    per_game = round(total_seconds / games)
    return f"{per_game // 60}:{per_game % 60:02d}"


def radar_points(attributes):
    """Return [(x, y), ...] for the six grouped averages, plotted on a 0-20 scale."""
    points = []
    for name, keys, angle in RADAR_AXES:
        missing = [k for k in keys if k not in attributes]
        if missing:
            raise BuildError(f"radar axis {name} needs {', '.join(missing)}")
        average = sum(attributes[k] for k in keys) / len(keys)
        radius = RADAR_MAX_RADIUS * average / RADAR_SCALE_MAX
        radians = math.radians(angle)
        points.append(
            (
                RADAR_CENTRE_X + radius * math.cos(radians),
                RADAR_CENTRE_Y + radius * math.sin(radians),
            )
        )
    return points


def bar_geometry(total_tpe, applied_tpe):
    """Widths and label placement for the TPE bar.

    The bar's three zones are the three figures: the solid fill is applied TPE,
    the lighter fill running out to the head is banked, and the head is the
    total. Banked is therefore never labelled; it is the gap between the two.
    """
    if applied_tpe > total_tpe:
        raise BuildError(f"applied TPE {applied_tpe} exceeds total {total_tpe}")
    if total_tpe > BAR_MAX_TPE:
        raise BuildError(
            f"total TPE {total_tpe} overflows the {BAR_MAX_TPE:.0f} bar; the design needs rescaling"
        )

    total_px = round1(CANVAS_WIDTH * total_tpe / BAR_MAX_TPE)
    applied_px = round1(CANVAS_WIDTH * applied_tpe / BAR_MAX_TPE)

    applied_label = str(applied_tpe)
    total_label = f"{total_tpe} TPE"
    scale_label = "2000"

    # If the solid zone is too narrow to hold its own number, the number moves
    # out to the head rather than overrunning into the track. Only reachable
    # below roughly 100 applied TPE, i.e. a brand new player.
    if applied_px < text_width(applied_label) + 2 * LABEL_PAD:
        applied_label = ""
        total_label = f"{total_tpe} TPE  ·  {applied_tpe} APPLIED"

    # The 2000 scale marker is the first thing to go when the track gets short.
    track_room = CANVAS_WIDTH - total_px
    if track_room < text_width(total_label) + text_width(scale_label) + 3 * LABEL_PAD:
        scale_label = ""

    return {
        "TPE_TOTAL_PX": fmt1(total_px),
        "TPE_APPLIED_PX": fmt1(applied_px),
        "TPE_HEAD_X": fmt1(total_px),
        "TPE_APPLIED_LABEL_X": fmt1(applied_px - LABEL_PAD),
        "TPE_APPLIED_LABEL": applied_label,
        "TPE_TOTAL_LABEL_X": fmt1(total_px + LABEL_PAD),
        "TPE_TOTAL_LABEL": total_label,
        "TPE_SCALE_LABEL": scale_label,
    }


def stat_tokens(stats):
    games = stats["gamesPlayed"]
    shots = stats["shotsOnGoal"]
    advanced = stats["advanced"]
    shooting_pct = (stats["goals"] / shots * 100) if shots else 0.0
    return {
        "ST_GP": str(games),
        "ST_G": str(stats["goals"]),
        "ST_A": str(stats["assists"]),
        "ST_P": str(stats["points"]),
        "ST_PM": f"{stats['plusMinus']:+d}",
        "ST_PIM": str(stats["pim"]),
        "ST_SOG": str(shots),
        "ST_SHPCT": fmt1(shooting_pct),
        "ST_HITS": str(stats["hits"]),
        "ST_BLK": str(stats["shotsBlocked"]),
        "ST_TK": str(stats["takeaways"]),
        "ST_GV": str(stats["giveaways"]),
        "ST_CF": fmt1(advanced["CFPct"]),
        "ST_FF": fmt1(advanced["FFPct"]),
        "ST_PDO": fmt1(advanced["PDO"]),
        "ST_TOIGP": format_toi(stats["timeOnIce"], games),
        "ST_GF60": fmt1(advanced["GF60"]),
        "ST_GA60": fmt1(advanced["GA60"]),
        "ST_SF60": fmt1(advanced["SF60"]),
        "ST_SA60": fmt1(advanced["SA60"]),
        "ST_PPP": str(stats["ppPoints"]),
        "ST_SHP": str(stats["shPoints"]),
        "ST_PPTOI": format_toi(stats["ppTimeOnIce"], games),
        "ST_SHTOI": format_toi(stats["shTimeOnIce"], games),
    }


def build_tokens(data):
    player = data["player"]
    team = data["team"]
    stats = data["stats"]

    tokens = {
        "ARIA_LABEL": (
            f"{player['name']}, number {player['jerseyNumber']}, "
            f"{player['position']}, {team['name']}"
        ),
        "NUMBER": str(player["jerseyNumber"]),
        "NAME": player["name"].upper(),
        "POSITION": player["position"].upper(),
        "BIRTHPLACE": player["birthplace"].upper(),
        "DRAFT_CLASS": f"S{player['draftSeason']}",
        "HEIGHT": format_height(player["height"]),
        "WEIGHT": str(player["weight"]),
        "SHOOTS": player["handedness"].upper(),
        "TEAM": team["name"].upper(),
        "LEAGUE": player["currentLeague"].upper(),
        "SEASON": str(stats["season"]),
    }
    tokens.update(bar_geometry(player["totalTPE"], player["appliedTPE"]))
    tokens.update(stat_tokens(stats))

    points = radar_points(data["attributes"])
    tokens["RADAR_POINTS"] = " ".join(f"{fmt1(x)},{fmt1(y)}" for x, y in points)
    for index, (x, y) in enumerate(points, start=1):
        tokens[f"RADAR_X{index}"] = fmt1(x)
        tokens[f"RADAR_Y{index}"] = fmt1(y)
    return tokens


def render(template, tokens):
    unknown = set(re.findall(r"\{\{(\w+)\}\}", template)) - set(tokens)
    if unknown:
        raise BuildError(f"template uses tokens nothing supplies: {', '.join(sorted(unknown))}")
    rendered = template
    for name, value in tokens.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    return rendered


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _keyframe_width(svg, keyframe):
    match = re.search(
        r"@keyframes\s+" + keyframe + r"\s*\{.*?to\s*\{\s*width:\s*([0-9.]+)px",
        svg,
        re.DOTALL,
    )
    return match.group(1) if match else None


def _reduced_motion_width(svg, css_class):
    match = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{.*?\."
        + css_class
        + r"\s*\{[^}]*width:\s*([0-9.]+)px",
        svg,
        re.DOTALL,
    )
    return match.group(1) if match else None


def _rect_width(svg, css_class):
    match = re.search(r'<rect[^>]*class="' + css_class + r'"[^>]*width="([0-9.]+)"', svg)
    return match.group(1) if match else None


def check_bar_widths(svg):
    """The one thing most likely to end up half-updated.

    Each bar width is written in three independent places: the keyframe that
    animates it, the reduced-motion rule that pins it, and the rect attribute
    that renders it without CSS. Any one of them going stale is invisible until
    someone views the sig in the exact mode that reads the stale copy.
    """
    errors = []
    for keyframe, css_class in (("fillTotal", "tpeTotal"), ("fillApp", "tpeApp")):
        places = {
            f"@keyframes {keyframe}": _keyframe_width(svg, keyframe),
            f"reduced-motion .{css_class}": _reduced_motion_width(svg, css_class),
            f'<rect class="{css_class}">': _rect_width(svg, css_class),
        }
        absent = [where for where, value in places.items() if value is None]
        if absent:
            errors.append(f"{css_class}: width not found in {', '.join(absent)}")
            continue
        if len(set(places.values())) != 1:
            detail = ", ".join(f"{where}={value}" for where, value in places.items())
            errors.append(f"{css_class}: widths disagree across its three places: {detail}")
    return errors


def check_logo(svg):
    paths = re.findall(r'<path[^>]*\sd="([^"]+)"', svg)
    total = sum(len(d) for d in paths)
    if total < MIN_LOGO_PATH_BYTES:
        return [
            f"logo path data is {total} bytes across {len(paths)} paths, "
            f"want at least {MIN_LOGO_PATH_BYTES}; the club mark was eaten"
        ]
    return []


def check_labels_fit(svg):
    """A bar label that runs off the canvas would be clipped by the viewBox."""
    errors = []
    for match in re.finditer(r'<text\s([^>]*class="tpeLbl[^"]*"[^>]*)>([^<]*)</text>', svg):
        attributes, text = match.group(1), match.group(2)
        if not text.strip():
            continue
        anchor = re.search(r'\bx="([0-9.]+)"', attributes)
        if not anchor:
            errors.append(f"bar label {text!r} has no usable x attribute")
            continue
        x = float(anchor.group(1))
        width = text_width(text)
        ends_at_x = 'text-anchor="end"' in attributes
        left = x - width if ends_at_x else x
        right = x if ends_at_x else x + width
        if right > CANVAS_WIDTH:
            errors.append(
                f"bar label {text!r} ends at x={right:.1f}, past the {CANVAS_WIDTH:.0f} canvas"
            )
        if left < 0:
            errors.append(f"bar label {text!r} starts at x={left:.1f}, left of the canvas")
    return errors


def validate(svg, template):
    """Return a list of reasons this SVG must not be written. Empty means good."""
    errors = []

    try:
        ElementTree.fromstring(svg)
    except ElementTree.ParseError as exc:
        errors.append(f"output is not well-formed XML: {exc}")

    leftover = sorted(set(re.findall(r"\{\{\w*\}?\}?", svg)))
    if leftover:
        errors.append(f"unsubstituted tokens remain: {', '.join(leftover)}")

    errors.extend(check_bar_widths(svg))

    for required, label in (
        ("@media (prefers-color-scheme: light)", "light theme block"),
        ("@media (prefers-reduced-motion: reduce)", "reduced motion block"),
        ("@keyframes fillTotal", "fillTotal keyframes"),
        ("@keyframes fillApp", "fillApp keyframes"),
        ("@keyframes cycle", "ticker cycle keyframes"),
        ("@keyframes bloom", "radar bloom keyframes"),
    ):
        if required not in svg:
            errors.append(f"{label} did not survive the build ({required!r} missing)")

    errors.extend(check_logo(svg))
    errors.extend(check_labels_fit(svg))

    size = len(svg.encode("utf-8"))
    if size < MIN_OUTPUT_BYTES:
        errors.append(f"output is {size} bytes, below the {MIN_OUTPUT_BYTES} floor")
    reference = len(template.encode("utf-8"))
    drift = abs(size - reference) / reference
    if drift > SIZE_TOLERANCE:
        errors.append(
            f"output is {size} bytes against a {reference}-byte template, "
            f"a {drift:.0%} change; over the {SIZE_TOLERANCE:.0%} tolerance"
        )
    return errors


def build(template, data):
    """Render and validate. Raises BuildError rather than returning bad markup."""
    svg = render(template, build_tokens(data))
    errors = validate(svg, template)
    if errors:
        raise BuildError("refusing to write leyla.svg:\n  - " + "\n  - ".join(errors))
    return svg


def main():
    try:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        svg = build(template, data)
    except (OSError, json.JSONDecodeError, KeyError, BuildError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    OUTPUT_PATH.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote leyla.svg, {len(svg.encode('utf-8'))} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
