"""News incidents from GDELT 2.0 event exports.

GDELT publishes a zipped tab-separated export of coded news events every 15 minutes and names the
newest one in ``lastupdate.txt``. This module keeps a rolling window of the last ``WINDOW`` exports
(3 hours), parses only conflict-coded events, and folds rows that share a place and a CAMEO code
into one item with a ``count``, so a city with forty "fight" reports is one marker, not forty.

GDELT's own coding is not equally reliable, and this module treats the three things it conflates as
three separate questions:

* **What happened.** Every row carries a full CAMEO code, its base code and its root code. The root
  ("FIGHT") is the coarsest of the three, so an item is labelled with the base code its rows agree
  on — "Arrest or detention", "Fight with small arms" — and falls back to the root only when the
  rows disagree or were coded to the root alone.
* **Where.** ``ActionGeo`` is meant to be where the action happened, but GDELT copies an actor's own
  location there when it cannot resolve one (over 70% of conflict rows), and a type-1 row is a
  country's centroid rather than a place. Both are kept, and both are marked as approximate.
* **Whether it is a story at all.** ``IsRootEvent`` says whether an article is about the action or
  merely mentions it, and a row coded only to its root by a single source is GDELT guessing. Each
  item gets a ``confidence`` from those signals; the weakest are withheld from the map rather than
  drawn as though they were confirmed.

Each export is downloaded once: a refresh only fetches stamps that entered the window, and stamps
that left it are dropped, so memory stays bounded by the window. GDELT's doc/search API is rate
limited and not used.
"""

from __future__ import annotations

import io
import math
import re
import threading
import zipfile
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import geo
from .http import UpstreamError
from .parsers import clamp01, item, number, text

LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
EXPORT_URL = "https://data.gdeltproject.org/gdeltv2/{stamp}.export.CSV.zip"
WINDOW = 12  # 15-minute exports → 3 hours
MAX_ITEMS = 5000
MAX_KEYS_PER_FILE = 5000
MAX_UNZIPPED = 64 * 1024 * 1024
COLUMNS = 61

# CAMEO root code → label. 13 and 14 are verbal conflict (QuadClass 3), 15-20 material conflict
# (QuadClass 4). 16 ("reduce relations") is diplomatic but reads as tension — sanctions, expulsions,
# broken relations — so it is kept and labelled by its base code rather than shown as violence.
ROOTS = {
    "13": "Threaten",
    "14": "Protest",
    "15": "Force posture",
    "16": "Reduce relations",
    "17": "Coerce",
    "18": "Assault",
    "19": "Fight",
    "20": "Mass violence",
}

# Root code → the kind slug the page and the contract use for an incident item. An item's title is
# labelled by the specific base code its rows agree on; the kind stays the root, which is what a
# reader filters on and what the badge shows.
ROOT_KINDS = {
    "13": "threaten", "14": "protest", "15": "force", "16": "reduce-relations",
    "17": "coerce", "18": "assault", "19": "fight", "20": "mass-violence",
}

# CAMEO base code → label, from GDELT's own CAMEO.eventcodes lookup. GDELT records the full code,
# the base and the root for every event; the base is the one a reader wants, and the root is always
# base[:2]. A base code ending in "0" is the root's own generic code and adds nothing.
BASES = {
    "130": "Threaten", "131": "Threaten to cut aid or trade", "132": "Threaten administrative sanctions",
    "133": "Threaten dissent or protest", "134": "Threaten to halt negotiations", "135": "Threaten to halt mediation",
    "136": "Threaten to halt international involvement", "137": "Threaten violent repression",
    "138": "Threaten to use military force", "139": "Give an ultimatum",
    "140": "Political dissent", "141": "Demonstrate or rally", "142": "Hunger strike", "143": "Strike or boycott",
    "144": "Block passage", "145": "Violent protest or riot",
    "150": "Demonstrate military or police power", "151": "Increase police alert status",
    "152": "Increase military alert status", "153": "Mobilise police", "154": "Mobilise armed forces",
    "160": "Reduce relations", "161": "Break diplomatic relations", "162": "Reduce or cut aid",
    "163": "Impose embargo, boycott or sanctions", "164": "Halt negotiations", "165": "Halt mediation",
    "166": "Expel or withdraw",
    "170": "Coerce", "171": "Seize or damage property", "172": "Impose administrative sanctions",
    "173": "Arrest or detention", "174": "Expel or deport", "175": "Repress violently",
    "180": "Use unconventional violence", "181": "Abduct, hijack or take hostage", "182": "Physically assault",
    "183": "Bombing (non-military)", "184": "Use as a human shield", "185": "Attempt to assassinate",
    "186": "Assassinate",
    "190": "Use conventional military force", "191": "Impose blockade or restrict movement",
    "192": "Occupy territory", "193": "Fight with small arms", "194": "Fight with artillery and tanks",
    "195": "Employ aerial weapons", "196": "Violate a ceasefire",
    "200": "Unconventional mass violence", "201": "Mass expulsion", "202": "Mass killings",
    "203": "Ethnic cleansing", "204": "Use weapons of mass destruction",
}

