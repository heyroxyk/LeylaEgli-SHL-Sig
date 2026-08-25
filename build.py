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
LOGO_PATH = HERE / "logo.svg"
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

# Ticker pacing. Every card holds for the same length of time no matter how many
# are in the rotation, so adding cards lengthens the loop rather than speeding it
# up. A card that flashes past cannot be read at all; a card late in a long loop
# is at least legible to anyone who lingers.
CARD_DWELL_SECONDS = 3.5
CARD_FADE_IN = 0.15   # fraction of a card's turn spent fading in
CARD_FADE_OUT = 0.85  # fraction at which it starts fading out

REGULAR_CARDS = 6
PLAYOFF_CARDS = 4

MIN_OUTPUT_BYTES = 24000  # a signature this small has lost either the crest or the rail
SIZE_TOLERANCE = 0.10


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


def counting_tokens(stats, prefix):
    """Figures that mean the same thing over four games as over sixty-six.

    Goals, hits and blocks are facts about what happened. They are simply small
    when the sample is small, which is honest. Contrast advanced_tokens below.
    """
    games = stats["gamesPlayed"]
    shots = stats["shotsOnGoal"]
    shooting_pct = (stats["goals"] / shots * 100) if shots else 0.0
    return {
        f"{prefix}_GP": str(games),
        f"{prefix}_G": str(stats["goals"]),
        f"{prefix}_A": str(stats["assists"]),
        f"{prefix}_P": str(stats["points"]),
        f"{prefix}_PM": f"{stats['plusMinus']:+d}",
        f"{prefix}_PIM": str(stats["pim"]),
        f"{prefix}_SOG": str(shots),
        f"{prefix}_SHPCT": fmt1(shooting_pct),
        f"{prefix}_HITS": str(stats["hits"]),
        f"{prefix}_BLK": str(stats["shotsBlocked"]),
        f"{prefix}_TK": str(stats["takeaways"]),
        f"{prefix}_GV": str(stats["giveaways"]),
        # Shown instead of raw giveaways. Takeaways and this net recover the
        # giveaway count exactly, so the card loses nothing by carrying it.
        f"{prefix}_TKGV": f"{stats['takeaways'] - stats['giveaways']:+d}",
        f"{prefix}_PPP": str(stats["ppPoints"]),
        f"{prefix}_SHP": str(stats["shPoints"]),
        f"{prefix}_TOIGP": format_toi(stats["timeOnIce"], games),
        f"{prefix}_PPTOI": format_toi(stats["ppTimeOnIce"], games),
        f"{prefix}_SHTOI": format_toi(stats["shTimeOnIce"], games),
    }


def advanced_tokens(stats, prefix):
    """Possession and rate metrics, which estimate true talent rather than record events.

    Deliberately regular-season only. Over a short playoff run these are noise:
    PDO regresses to 100 by construction, so a four-game 113.3 describes luck,
    not the player, and putting it on the sig beside a 66-game number would
    invite exactly the wrong comparison.
    """
    advanced = stats["advanced"]
    return {
        f"{prefix}_CF": fmt1(advanced["CFPct"]),
        f"{prefix}_FF": fmt1(advanced["FFPct"]),
        f"{prefix}_PDO": fmt1(advanced["PDO"]),
        f"{prefix}_GF60": fmt1(advanced["GF60"]),
        f"{prefix}_GA60": fmt1(advanced["GA60"]),
        f"{prefix}_SF60": fmt1(advanced["SF60"]),
        f"{prefix}_SA60": fmt1(advanced["SA60"]),
    }


