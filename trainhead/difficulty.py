"""Trail classification and the difficulty rules every route has to pass."""
from __future__ import annotations

SAC_LEVELS = {
    "hiking": 1,
    "mountain_hiking": 2,
    "demanding_mountain_hiking": 3,
    "alpine_hiking": 4,
    "demanding_alpine_hiking": 5,
    "difficult_alpine_hiking": 6,
}

# Rough correspondence to the DAV trail colours, for display only.
DAV_COLOUR = {1: "valley path", 2: "blue", 3: "red", 4: "black", 5: "black", 6: "black"}

# highway=* values we route on, grouped into classes.
HIGHWAY_CLASS = {
    "path": "path",
    "footway": "path",
    "bridleway": "path",
    "steps": "steps",
    "track": "track",
    "pedestrian": "minor_road",
    "living_street": "minor_road",
    "residential": "minor_road",
    "service": "minor_road",
    "cycleway": "minor_road",  # only kept when foot access is allowed
    "unclassified": "road",
    "tertiary": "busy_road",
    "tertiary_link": "busy_road",
    "secondary": "busy_road",
    "secondary_link": "busy_road",
}

CLASS_CODES = {"path": 0, "steps": 1, "track": 2, "minor_road": 3, "road": 4, "busy_road": 5}
CODE_CLASSES = {v: k for k, v in CLASS_CODES.items()}

# Multipliers on length when searching for routes: prefer marked trails,
# tolerate tracks, avoid roads.
COST_FACTOR = {
    ("path", True): 1.0,
    ("path", False): 1.2,
    ("steps", True): 1.0,
    ("steps", False): 1.3,
    ("track", True): 1.05,
    ("track", False): 1.2,
    ("minor_road", True): 1.4,
    ("minor_road", False): 1.5,
    ("road", True): 1.7,
    ("road", False): 1.8,
    ("busy_road", True): 4.0,
    ("busy_road", False): 4.0,
}

PAVED = {"asphalt", "paved", "concrete", "concrete:plates", "paving_stones", "sett", "cobblestone"}
BAD_VISIBILITY = {"bad", "horrible", "no"}
NO_FOOT = {"no", "private", "discouraged"}
FOOT_OK = {"yes", "designated", "permissive"}


def parse_sac(value: str | None) -> int | None:
    """sac_scale tag to 1..6. Unknown or empty values return None."""
    if not value:
        return None
    best = None
    for part in value.replace(",", ";").split(";"):
        part = part.strip().lower()
        level = SAC_LEVELS.get(part)
        if level is None and len(part) == 2 and part[0] == "t" and part[1].isdigit():
            level = int(part[1])
            if not 1 <= level <= 6:
                level = None
        if level is not None:
            best = level if best is None else max(best, level)
    return best


def sac_label(level: int | None) -> str:
    return f"T{level}" if level else "untagged"


def way_is_walkable(tags) -> bool:
    """Access and quality filters applied when reading OSM ways."""
    highway = tags.get("highway")
    if highway not in HIGHWAY_CLASS:
        return False
    foot = tags.get("foot")
    if foot in NO_FOOT:
        return False
    access = tags.get("access")
    if access in ("no", "private") and foot not in FOOT_OK:
        return False
    if highway == "cycleway" and foot not in FOOT_OK:
        return False
    if tags.get("trail_visibility") in BAD_VISIBILITY:
        return False
    if tags.get("informal") == "yes":
        return False
    if tags.get("area") == "yes":
        return False
    return True


def way_class(tags) -> str:
    return HIGHWAY_CLASS[tags.get("highway")]


def is_unpaved(highway_class: str, surface: str | None) -> bool:
    if surface:
        return surface not in PAVED
    return highway_class in ("path", "track", "steps")


def edge_allowed(cls_code: int, sac: int, max_ele_m: float, max_sac: int, untagged_max_ele_m: float) -> bool:
    """The rule from the plan: only segments tagged at or below the limit.

    sac == 0 means untagged. Untagged paths and steps are only used low down,
    where they are almost always easy. Tracks and roads carry no difficulty.
    """
    if sac:
        return sac <= max_sac
    cls = CODE_CLASSES[cls_code]
    if cls in ("path", "steps"):
        return max_ele_m <= untagged_max_ele_m
    return True