# CAMEO actor type code → label (GDELT's CAMEO.type lookup). Used when an actor's name is a
# category placeholder rather than a real actor.
TYPES = {
    "COP": "police", "GOV": "government", "INS": "insurgents", "JUD": "the judiciary", "MIL": "military forces",
    "OPP": "political opposition", "REB": "rebels", "SEP": "separatist rebels", "SPY": "an intelligence service",
    "UAF": "unaligned armed forces", "AGR": "farmers", "BUS": "a business", "CRM": "criminals", "CVL": "civilians",
    "DEV": "a development agency", "EDU": "an education body", "ELI": "political elites", "ENV": "environmentalists",
    "HLH": "a health organisation", "HRI": "human rights activists", "LAB": "a labor group", "LEG": "a legislature",
    "MED": "the media", "REF": "refugees", "MOD": "moderates", "RAD": "radicals", "IGO": "an intergovernmental body",
    "IMG": "an international militarized group", "INT": "an international body", "MNC": "a multinational company",
    "NGM": "a popular movement", "NGO": "an NGO", "PKO": "peacekeepers", "SET": "settlers",
    "UIS": "an unidentified state actor", "UNO": "the United Nations", "IRC": "the Red Cross",
    "AMN": "Amnesty International", "GRP": "Greenpeace",
}

# Actor names GDELT uses as category placeholders instead of names of real actors. Shown as written
# they read as institutions ("Prison", "Constituent", "Africa", "Village"), so the actor's CAMEO type
# is used instead and the placeholder is dropped. Only generic words go here: a demonym like "Greek"
# or a real place-actor like "Cape Town" is a name and is shown as written.
VAGUE_ACTORS = frozenset("""citizen citizens constituent constituents resident residents community communities
business businesses person persons individual individuals people public civilian civilians prison inmate inmates
official officials officialdom authority authorities state nation nationwide village villages hamlet hamlets
settlement settlements minist minister ministers africa asia europe americas middle east caribbean
balkans caucasus latin america south asia southeast asia west africa east africa north africa central asia the west
mediterranean persian gulf scandinavia""".split())

_STAMP = re.compile(r"(\d{14})\.export\.CSV\.zip\s*$")

# Column positions in the GDELT 2.0 event table (61 tab-separated fields).
C_SQLDATE = 1
C_ACTOR1_CODE, C_ACTOR1_NAME = 5, 6
C_ACTOR2_CODE, C_ACTOR2_NAME = 15, 16
C_IS_ROOT, C_EVENT_CODE, C_BASE_CODE, C_ROOT, C_QUAD = 25, 26, 27, 28, 29
C_GOLDSTEIN, C_MENTIONS, C_SOURCES, C_ARTICLES, C_TONE = 30, 31, 32, 33, 34
# Actor1Geo / Actor2Geo / ActionGeo each occupy a type, name, country code and FeatureID.
C_A1_TYPE, C_A1_FIPS, C_A1_FID = 35, 37, 42
C_A2_TYPE, C_A2_FIPS, C_A2_FID = 43, 45, 50
C_ACTION_TYPE, C_ACTION_NAME, C_ACTION_FIPS, C_ACTION_FID = 51, 52, 53, 58
C_LAT, C_LON = 56, 57
C_DATEADDED, C_URL = 59, 60