def trim(value, places):
    """Shortest CSS-safe form: 21 rather than 21.00, 14.167 rather than 14.16700."""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def cycle_timings(card_count):
    """Animation timings for a rail of card_count cards.

    The keyframe percentages are one card's turn expressed against the whole
    loop, so they have to move with the card count. Hardcoding them is what
    silently breaks the rotation when a card is added.
    """
    if card_count < 1:
        raise BuildError("the ticker rail needs at least one card")
    dwell = CARD_DWELL_SECONDS
    total = dwell * card_count
    slot = 100.0 / card_count  # one card's share of the loop, as a percentage

    # Negative delays start each card partway through the loop, so card i comes
    # up at (i-1) * dwell seconds. Card 1 needs none; it leads.
    delays = " ".join(
        f".c{i}{{animation-delay:-{trim(total - (i - 1) * dwell, 2)}s}}"
        for i in range(2, card_count + 1)
    )
    return {
        "CYCLE_SECONDS": trim(total, 2),
        "CYCLE_DELAYS": delays,
        "CYCLE_IN": trim(slot * CARD_FADE_IN, 3),
        "CYCLE_HOLD": trim(slot * CARD_FADE_OUT, 3),
        "CYCLE_OUT": trim(slot, 3),
    }


# --------------------------------------------------------------------------
# club mark
#
# The crest is fetched into logo.svg from the league's sprite stack, so it
# changes on its own when she is traded. What cannot be automatic is how a
# given crest sits in the medallion: the marks differ in kind, not degree.
# --------------------------------------------------------------------------

MEDALLION_RADIUS = 42.0
MARK_CLIP_REACH = 95.0  # how far past the ring a break-out sector extends

# Hand-tuned per club. Detroit's crest is compact and fills a square, so it
# needs nothing. Tampa Bay's is a wide lockup whose wordmark spells out the club
# name already printed under the medallion and turns to mush at 84px, so that
# band is dropped and the remainder scaled up.
#   drop   - discard paths lying wholly within this (y0, y1) band of the source
#   box    - size the remaining artwork is fitted to, against the 84px ring
#   focus  - point of the source artwork to sit on the medallion's centre;
#            defaults to the middle of the viewBox
#   breaks - arc, clockwise from twelve, through which artwork may cross the ring
#
# Measure `breaks` from the RENDERED artwork, never from path coordinates, and
# re-measure it whenever `box` or `focus` changes, because both move the artwork
# against the ring. A filled shape covers far more arc than its corner points do:
# this spike has vertices spanning 43-49 degrees but fills 42-62, and a wedge cut
# to the vertices takes a notch out of it. The crest leaves the ring in five
# places; these bounds pass the spike (42-62), the jaw edge (87-92) and the snout
# (104-123), and hold back the two triangle corners at 139-176 and 263-279.
# Tampa Bay's focus is the fish's own ink centre, measured from a render. With
# the wordmark dropped, the artwork's geometric middle falls 109 units below the
# fish, because the triangle keeps running on past it, so centring on the box
# would leave the fish riding high in the medallion.
MARK_PRESENTATION = {
    "Tampa_Bay": {"drop": (410.0, 760.0), "box": 104.0,
                  "focus": (500.0, 337.0), "breaks": (32.0, 130.0)},
    "Detroit": {"drop": None, "box": 80.0, "focus": None, "breaks": None},
}
DEFAULT_PRESENTATION = {"drop": None, "box": 80.0, "focus": None, "breaks": None}

# SVG lets numbers run together wherever the next one starts with a sign or a
# decimal point, so "758.629.943-3.8" is three numbers and the data cannot be
# split on whitespace.
PATH_NUMBER = re.compile(r"[+-]?(?:\d*\.\d+|\d+\.?)(?:[eE][+-]?\d+)?")
PATH_ARGC = {"m": 2, "l": 2, "h": 1, "v": 1, "c": 6, "s": 4, "q": 4, "t": 2, "a": 7, "z": 0}


