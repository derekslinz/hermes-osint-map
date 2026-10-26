"""GDELT export parsing, aggregation, the rolling 3-hour window, and how far each item can be trusted."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

import pytest

from osintmap import gdelt
from osintmap.http import UpstreamError

LASTUPDATE = (
    "47277 7e603202643d72bbe0b9731640d22166 http://data.gdeltproject.org/gdeltv2/20261023044500.export.CSV.zip\n"
    "51551 8649ec20aa1cb2743854b5e9f74affdc http://data.gdeltproject.org/gdeltv2/20261023044500.mentions.CSV.zip\n"
    "2489657 86cea0d0f3d7ca84f275c12994f62dcd http://data.gdeltproject.org/gdeltv2/20261023044500.gkg.csv.zip\n"
)


def zipped(body: str, name: str = "x.export.CSV") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, body)
    return buffer.getvalue()


def row(root="19", quad="4", lat="50.0", lon="36.25", place="Kharkiv, Kharkivs'ka Oblast', Ukraine", fips="UP",
        mentions="10", goldstein="-10.0", added="20261023044500", url="https://news.example.com/a", actor="RUSSIA",
        base=None, is_root="1", sqldate="20261023", geo_type="4", sources="3", fid="7010881", a1_fid="",
        a2_fid="", actor2="UKRAINE", actor2_name=""):
    """One GDELT 2.0 event row.

    The defaults describe a well-coded event: GDELT says the article is about the action (is_root=1),
    resolved it to a city (geo_type=4) and did not have to borrow an actor's location (a1_fid/a2_fid
    empty). Pass the opposite to describe a row GDELT is guessing at."""
    fields = [""] * 61
    fields[1] = sqldate
    fields[5], fields[6] = actor, actor
    fields[15], fields[16] = actor2, actor2_name or actor2
    fields[25] = is_root
    fields[26], fields[27], fields[28], fields[29] = (base or root + "0"), (base or root + "0"), root, quad
    fields[30], fields[31], fields[32], fields[33], fields[34] = goldstein, mentions, sources, mentions, "-5.5"
    fields[42], fields[50] = a1_fid, a2_fid  # Actor1Geo / Actor2Geo FeatureIDs
    fields[51], fields[52], fields[53] = geo_type, place, fips
    fields[56], fields[57], fields[58] = lat, lon, fid
    fields[59], fields[60] = added, url
    return "\t".join(fields)


def items_of(body: str, now: datetime | None = None):
    return gdelt.to_items(gdelt.parse_export(body, now))


def only_group(body: str, now: datetime | None = None) -> dict:
    """The single group a one-row export produces."""
    return next(iter(gdelt.parse_export(body, now).values()))


def stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def test_lastupdate_names_the_export():
    assert gdelt.parse_lastupdate(LASTUPDATE) == "20261023044500"
    assert gdelt.parse_lastupdate("garbage") is None


def test_window_is_twelve_quarter_hours_newest_first():
    stamps = gdelt.window_stamps("20261023004500")
    assert len(stamps) == 12 and stamps[0] == "20261023004500" and stamps[1] == "20261023003000"
    assert stamps[-1] == "20261022220000"  # crosses midnight


def test_recorded_export_keeps_only_conflict_roots(fixture):
    groups = gdelt.parse_export(fixture("gdelt_export.tsv"))
    roots = {g["root"] for g in groups.values()}
    assert roots == {"19"}  # the fixture's 04/01 (consult, make statement) rows are skipped
    canberra = next(g for g in groups.values() if g["place"].startswith("Canberra"))
    items, _ = gdelt.to_items(groups)
    titles = [i["title"] for i in items]
    canberra_titles = [t for t in titles if t.endswith("Canberra, Australia")]
    assert canberra_titles, "the fixture's Canberra event should still be drawn"
    assert next(i for i in items if i["title"] == canberra_titles[0])["country"] == "AU"  # FIPS AS is Australia, not American Samoa
    assert canberra["events"] == 1


def test_an_item_is_labelled_with_the_code_its_rows_agree_on_not_the_root():
    # GDELT coded this as 173 (arrest/detention) under root 17 ("Coerce"). "Coerce" is the coarsest
    # thing GDELT knows; the reader wants the code the article was actually about.
    items, _ = items_of(row(root="17", quad="4", base="173", place="Ikeja, Lagos, Nigeria", fips="NI"))
    assert items[0]["title"] == "Arrest or detention · Ikeja, Nigeria"
    assert dict(items[0]["props"])["CAMEO code"] == "173 — Arrest or detention, under 17 Coerce"

    # A row coded to its root alone gets the root label, and says that is what happened.
    items, _ = items_of(row(root="17", quad="4", base="170"))
    assert items[0]["title"].startswith("Coerce ·")
    assert dict(items[0]["props"])["CAMEO code"] == "170 — coded only to the root (Coerce)"

    # Rows that disagree about the specific code fall back to the root label, naming the codes they disagree on.
    items, _ = items_of("\n".join([row(root="19", base="193"), row(root="19", base="194"), row(root="19", base="194")]))
    assert items[0]["title"].startswith("Fight ·")
    assert dict(items[0]["props"])["CAMEO code"] == \
        "2 codes (194 Fight with artillery and tanks, 193 Fight with small arms)"


def test_diplomatic_tension_is_kept_and_named():
    # Root 16 was dropped before. Sanctions and broken relations are exactly what a tension map wants,
    # and naming the code keeps them from reading as violence.
    items, _ = items_of(row(root="16", quad="4", base="163", place="Tehran, Tehran, Iran", fips="IR"))
    assert items[0]["title"] == "Impose embargo, boycott or sanctions · Tehran, Iran"
    assert items[0]["kind"] == "reduce-relations"


def test_rows_at_the_same_place_and_root_fold_into_one_item():
    body = "\n".join([
        row(mentions="10", url="https://news.example.com/small"),
        row(mentions="80", url="https://news.example.com/big", goldstein="-9.0", added="20261023050000"),
        row(root="14", quad="3", goldstein="-6.5"),  # same place, different root → its own item
        row(root="04", quad="1"),  # cooperation: skipped
        row(root="19", quad="2"),  # mismatched quad class: skipped
        row(lat="", lon=""),  # no action location: skipped
        "short\tline",
    ])
    items, withheld = items_of(body)
    assert len(items) == 2 and withheld == 0
    fight = next(i for i in items if i["kind"] == "fight")
    assert fight["count"] == 2 and fight["title"] == "Fight · Kharkiv, Ukraine"
    assert fight["url"] == "https://news.example.com/big" and fight["country"] == "UA"
    assert fight["time"] == gdelt.stamp_ms("20261023050000")
    assert fight["id"] == "incidents:19:geo:UP:7010881"
    assert dict(fight["props"])["Mentions"] == "90"
    assert next(i for i in items if i["kind"] == "protest")["count"] == 1


def test_the_same_city_reported_at_two_precisions_is_one_marker():
    # GDELT resolves some reports to the city and some to a district of it. FeatureID is the city's own
    # id, so both fold into one marker instead of drawing the same event twice.
    body = "\n".join([
        row(place="Kharkiv, Kharkivs'ka Oblast', Ukraine", lat="49.9935", lon="36.2304", fid="7010881"),
        row(place="Kharkiv, Ukraine", lat="50.0010", lon="36.2290", fid="7010881"),
    ])
    items, _ = items_of(body)
    assert len(items) == 1 and items[0]["count"] == 2
    # A different place is a different event even at the same coordinates.
    items, _ = items_of("\n".join([row(fid="7010881"), row(fid="1234567")]))
    assert len(items) == 2


def test_merge_sums_across_files():
    a = gdelt.parse_export(row(mentions="5"))
    b = gdelt.parse_export(row(mentions="7", url="https://news.example.com/b"))
    merged = gdelt.merge([a, b])
    assert len(merged) == 1
    only = next(iter(merged.values()))
    assert only["events"] == 2 and only["mentions"] == 12 and only["url"] == "https://news.example.com/b"
    assert only["urls"] == {"https://news.example.com/a", "https://news.example.com/b"}
    assert only["codes"]["190"] == 2
    assert a[next(iter(a))]["events"] == 1  # inputs are not mutated


def test_confidence_follows_gdelts_own_reliability_signals():
    strong = gdelt.group_confidence(only_group(row(is_root="1", base="193", sources="9")))
    # The same event, but GDELT only knows it was mentioned in passing, coded to the root, by one source.
    weak = gdelt.group_confidence(only_group(row(is_root="0", base="190", sources="1")))
    assert strong > 0.7 > weak >= gdelt.MIN_CONFIDENCE
    # A country centroid is a far weaker position than a resolved city.
    assert gdelt.group_confidence(only_group(row(geo_type="1", place="Ukraine", fips="UP", fid=""))) < \
        gdelt.group_confidence(only_group(row()))
    # A location borrowed from an actor is weaker than one GDELT resolved for the action itself.
    assert gdelt.group_confidence(only_group(row(a1_fid="7010881"))) < gdelt.group_confidence(only_group(row()))
    # An event whose own reported date is years old is background, not news.
    stale = gdelt.group_confidence(only_group(row(is_root="0", base="190", sources="1", sqldate="20161023"),
                                             now=stamp("20261023044500")))
    assert stale < gdelt.group_confidence(only_group(row(is_root="0", base="190", sources="1")))
    assert stale < gdelt.MIN_CONFIDENCE


def test_weak_items_are_withheld_instead_of_drawn_as_confirmed():
    body = "\n".join([
        row(is_root="1", base="193", sources="6"),
        row(is_root="0", base="190", sources="1", mentions="1", geo_type="1", place="Ukraine", fips="UP", fid=""),
    ])
    items, withheld = items_of(body)
    assert len(items) == 1 and withheld == 1
    assert items[0]["confidence"] >= gdelt.MIN_CONFIDENCE
    assert dict(items[0]["props"])["Confidence"].startswith("High")
    assert gdelt.confidence_band(0.7) == "High" and gdelt.confidence_band(0.45) == "Medium"
    assert gdelt.confidence_band(0.44) == "Low"


def test_a_low_confidence_item_is_dimmed_not_drawn_at_full_strength():
    assert gdelt.incident_severity(100, -10, 1.0) == 1.0
    assert gdelt.incident_severity(100, -10, 0.3) < gdelt.incident_severity(100, -10, 1.0)
    assert gdelt.incident_severity(1, -1) < gdelt.incident_severity(100, -1) < gdelt.incident_severity(100, -10)
    assert gdelt.incident_severity(10_000, -10) == 1.0


def test_an_inferred_or_approximate_location_says_so():
    borrowed = only_group(row(a1_fid="7010881"))
    assert borrowed["inferred"] is True and gdelt.location_note(borrowed).startswith("Inferred:")
    centroid = only_group(row(geo_type="1", place="Ukraine", fips="UP", fid=""))
    assert gdelt.location_note(centroid).startswith("Approximate:")
    resolved = only_group(row())
    assert resolved["inferred"] is False and gdelt.location_note(resolved) is None


def test_actor_names_gdeelt_uses_as_categories_are_not_shown_as_institutions():
    # "CONSTITUENT", "PRISON" and "VILLAGE" are GDELT's placeholders for a kind of actor; the CAMEO
    # type code says what that kind is. "AFRICA" is a region, and "MINIST" is GDELT's own truncation
    # of "Minister". A demonym or a real place-actor is a name, and is shown as written.
    body = "\n".join([
        row(actor2="CVL", actor2_name="CONSTITUENT"),
        row(actor2="COP", actor2_name="PRISON"),
        row(actor2="CVL", actor2_name="VILLAGE"),
        row(actor2="GOV", actor2_name="MINIST"),
        row(actor2="AFR", actor2_name="AFRICA"),
    ])
    items, _ = items_of(body)
    actors = dict(items[0]["props"])["Actors"]
    assert "Civilians" in actors and "Police" in actors and "Government" in actors
    assert "Constituent" not in actors and "Prison" not in actors and "Africa" not in actors
    assert "Village" not in actors and "Minist" not in actors
    # A real actor's name is shown as written.
    items, _ = items_of(row(actor2="PSE", actor2_name="HAMAS"))
    assert "Hamas" in dict(items[0]["props"])["Actors"]
    items, _ = items_of(row(actor2="GRC", actor2_name="GREEK"))
    assert "Greek" in dict(items[0]["props"])["Actors"]


class FakeGdelt:
    def __init__(self, latest):
        self.latest, self.calls, self.missing, self.broken = latest, [], set(), set()

    def text(self, url):
        return f"1 x http://data.gdeltproject.org/gdeltv2/{self.latest}.export.CSV.zip\n"

    def bytes(self, url):
        stamp = url.rsplit("/", 1)[-1][:14]
        self.calls.append(stamp)
        if stamp in self.missing:
            raise UpstreamError(404, "gone")
        if stamp in self.broken:
            raise UpstreamError(503, "down")
        slot = int(stamp[8:10]) * 4 + int(stamp[10:12]) // 15
        # A different place per quarter hour: FeatureID is what identifies a place now, so it varies too.
        return zipped(row(lat=str(slot % 80), fid=str(7000000 + slot), added=stamp))


def test_window_downloads_each_export_once_and_rolls():
    fake = FakeGdelt("20261023044500")
    window = gdelt.Window(fake.text, fake.bytes, workers=1)
    first, withheld = window.refresh()
    assert len(fake.calls) == 12 and window.loaded() == 12 and len(first) == 12 and withheld == 0
    fake.calls.clear()
    assert len(window.refresh()[0]) == 12 and fake.calls == []  # nothing new, nothing fetched
    fake.latest = "20261023050000"
    window.refresh()
    assert fake.calls == ["20261023050000"] and window.loaded() == 12  # oldest dropped


def test_window_tolerates_missing_and_failing_files():
    fake = FakeGdelt("20261023044500")
    fake.missing, fake.broken = {"20261023043000"}, {"20261023041500"}
    window = gdelt.Window(fake.text, fake.bytes, workers=1)
    assert len(window.refresh()[0]) == 10
    fake.calls.clear()
    fake.broken.clear()
    window.refresh()
    assert fake.calls == ["20261023041500"]  # a 404 is remembered as empty; a 503 is retried


def test_window_raises_when_nothing_loads():
    fake = FakeGdelt("20261023044500")
    fake.broken = set(gdelt.window_stamps(fake.latest))
    with pytest.raises(UpstreamError, match="Could not download"):
        gdelt.Window(fake.text, fake.bytes, workers=1).refresh()


def test_an_export_is_judged_against_its_own_stamp():
    # A row whose SQLDATE is the export's own date is fresh; one dated years earlier is not, and is
    # withheld. The judgement uses the file's stamp, so it does not drift as the window is re-read.
    # One row, drawn twice: the same signals, only its own reported date different.
    weakly_placed = dict(fid="999999", lat="10", lon="10", geo_type="2", place="Poltava, Ukraine", a1_fid="999999",
                         sources="1")
    fresh = zipped(row(sqldate="20261023", **weakly_placed))
    old = zipped(row(sqldate="20161023", **weakly_placed))
    assert gdelt.to_items(gdelt.parse_export(gdelt.unzip_export(fresh), now=stamp("20261023044500")))[1] == 0
    groups = gdelt.parse_export(gdelt.unzip_export(old), now=stamp("20261023044500"))
    assert next(iter(groups.values()))["stale"] is True
    assert gdelt.to_items(groups)[1] == 1  # withheld
    assert gdelt.to_items(gdelt.parse_export(gdelt.unzip_export(old)))[1] == 0  # no stamp, nothing judged stale


def test_bad_zip_is_an_upstream_error():
    with pytest.raises(UpstreamError, match="not a valid zip"):
        gdelt.unzip_export(b"not a zip")
    assert "Kharkiv" in gdelt.unzip_export(zipped(row()))


def test_short_place():
    assert gdelt.short_place("Kharkiv, Kharkivs'ka Oblast', Ukraine") == "Kharkiv, Ukraine"
    assert gdelt.short_place("Ohio, United States") == "Ohio, United States"
    assert gdelt.short_place(None) == "Unknown place"