# How far each ActionGeo type can be trusted to be where the action happened: 4 is a resolved place,
# 3 a city, 2 a state or region, 5 a first-level administrative area, 1 a country centroid, 0 nothing.
GEO_WEIGHT = {"4": 1.0, "3": 0.85, "2": 0.7, "5": 0.6, "1": 0.35, "0": 0.2}
# An ActionGeo point copied from an actor's location is a guess about where, not a report of it.
INFERRED_WEIGHT = 0.55
# Events whose own reported date is older than this are background, not something happening now.
MAX_EVENT_AGE_DAYS = 7
STALE_PENALTY = 0.25
# Items scoring below this are withheld: single-source, root-coded mentions of a country centroid.
MIN_CONFIDENCE = 0.30



def parse_lastupdate(body: str) -> str | None:
    """The 14-digit stamp of the newest export named in ``lastupdate.txt``."""
    for line in body.splitlines():
        match = _STAMP.search(line.strip())
        if match:
            return match.group(1)
    return None


def window_stamps(latest: str, size: int = WINDOW) -> list[str]:
    """``latest`` and the ``size - 1`` export stamps before it, newest first."""
    start = datetime.strptime(latest, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return [(start - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S") for i in range(size)]


def stamp_ms(stamp: str) -> int | None:
    try:
        return int(datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def unzip_export(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise UpstreamError(502, "A GDELT export had no CSV inside.")
            info = archive.getinfo(names[0])
            if info.file_size > MAX_UNZIPPED:
                raise UpstreamError(502, "A GDELT export was larger than this plugin accepts.")
            return archive.read(info).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise UpstreamError(502, "A GDELT export was not a valid zip file.") from exc


def _group_key(fields: list[str], lat: float, lon: float) -> str:
    """Where a row's action happened, as stably as the row allows.

    A resolved place (type 3 or 4) is identified by GDELT's own FeatureID, so the same city reported
    once at city precision and once at street precision folds into one marker instead of two. Coarser
    types fall back to rounded coordinates, which is the best identity available for a state or a
    country centroid."""
    fid = fields[C_ACTION_FID].strip()
    if fid and fields[C_ACTION_TYPE].strip() in ("3", "4"):
        return f"geo:{fields[C_ACTION_FIPS].strip()}:{fid}"
    return f"coord:{fields[C_ACTION_FIPS].strip()}:{lat:.2f},{lon:.2f}"


def _location_inferred(fields: list[str]) -> bool:
    """True when GDELT copied an actor's location into ActionGeo instead of resolving the action's."""
    fid = fields[C_ACTION_FID].strip()
    return bool(fid) and fid in (fields[C_A1_FID].strip(), fields[C_A2_FID].strip())


def _actor_label(fields: list[str], code_column: int, name_column: int) -> str | None:
    """A readable actor name: "Russia", "Hamas", "police".

    GDELT's actor fields are a CAMEO code plus a free-text name, and the name alone is unreliable:
    the same code appears as "POLICE" in one row and "PRISON" in the next, and category placeholders
    like "CONSTITUENT" or "AFRICA" read as though they named organisations. Where the name is one of
    those, the actor's CAMEO type is shown instead."""
    name = text(fields[name_column], 40)
    if not name:
        return None
    if name.lower() in VAGUE_ACTORS:
        kind = TYPES.get(fields[code_column].strip().upper())
        return kind.capitalize() if kind else None
    return name.title()


def _event_age_days(fields: list[str], now: datetime | None) -> int | None:
    """How old the reported action is, by GDELT's own SQLDATE, relative to this export's stamp."""
    if now is None:
        return None
    try:
        reported = datetime.strptime(fields[C_SQLDATE].strip(), "%Y%m%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (now - reported).days


def parse_export(body: str, now: datetime | None = None) -> dict[str, dict]:
    """One export's rows → per (place, root) accumulators. Rows of other quad classes, and rows
    without an action location, are skipped. ``now`` is the export's own stamp, used to tell a
    report about something happening now from one about something that happened years ago."""
    groups: dict[str, dict] = {}
    for line in body.splitlines():
        fields = line.split("\t")
        if len(fields) < COLUMNS:
            continue
        root = fields[C_ROOT].strip().zfill(2)
        if root not in ROOTS or fields[C_QUAD].strip() not in ("3", "4"):
            continue
        lat, lon = number(fields[C_LAT], -90, 90), number(fields[C_LON], -180, 180)
        if lat is None or lon is None or (lat == 0 and lon == 0):
            continue
        key = f"{root}:{_group_key(fields, lat, lon)}"
        mentions = int(number(fields[C_MENTIONS], 0, 1e7) or 0)
        sources = int(number(fields[C_SOURCES], 0, 1e7) or 0)
        base = fields[C_BASE_CODE].strip().zfill(3)
        geo_type = fields[C_ACTION_TYPE].strip()
        group = groups.get(key)
        if group is None:
            if len(groups) >= MAX_KEYS_PER_FILE:
                continue
            group = groups[key] = {
                "root": root, "lat": lat, "lon": lon, "place": text(fields[C_ACTION_NAME], 120),
                "fips": fields[C_ACTION_FIPS].strip(), "geo_type": geo_type,
                "events": 0, "mentions": 0, "sources": 0, "articles": 0, "goldstein_sum": 0.0,
                "goldstein_min": 10.0, "tone_sum": 0.0, "added": None, "url": None, "url_mentions": -1,
                "actors": [], "codes": Counter(), "root_events": 0, "specific_events": 0,
                "urls": set(), "max_sources": 0, "inferred": False, "stale": False,
            }
        goldstein = number(fields[C_GOLDSTEIN], -10, 10) or 0.0
        group["events"] += 1
        group["mentions"] += mentions
        group["sources"] += sources
        group["articles"] += int(number(fields[C_ARTICLES], 0, 1e7) or 0)
        group["goldstein_sum"] += goldstein
        group["goldstein_min"] = min(group["goldstein_min"], goldstein)
        group["tone_sum"] += number(fields[C_TONE], -100, 100) or 0.0
        group["codes"][base] += 1
        group["root_events"] += fields[C_IS_ROOT].strip() == "1"
        group["specific_events"] += base != root + "0" and base in BASES
        group["max_sources"] = max(group["max_sources"], sources)
        group["geo_type"] = max((group["geo_type"], geo_type), key=lambda t: GEO_WEIGHT.get(t, 0.0))
        group["inferred"] = group["inferred"] or _location_inferred(fields)
        age = _event_age_days(fields, now)
        group["stale"] = group["stale"] or (age is not None and age > MAX_EVENT_AGE_DAYS)
        url = fields[C_URL].strip()
        if url.startswith(("http://", "https://")):
            group["urls"].add(url[:500])
            if mentions > group["url_mentions"]:
                group["url"], group["url_mentions"] = url, mentions
        added = stamp_ms(fields[C_DATEADDED].strip())
        if added and (group["added"] is None or added > group["added"]):
            group["added"] = added
        for code_column, name_column in ((C_ACTOR1_CODE, C_ACTOR1_NAME), (C_ACTOR2_CODE, C_ACTOR2_NAME)):
            actor = _actor_label(fields, code_column, name_column)
            if actor and actor.lower() not in {a.lower() for a in group["actors"]} and len(group["actors"]) < 4:
                group["actors"].append(actor)
    return groups


def merge(files: list[dict[str, dict]]) -> dict[str, dict]:
    """Sum per-file accumulators into one per (place, root) over the whole window."""
    total: dict[str, dict] = {}
    for groups in files:
        for key, group in groups.items():
            have = total.get(key)
            if have is None:
                total[key] = {**group, "actors": list(group["actors"]), "codes": Counter(group["codes"]),
                              "urls": set(group["urls"])}
                continue
            for field in ("events", "mentions", "sources", "articles", "goldstein_sum", "tone_sum",
                          "root_events", "specific_events"):
                have[field] += group[field]
            have["goldstein_min"] = min(have["goldstein_min"], group["goldstein_min"])
            have["max_sources"] = max(have["max_sources"], group["max_sources"])
            have["codes"].update(group["codes"])
            have["urls"].update(group["urls"])
            have["geo_type"] = max((have["geo_type"], group["geo_type"]), key=lambda t: GEO_WEIGHT.get(t, 0.0))
            have["inferred"] = have["inferred"] or group["inferred"]
            have["stale"] = have["stale"] or group["stale"]
            if group["added"] and (have["added"] is None or group["added"] > have["added"]):
                have["added"] = group["added"]
            if group["url"] and group["url_mentions"] > have["url_mentions"]:
                have["url"], have["url_mentions"] = group["url"], group["url_mentions"]
            for actor in group["actors"]:
                if actor.lower() not in {a.lower() for a in have["actors"]} and len(have["actors"]) < 4:
                    have["actors"].append(actor)
    return total


def short_place(full: str | None) -> str:
    """"Kharkiv, Kharkivs'ka Oblast', Ukraine" → "Kharkiv, Ukraine"."""
    if not full:
        return "Unknown place"
    # GDELT pads a country-level place with "(general)" and sometimes leaves an empty component.
    parts = [re.sub(r"\s*\(general\)\s*$", "", p).strip() for p in full.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return "Unknown place"
    if len(parts) >= 3:
        return f"{parts[0]}, {parts[-1]}"
    return ", ".join(dict.fromkeys(parts))


def group_label(group: dict) -> tuple[str, str]:
    """The label an item gets, and a line saying which CAMEO code it is showing.

    A group whose specific-coded rows all agree is labelled with that base code. A group whose rows
    disagree about what kind of <root> it was, or that was coded to the root alone, is labelled with
    the root instead: "Fight" is true of all of them, "Fight with small arms" would overclaim."""
    root = group["root"]
    specific = sorted(((count, code) for code, count in group["codes"].items()
                       if code[:2] == root and code != root + "0" and code in BASES),
                      key=lambda t: (-t[0], t[1]))
    if not specific:
        return ROOTS[root], f"{root}0 — coded only to the root ({ROOTS[root]})"
    _, code = specific[0]
    if len(specific) > 1:
        kinds = ", ".join(f"{c} {BASES[c]}" for _, c in specific[:3])
        return ROOTS[root], f"{len(specific)} codes ({kinds})"
    return BASES[code], f"{code} — {BASES[code]}, under {root} {ROOTS[root]}"


def group_confidence(group: dict) -> float:
    """How far this item can be trusted, from GDELT's own reliability signals (0..1).

    All four signals are measured on the group rather than on any one row, so an event corroborated
    by several articles scores higher than any single one of them: whether the articles are *about*
    the action (``IsRootEvent``), whether GDELT coded it below its root, how many distinct sources
    reported it, and how precisely its location was resolved. An event whose own reported date is old
    is penalised: it is background, not something happening now."""
    n = group["events"] or 1
    about = group["root_events"] / n
    specific = group["specific_events"] / n
    corroboration = min(1.0, math.log10(1 + max(len(group["urls"]), group["max_sources"])) / 1.5)
    location = GEO_WEIGHT.get(group["geo_type"], 0.0) * (INFERRED_WEIGHT if group["inferred"] else 1.0)
    score = 0.05 + 0.30 * about + 0.20 * specific + 0.20 * corroboration + 0.25 * location
    if group["stale"]:
        score -= STALE_PENALTY
    return clamp01(score)


def confidence_band(confidence: float) -> str:
    return "High" if confidence >= 0.7 else ("Medium" if confidence >= 0.45 else "Low")


def location_note(group: dict) -> str | None:
    """Why this marker's position may not be where the action happened, or None when it is sound."""
    if group["geo_type"] == "1":
        return "Approximate: GDELT placed this at the centre of the country, not at the event"
    if group["inferred"]:
        return "Inferred: GDELT could not locate the action and used an actor's location instead"
    return None


def incident_severity(mentions: int, goldstein_min: float, confidence: float = 1.0) -> float:
    """More coverage and a more negative Goldstein score (−10 is the most destabilising) → brighter,
    scaled down for items GDELT's own signals say are weak. Coverage saturates at about 100 mentions."""
    coverage = min(1.0, math.log10(1 + max(0, mentions)) / 2.0)
    impact = max(0.0, min(1.0, -goldstein_min / 10))
    weight = 0.45 + 0.55 * clamp01(confidence)
    return clamp01(0.1 + 0.9 * (0.55 * coverage + 0.45 * impact) * weight)


def to_items(groups: dict[str, dict], limit: int = MAX_ITEMS) -> tuple[list[dict], int]:
    """Items worth drawing, and how many groups were withheld as too weak to show."""
    out = []
    withheld = 0
    for key, g in groups.items():
        confidence = group_confidence(g)
        if confidence < MIN_CONFIDENCE:
            withheld += 1
            continue
        label, coded_as = group_label(g)
        country = geo.from_fips(g["fips"]) or geo.from_place(g["place"])
        out.append(item(
            "incidents", key, ROOT_KINDS[g["root"]], g["lat"], g["lon"], f"{label} · {short_place(g['place'])}",
            subtitle=f"{g['events']} report{'s' if g['events'] != 1 else ''} · {g['mentions']} mentions",
            severity=incident_severity(g["mentions"], g["goldstein_min"], confidence),
            time=g["added"],
            country=country,
            url=g["url"],
            rows=[("Type", label),
                  ("CAMEO code", coded_as),
                  ("Confidence", f"{confidence_band(confidence)} · {confidence:.0%}"),
                  ("Location", location_note(g)),
                  ("Reports", g["events"]), ("Mentions", g["mentions"]), ("Sources", g["sources"]),
                  ("Actors", ", ".join(g["actors"]) or None)],
            count=g["events"],
            confidence=confidence,
        ))
    out.sort(key=lambda i: (i["severity"], i["time"] or 0), reverse=True)
    return out[:limit], withheld


Fetch = Callable[[str], bytes]


class Window:
    """The rolling set of parsed exports. ``refresh`` is called through the layer's snapshot cache
    (at most every 15 minutes) and returns the merged items."""

    def __init__(self, fetch_text: Callable[[str], str], fetch_bytes: Fetch, size: int = WINDOW,
                 workers: int = 4) -> None:
        self._fetch_text, self._fetch_bytes = fetch_text, fetch_bytes
        self.size, self._workers = size, workers
        self._files: OrderedDict[str, dict[str, dict]] = OrderedDict()
        self._lock = threading.Lock()
        self.latest: str | None = None

    def _load(self, stamp: str) -> dict[str, dict] | None:
        try:
            body = unzip_export(self._fetch_bytes(EXPORT_URL.format(stamp=stamp)))
        except UpstreamError as exc:
            if exc.status == 404:
                return {}  # GDELT skips a slot now and then; remember it as empty
            return None  # try again on the next refresh
        # Each export is judged against its own stamp, so an old event date is old relative to the
        # file that reported it, not relative to whenever this window happens to be read.
        return parse_export(body, now=datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc))

    def refresh(self) -> tuple[list[dict], int]:
        """Items to draw, and the number of groups withheld as too weak to show."""
        latest = parse_lastupdate(self._fetch_text(LASTUPDATE_URL))
        if latest is None:
            raise UpstreamError(502, "GDELT's lastupdate.txt did not name an export file.")
        wanted = window_stamps(latest, self.size)
        with self._lock:
            for stamp in [s for s in self._files if s not in wanted]:
                del self._files[stamp]
            missing = [s for s in wanted if s not in self._files]
        if missing:
            with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="osint-gdelt") as pool:
                loaded = list(pool.map(self._load, missing))
            with self._lock:
                for stamp, groups in zip(missing, loaded):
                    if groups is not None:
                        self._files[stamp] = groups
        with self._lock:
            self.latest = latest
            if not any(self._files.values()) and latest not in self._files:
                raise UpstreamError(503, "Could not download any GDELT export in the last 3 hours.")
            files = [self._files[s] for s in wanted if s in self._files]
        return to_items(merge(files))

    def loaded(self) -> int:
        with self._lock:
            return len(self._files)
