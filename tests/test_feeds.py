"""Feed parsers over the saved fixtures: deterministic ids, categories, cutoffs, and merge() rules. No network."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from highground import feeds, geo
from highground.feeds import chp_docs, datasf_doc, iem_docs, merge, nws_docs, open311_doc, tide_docs, weather_docs

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


def load(name):
    return json.loads((FIX / name).read_text())


# ---------------------------------------------------------------- Open311 (live)


def test_open311_sewer_fixture():
    rows = load("open311_sewer.json")
    docs = [open311_doc(r) for r in rows]
    assert all(d is not None for d in docs) and len(docs) == len(rows)
    for row, d in zip(rows, docs, strict=True):
        assert d["_id"] == f"311:{row['service_request_id']}"
        assert d["case_id"] == str(row["service_request_id"])
        assert d["category"] == feeds.classify_text(row.get("description"))  # PUC:Water:Sewer maps to None -> keyword classification
        assert d["category_source"] == "keywords"
        assert d["requested_at"] == feeds.utc(row["requested_datetime"]).isoformat()
        assert d["updated_at"] == feeds.utc(row["updated_datetime"]).isoformat()
        assert d["location"] == {"lat": float(row["lat"]), "lon": float(row["long"])}
        assert d["h3_9"] == geo.cell(float(row["lat"]), float(row["long"]))
        assert d["seen_open311"] is True and d["status"] == row["status"]
    assert len({d["_id"] for d in docs}) == len(docs)  # ids unique
    # deterministic: parsing twice yields identical docs
    assert [open311_doc(r) for r in rows] == docs


def test_open311_fixed_service_codes_and_subcategories():
    tree = {"service_request_id": "1", "lat": 37.7749, "long": -122.4194, "requested_datetime": "2026-11-03T10:00:00Z",
            "service_code": "PW:BUF:Tree Maintenance", "description": "A fallen tree is blocking the sidewalk"}
    d = open311_doc(tree)
    assert d["category"] == "tree" and d["subcategory"] == "fallen_or_damaged" and d["category_source"] == "service_code"
    pothole = {**tree, "service_request_id": "2", "service_code": "input:Pothole & Street Issues", "description": "big pothole"}
    d = open311_doc(pothole)
    assert d["category"] == "street" and d["category_source"] == "service_code" and d.get("subcategory") is None  # None fields are dropped


def test_open311_rejects_bad_rows():
    assert open311_doc({"service_request_id": "1", "requested_datetime": "2026-11-03T10:00:00Z"}) is None  # no lat
    assert open311_doc({"service_request_id": "1", "lat": 37.7749, "long": -122.4194}) is None  # no requested_datetime
    assert open311_doc({"service_request_id": "1", "lat": 0.0, "long": 0.0, "requested_datetime": "2026-11-03T10:00:00Z"}) is None  # outside SF
    assert open311_doc({"service_request_id": "1", "lat": 40.7, "long": -74.0, "requested_datetime": "2026-11-03T10:00:00Z"}) is None  # NYC


# ---------------------------------------------------------------- DataSF 311 (history)


def test_datasf_fixture():
    rows = load("datasf_311_20221231.json")
    docs = [d for d in (datasf_doc(r) for r in rows) if d]
    assert 0 < len(docs) < len(rows)  # rows outside SF / non-actionable subtypes are dropped
    by_id = {r["service_request_id"]: r for r in rows}
    for d in docs:
        row = by_id[d["case_id"]]
        assert d["_id"] == f"311:{d['case_id']}"
        assert d["category_source"] == "datasf"
        assert d["requested_at"] == feeds.pacific(row["requested_datetime"]).isoformat()  # floating Pacific time -> UTC
        assert d["status"] == (row.get("status_description") or "").lower() or None
        assert d["h3_9"] == geo.cell(float(row["lat"]), float(row["long"]))
        assert d["seen_datasf"] is True
        name, st = row["service_name"], (row.get("service_subtype") or "").lower()
        if name in ("Sewer Issues", "Sewer"):
            assert d["category"] == feeds.SEWER_SUB.get(st, "other") and d["subcategory"] == st
        elif name == "Catch Basin Maintenance":
            assert d["category"] == "catch_basin"
        elif name == "Tree Maintenance":
            assert d["category"] == "tree" and d["subcategory"] == "fallen_or_damaged"
        elif name == "Street Defects":
            assert d["category"] == "street"
    cats = {d["category"] for d in docs}
    assert {"flooding", "tree", "street"} <= cats
    assert [d for d in (datasf_doc(r) for r in rows) if d] == docs  # deterministic


def test_datasf_pacific_timestamps_become_utc():
    row = {"service_request_id": "9", "lat": 37.7749, "long": -122.4194, "requested_datetime": "2022-12-31T10:00:00.000",
           "service_name": "Sewer Issues", "service_subtype": "Flooding", "status_description": "Closed"}
    d = datasf_doc(row)
    assert d["requested_at"] == "2022-12-31T18:00:00+00:00"  # 10:00 PST == 18:00Z
    assert d["category"] == "flooding" and d["subcategory"] == "flooding" and d["status"] == "closed"


def test_datasf_rejects_unmapped_services_and_bad_rows():
    base = {"lat": 37.7749, "long": -122.4194, "requested_datetime": "2022-12-31T10:00:00"}
    assert datasf_doc({**base, "service_name": "Pothole"}) is None
    assert datasf_doc({**base, "service_name": "Tree Maintenance", "service_subtype": "Trees - Other"}) is None  # not damaged
    assert datasf_doc({"service_request_id": "5", "long": -122.4194, "requested_datetime": "2022-12-31T10:00:00",
                       "service_name": "Sewer Issues", "service_subtype": "Flooding"}) is None  # no lat
    no_dt = {"service_request_id": "6", "lat": 37.7749, "long": -122.4194, "service_name": "Sewer Issues", "service_subtype": "Flooding"}
    assert datasf_doc(no_dt) is None  # no requested_datetime


# ---------------------------------------------------------------- NWS


def test_nws_docs_fixture():
    docs = nws_docs(load("nws_alerts.json"))
    assert docs and {d["event"] for d in docs} <= feeds.RELEVANT
    for d, f in zip(docs, [f for f in load("nws_alerts.json")["features"] if f["properties"]["event"] in feeds.RELEVANT], strict=True):
        p = f["properties"]
        assert d["_id"] == f"nws:{p['id']}"
        assert d["event"] == p["event"]
        assert d["valid_from"] == (feeds.utc(p.get("onset") or p.get("effective"))).isoformat()
        assert d["valid_to"] == (feeds.utc(p.get("ends") or p.get("expires"))).isoformat()
        assert d["@timestamp"] == (feeds.utc(p.get("sent")) or feeds.utc(p.get("onset") or p.get("effective"))).isoformat()
        assert d["kind"] == "nws_alert" and d["url"] == p.get("@id")
    assert nws_docs(load("nws_alerts.json")) == docs  # deterministic ids


def test_nws_docs_skips_irrelevant_events():
    payload = {"features": [{"properties": {"id": "x1", "event": "Earthquake Warning", "sent": "2026-11-03T11:00:00Z",
                                            "onset": "2026-11-03T12:00:00Z", "ends": "2026-11-03T13:00:00Z"}}]}
    assert nws_docs(payload) == []


def test_iem_docs_fixture_window():
    payload = load("iem_mtr_20221231.json")
    start, end, day = "2022-12-29T08:00:00Z", "2023-01-01T08:00:00Z", "2022-12-31"
    docs = iem_docs(payload, start, end, day)
    assert docs
    ids = [d["_id"] for d in docs]
    assert len(ids) == len(set(ids))  # deduped by phenomena.significance.eventid
    for d in docs:
        assert d["_id"].startswith(f"iem:{day}:")
        assert d["event"] in set(feeds.VTEC.values())
        assert d["valid_from"] < end and d["valid_to"] > start  # overlaps the window
        assert d["kind"] == "nws_alert" and d["replay"] == day
        assert d["issued_at"] and d["valid_from"] and d["valid_to"]
    assert any(d["event"] == "Flood Watch" for d in docs)  # FA.A on CAZ006
    assert iem_docs(payload, start, end, day) == docs  # deterministic


def test_iem_docs_window_and_zone_filters():
    payload = load("iem_mtr_20221231.json")
    assert iem_docs(payload, "2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z", "x") == []  # nothing overlaps
    ev = {"phenomena": "FA", "significance": "Y", "eventid": 99, "ugc": "CAZ006", "issue": "2022-12-31T10:00:00Z",
          "expire": "2022-12-31T12:00:00Z", "product_issued": "2022-12-31T09:55:00Z", "url": "https://x"}
    docs = iem_docs({"events": [ev, dict(ev)]}, "2022-12-31T00:00:00Z", "2023-01-01T00:00:00Z", "2022-12-31")
    assert len(docs) == 1 and docs[0]["_id"] == "iem:2022-12-31:FA.Y.99" and docs[0]["event"] == "Flood Advisory"
    outside = {**ev, "eventid": 100, "ugc": "CAC041"}  # not an SF zone
    assert iem_docs({"events": [outside]}, "2022-12-31T00:00:00Z", "2023-01-01T00:00:00Z", "d") == []
    assert iem_docs({"events": [{**ev, "eventid": 101, "phenomena": "SC"}]}, "2022-12-31T00:00:00Z", "2023-01-01T00:00:00Z", "d") == []  # not in VTEC map


# ---------------------------------------------------------------- NOAA tides


def test_tide_docs_hourly_observations():
    docs = tide_docs(load("noaa_hourly_20221231.json"), "tide_obs")
    rows = load("noaa_hourly_20221231.json")["data"]
    assert len(docs) == len(rows) == 48
    for d, row in zip(docs, rows, strict=True):
        t = datetime.strptime(row["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        assert d["_id"] == f"tide_obs:{t:%Y%m%d%H%M}"
        assert d["@timestamp"] == t.isoformat() and d["value"] == float(row["v"]) and d["kind"] == "tide_obs" and d["source"] == "noaa"
    assert len({d["_id"] for d in docs}) == len(docs)
    assert tide_docs(load("noaa_hourly_20221231.json"), "tide_obs") == docs


def test_tide_docs_predictions_and_missing_values():
    docs = tide_docs(load("noaa_pred.json"), "tide_pred")
    assert len(docs) == 8 and {d["tide_type"] for d in docs} == {"H", "L"}
    assert all(d["_id"].startswith("tide_pred:") and d["kind"] == "tide_pred" for d in docs)
    assert tide_docs({"data": [{"t": "2022-12-31 00:00", "v": ""}, {"t": "2022-12-31 01:00", "v": None}, {"t": "2022-12-31 02:00", "v": "1.5"}]}, "k") == [
        {"_id": "k:202212310200", "@timestamp": "2022-12-31T02:00:00+00:00", "kind": "k", "value": 1.5, "tide_type": None, "source": "noaa"}
    ]


# ---------------------------------------------------------------- Open-Meteo weather


def test_weather_docs_archive_fixture():
    payload = load("openmeteo_archive_20221231.json")  # 2 blocks -> first 2 points (nw, sw), 72 h each
    docs = weather_docs(payload, None, tag="replay2022-12-31", source="open-meteo-archive", replay="2022-12-31")
    assert len(docs) == 2 * 72
    for d in docs:
        assert d["point"] in geo.POINTS and d["kind"] == "weather"
        assert d["_id"].startswith("replay2022-12-31:") and d["_id"].split(":")[1] == d["point"]
        assert d["forecast"] is False and d["source"] == "open-meteo-archive" and d["replay"] == "2022-12-31"
        assert isinstance(d["rain_mm"], float)
    assert len({d["_id"] for d in docs}) == len(docs)
    assert docs[0]["_id"] == f"replay2022-12-31:{docs[0]['point']}:2022123000"
    assert weather_docs(payload, None, tag="replay2022-12-31", source="open-meteo-archive", replay="2022-12-31") == docs


def test_weather_docs_forecast_flag():
    now = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    docs = weather_docs(load("openmeteo_forecast.json"), now)
    assert docs and all(d["_id"].startswith("wx:") for d in docs)
    for d in docs:
        ts = datetime.fromisoformat(d["@timestamp"])
        assert d["forecast"] == (ts > now)
    assert any(d["forecast"] for d in docs) and any(not d["forecast"] for d in docs)


# ---------------------------------------------------------------- CHP incidents


def test_chp_docs_fixture():
    docs = chp_docs((FIX / "chp_ggcc.xml").read_text(), NOW)
    assert len(docs) == 2  # only the two SF logs with a relevant LogType + LATLON survive
    assert {d["_id"] for d in docs} == {"chp:261103GG1447", "chp:261103GG1422"}
    for d in docs:
        assert "Traffic Hazard" in d["type"]
        assert geo.in_sf(d["location"]["lat"], d["location"]["lon"])
        assert d["h3_9"] == geo.cell(d["location"]["lat"], d["location"]["lon"])
        assert d["flooding"] is False
        assert datetime.fromisoformat(d["started_at"]).tzinfo is not None
        assert d["last_seen"] == NOW.isoformat()
    assert chp_docs((FIX / "chp_ggcc.xml").read_text(), NOW) == docs  # deterministic


def test_chp_docs_filters_and_flooding_flag():
    xml = """<State><Center ID="GGCC"><Dispatch ID="GGCC">
      <Log ID="A1"><LogTime>"Nov  3 2026  3:15PM"</LogTime><LogType>"1135-Flood"</LogType><Location>"US101 S / Paul Ave"</Location>
        <LATLON>"37774900:122419400"</LATLON><LogDetails><details><IncidentDetail>"water over the roadway"</IncidentDetail></details></LogDetails></Log>
      <Log ID="A2"><LogTime>"Nov  3 2026  3:15PM"</LogTime><LogType>"1183-Trfc Collision-Unkn Inj"</LogType><Location>"I580 E"</Location>
        <LATLON>"37702256:121744729"</LATLON></Log>
      <Log ID="A3"><LogTime>"Nov  3 2026  3:15PM"</LogTime><LogType>"1125-Traffic Hazard"</LogType><Location>"outside SF"</Location>
        <LATLON>"38376909:122638719"</LATLON></Log>
      <Log ID="A4"><LogTime>"Nov  3 2026  3:15PM"</LogTime><LogType>"1125-Traffic Hazard"</LogType><Location>"no coords"</Location></Log>
    </Dispatch></Center></State>"""
    docs = chp_docs(xml, NOW)
    assert [d["_id"] for d in docs] == ["chp:A1"]  # collision type filtered, non-SF filtered, missing LATLON filtered
    assert docs[0]["flooding"] is True
    assert docs[0]["location"] == {"lat": 37.7749, "lon": -122.4194}
    assert docs[0]["started_at"] == "2026-11-03T23:15:00+00:00"  # 3:15PM PST == 23:15Z


# ---------------------------------------------------------------- merge


def test_merge_none_returns_new_without_derived_keys():
    new = {"_id": "311:1", "category": "flooding", "zone_100yr": True, "zones": ["x"], "slr24": False, "status": "open"}
    out = merge(None, new)
    assert out == {"_id": "311:1", "category": "flooding", "status": "open"}


def test_merge_stronger_category_source_wins():
    old = {"_id": "311:1", "category": "flooding", "subcategory": "flooding", "category_source": "keywords", "status": "open",
           "updated_at": "2022-12-31T10:00:00+00:00", "zone_100yr": True, "description": "keep me"}
    new = {"_id": "311:1", "category": "sewer_backup", "subcategory": "sewage_back_up", "category_source": "datasf", "status": "closed",
           "updated_at": "2022-12-31T11:00:00+00:00"}
    out = merge(old, new)
    assert out["category"] == "sewer_backup" and out["subcategory"] == "sewage_back_up" and out["category_source"] == "datasf"
    assert out["status"] == "closed" and out["updated_at"] == "2022-12-31T11:00:00+00:00"  # newer status wins
    assert out["description"] == "keep me" and out["_id"] == "311:1"
    assert "zone_100yr" not in out and "zones" not in out and "slr24" not in out  # derived keys never stored


def test_merge_weaker_source_keeps_old_category_but_new_status():
    old = {"_id": "311:2", "category": "sewer_backup", "category_source": "datasf", "status": "open", "updated_at": "2022-12-31T10:00:00+00:00"}
    new = {"_id": "311:2", "category": "flooding", "category_source": "keywords", "status": "closed", "updated_at": "2022-12-31T12:00:00+00:00"}
    out = merge(old, new)
    assert out["category"] == "sewer_backup" and out["category_source"] == "datasf"  # stronger source keeps the category
    assert out["status"] == "closed" and out["updated_at"] == "2022-12-31T12:00:00+00:00"  # but the newer status still lands


def test_merge_older_status_does_not_overwrite():
    old = {"_id": "311:3", "category": "flooding", "category_source": "datasf", "status": "closed", "updated_at": "2022-12-31T12:00:00+00:00"}
    new = {"_id": "311:3", "category": "flooding", "category_source": "datasf", "status": "open", "updated_at": "2022-12-31T10:00:00+00:00"}
    out = merge(old, new)
    assert out["status"] == "closed" and out["updated_at"] == "2022-12-31T12:00:00+00:00"


def test_merge_rank_order():
    base = {"_id": "311:4", "status": "open", "updated_at": "2022-12-31T10:00:00+00:00"}
    out = merge({**base, "category": "a", "category_source": "service_code"}, {**base, "category": "b", "category_source": "mistral", "updated_at": "2022-12-31T11:00:00+00:00"})
    assert out["category"] == "b"  # mistral (2) beats service_code (1)
    out = merge({**base, "category": "a", "category_source": "mistral"}, {**base, "category": "b", "category_source": "service_code", "updated_at": "2022-12-31T11:00:00+00:00"})
    assert out["category"] == "a"
    out = merge({**base, "category": "a", "category_source": "keywords"}, {**base, "category": "b", "category_source": "keywords", "updated_at": "2022-12-31T11:00:00+00:00"})
    assert out["category"] == "b"  # equal rank -> the newer doc wins the category


# ---------------------------------------------------------------- classification helpers


@pytest.mark.parametrize("text,expected", [
    ("Sewage back up", "sewer_backup"),
    ("Light flooding on the sidewalk, standing water", "flooding"),
    ("A sewer is overflowing from the catch basin", "flooding"),  # flood regex (overflow) is checked first
    ("Storm drain is clogged", "catch_basin"),
    ("Water is leaking out of the water meter", "water_leak"),
    ("pothole", "other"),
    (None, "other"),
])
def test_classify_text(text, expected):
    assert feeds.classify_text(text) == expected