def path_y_range(d):
    """Vertical extent of one path, in its own user units.

    Control points count toward the range rather than being solved for, which
    overstates the extent slightly. That is the safe direction: it can only make
    a path look too tall to drop, never too short.
    """
    y = start_y = 0.0
    seen = []
    for command, arguments in re.findall(
        r"([MmLlHhVvCcSsQqTtAaZz])([^MmLlHhVvCcSsQqTtAaZz]*)", d
    ):
        low, relative = command.lower(), command.islower()
        count = PATH_ARGC[low]
        numbers = [float(m.group()) for m in PATH_NUMBER.finditer(arguments)]
        if count == 0:
            y = start_y
            continue
        for offset in range(0, max(len(numbers) - count + 1, 0), count):
            group = numbers[offset : offset + count]
            if low == "h":
                pass
            elif low == "v":
                y = y + group[0] if relative else group[0]
            elif low == "a":
                y = y + group[6] if relative else group[6]
            else:
                for index in range(1, count - 1, 2):
                    seen.append(y + group[index] if relative else group[index])
                y = y + group[count - 1] if relative else group[count - 1]
            seen.append(y)
            if low == "m":
                start_y = y
                low = "l"  # pairs after a moveto are implicit linetos
    return (min(seen), max(seen)) if seen else None


def read_mark(markup):
    """Split the fetched crest into its viewBox and its drawable body."""
    opening = re.match(r"<svg\b[^>]*>", markup.strip())
    if not opening:
        raise BuildError("logo.svg does not start with an <svg> element")
    view_box = re.search(r'viewBox="([^"]+)"', opening.group(0))
    if not view_box:
        raise BuildError("logo.svg has no viewBox, so the mark cannot be scaled")
    numbers = [float(n) for n in view_box.group(1).split()]
    if len(numbers) != 4 or numbers[2] <= 0 or numbers[3] <= 0:
        raise BuildError(f"logo.svg viewBox {view_box.group(1)!r} is not a usable box")
    identifier = re.search(r'\sid="([^"]+)"', opening.group(0))
    body = markup.strip()[len(opening.group(0)) : markup.strip().rindex("</svg>")]
    return identifier.group(1) if identifier else "", numbers, body


def drop_band(body, band):
    """Remove paths lying wholly inside a horizontal band of the source artwork.

    Wholly inside, not merely overlapping: the club's backing shapes run the full
    height of the mark and must survive, while the lettering sits entirely within
    the band. Anything straddling it is kept, because dropping it would take a
    bite out of artwork that is still wanted.
    """
    if band is None:
        return body
    low, high = band
    kept, dropped = [], 0
    for element in re.findall(r"<path\b[^>]*?/>|<path\b[^>]*?>", body):
        data = re.search(r'\sd="([^"]+)"', element)
        extent = path_y_range(data.group(1)) if data else None
        if extent and low <= extent[0] and extent[1] <= high:
            dropped += 1
            continue
        kept.append(element)
    if not dropped:
        raise BuildError(
            f"mark presentation asks to drop the {low:.0f}-{high:.0f} band "
            "but no path lies within it; the artwork changed shape"
        )
    return "".join(kept)


def polar(degrees_clockwise_from_twelve, radius):
    radians = math.radians(degrees_clockwise_from_twelve)
    return radius * math.sin(radians), -radius * math.cos(radians)


def mark_geometry(markup):
    """Placement, clip shape and front-of-ring arc for the fetched crest."""
    name, view_box, body = read_mark(markup)
    presentation = MARK_PRESENTATION.get(name, DEFAULT_PRESENTATION)
    body = drop_band(body, presentation["drop"])

    _, _, width, height = view_box
    scale = presentation["box"] / max(width, height)
    focus_x, focus_y = presentation.get("focus") or (width / 2, height / 2)
    tokens = {
        "MARK_TRANSFORM": (
            f"translate({fmt1(-focus_x * scale)},{fmt1(-focus_y * scale)}) "
            f"scale({trim(scale, 5)})"
        )
    }

    breaks = presentation["breaks"]
    circle = f'<circle r="{trim(MEDALLION_RADIUS, 1)}"/>'
    if breaks is None:
        tokens["MARK_CLIP"] = circle
        return body, tokens, False

    # The sector is the only place artwork may leave the medallion. Everything
    # else is held at the ring however far the crest actually extends.
    start, end = breaks
    corners = [(0.0, 0.0)] + [
        polar(start + (end - start) * step / 16, MARK_CLIP_REACH) for step in range(17)
    ]
    tokens["MARK_CLIP"] = circle + '<polygon points="%s"/>' % " ".join(
        f"{fmt1(x)},{fmt1(y)}" for x, y in corners
    )

    # The ring is drawn once under the crest, then this arc redraws the part the
    # crest may not cross. That is what makes the parts that do cross read as
    # breaking out, rather than sitting behind the ring.
    x0, y0 = polar(end, MEDALLION_RADIUS)
    x1, y1 = polar(start, MEDALLION_RADIUS)
    span = (start - end) % 360
    tokens["RING_FRONT_D"] = (
        f"M {fmt1(x0)} {fmt1(y0)} A {trim(MEDALLION_RADIUS, 1)} {trim(MEDALLION_RADIUS, 1)} "
        f"0 {1 if span > 180 else 0} 1 {fmt1(x1)} {fmt1(y1)}"
    )
    tokens["RING_FRONT_LEN"] = fmt1(2 * math.pi * MEDALLION_RADIUS * span / 360)
    return body, tokens, True


def optional_block(template, name, keep):
    """Keep or drop one marked region, removing its markers either way."""
    region = r"[ \t]*<!--%s_START-->\n(.*?)[ \t]*<!--%s_END-->\n" % (name, name)
    match = re.search(region, template, re.DOTALL)
    if not match:
        raise BuildError(f"template has no <!--{name}_START/END--> block")
    return re.sub(region, match.group(1) if keep else "", template, flags=re.DOTALL)


def prepare_template(template, has_playoffs, ring_front):
    """Resolve every optional region before any token is substituted.

    These blocks live in the template so the design stays in one file; only the
    decision to include them lives here.
    """
    template = optional_block(template, "PLAYOFF_CARDS", has_playoffs)
    return optional_block(template, "RING_FRONT", ring_front)


def splice_mark(svg, body):
    """Drop the fetched crest in after token substitution.

    Deliberately last: the crest is markup from another system, and doing this
    after rendering means nothing inside it can ever be mistaken for a token.
    """
    marker = "<!--CLUB_MARK-->"
    if svg.count(marker) != 1:
        raise BuildError(f"template needs exactly one {marker}, found {svg.count(marker)}")
    return svg.replace(marker, body)


def count_cards(template):
    numbers = sorted(int(n) for n in re.findall(r'class="card c(\d+)"', template))
    if numbers != list(range(1, len(numbers) + 1)):
        raise BuildError(f"card classes are not a contiguous run from c1: found {numbers}")
    return len(numbers)


TEAM_LINE_SIZE = 9.0        # the .team face at its design size
TEAM_LINE_TRACKING = 2.0    # letter-spacing the class applies
TEAM_LINE_HALF_WIDTH = 82.0  # centre at x=534, between the divider at 446 and the canvas edge


def team_line_size(name):
    """Font size that keeps the club name inside its column.

    The club is read from the index now rather than typed by hand, so the name
    changes on its own the day she is traded. At the design size the longest
    names in the league ("Denver Glacier Guardians") run past the canvas edge and
    would be clipped by the viewBox, so the line gives up a little size instead.
    """
    width = text_width(name, TEAM_LINE_SIZE, TEAM_LINE_TRACKING)
    if width <= 2 * TEAM_LINE_HALF_WIDTH:
        return TEAM_LINE_SIZE
    return round1(TEAM_LINE_SIZE * (2 * TEAM_LINE_HALF_WIDTH) / width)


def build_tokens(data, card_count):
    player = data["player"]
    team = data["team"]
    stats = data["stats"]

    tokens = {
        "ARIA_LABEL": (
            f"{player['name']}, number {player['jerseyNumber']}, "
            f"{player['position']}, {team['name']}, {player['currentLeague']}"
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
        "TEAM_SIZE": trim(team_line_size(team["name"].upper()), 1),
    }
    tokens.update(bar_geometry(player["totalTPE"], player["appliedTPE"]))
    tokens.update(cycle_timings(card_count))
    tokens.update(counting_tokens(stats["regular"], "ST"))
    tokens.update(advanced_tokens(stats["regular"], "ST"))
    if "playoffs" in stats:
        tokens.update(counting_tokens(stats["playoffs"], "PO"))

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


def check_logo(svg, body):
    """The crest must arrive in the output substantially intact.

    Measured against the mark actually fetched rather than a fixed byte count,
    so the check keeps working when she changes club and the new crest is a
    different size entirely.
    """
    want = sum(len(d) for d in re.findall(r'<path[^>]*\sd="([^"]+)"', body))
    got = sum(len(d) for d in re.findall(r'<path[^>]*\sd="([^"]+)"', svg))
    if not want:
        return ["the fetched club mark has no path data at all"]
    if got < want:
        return [
            f"output carries {got} bytes of path data against {want} in the fetched "
            "mark; the club mark was eaten"
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


def validate(svg, template, body):
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
        ("@media (prefers-reduced-motion: reduce)", "reduced motion block"),
        ("@keyframes fillTotal", "fillTotal keyframes"),
        ("@keyframes fillApp", "fillApp keyframes"),
        ("@keyframes cycle", "ticker cycle keyframes"),
        ("@keyframes bloom", "radar bloom keyframes"),
    ):
        if required not in svg:
            errors.append(f"{label} did not survive the build ({required!r} missing)")

    errors.extend(check_logo(svg, body))
    errors.extend(check_labels_fit(svg))

    size = len(svg.encode("utf-8"))
    if size < MIN_OUTPUT_BYTES:
        errors.append(f"output is {size} bytes, below the {MIN_OUTPUT_BYTES} floor")

    # The crest is no longer part of the template, so the template alone is no
    # longer the right yardstick. Template plus the mark spliced into it is.
    reference = len(template.encode("utf-8")) + len(body.encode("utf-8"))
    drift = abs(size - reference) / reference
    if drift > SIZE_TOLERANCE:
        errors.append(
            f"output is {size} bytes against an expected {reference} "
            f"(template plus mark), a {drift:.0%} change; over the "
            f"{SIZE_TOLERANCE:.0%} tolerance"
        )
    return errors


def build(template, data, mark_markup):
    """Render and validate. Raises BuildError rather than returning bad markup."""
    has_playoffs = "playoffs" in data["stats"]
    body, mark_tokens, ring_front = mark_geometry(mark_markup)
    prepared = prepare_template(template, has_playoffs, ring_front)

    card_count = count_cards(prepared)
    expected = REGULAR_CARDS + (PLAYOFF_CARDS if has_playoffs else 0)
    if card_count != expected:
        raise BuildError(
            f"expected {expected} cards with playoffs "
            f"{'present' if has_playoffs else 'absent'}, template has {card_count}"
        )

    tokens = build_tokens(data, card_count)
    tokens.update(mark_tokens)
    svg = splice_mark(render(prepared, tokens), body)
    errors = validate(svg, prepared, body)
    if errors:
        raise BuildError("refusing to write leyla.svg:\n  - " + "\n  - ".join(errors))
    return svg


def main():
    try:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        mark = LOGO_PATH.read_text(encoding="utf-8")
        svg = build(template, data, mark)
    except (OSError, json.JSONDecodeError, KeyError, BuildError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    OUTPUT_PATH.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote leyla.svg, {len(svg.encode('utf-8'))} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
