"""DataSF reference layers -> ES: flood zones (geo_shape) for the enrich policy, and a places index for geocoding."""

import re

import httpx

from highground.geo import cell, in_sf

SODA = "https://data.sf.gov/resource/{}.{}"
ZONES = {"100yr": "jzu3-4yxp", "slr24": "esku-ejgv", "neighborhood": "j2bu-swwd"}
_ORD = re.compile(r"\b0+(\d+(ST|ND|RD|TH))\b")


def zone_docs(http: httpx.Client) -> list[dict]:
    out = []
    for layer, ds in ZONES.items():
        feats = http.get(SODA.format(ds, "geojson"), params={"$limit": 5000}, timeout=120).json()["features"]
        for i, f in enumerate(feats):
            g = f.get("geometry")
            if not g:
                continue
            name = (f["properties"] or {}).get("nhood") or layer
            # one doc per polygon: a single self-intersecting part then can't sink a whole layer
            parts = [{"type": "Polygon", "coordinates": c} for c in g["coordinates"]] if g["type"] == "MultiPolygon" and layer != "neighborhood" else [g]
            out += [{"_id": f"{layer}:{i}:{j}", "layer": layer, "name": name, "geometry": p} for j, p in enumerate(parts)]
    return out


def norm_street(s: str) -> str:
    """DataSF writes '03RD ST'; people type '3rd st'."""
    return _ORD.sub(r"\1", (s or "").upper().strip())


def place_docs(http: httpx.Client, addresses: bool = True) -> list[dict]:
    out = []
    for r in http.get(SODA.format("jfxm-zeee", "json"), params={"$limit": 50000}, timeout=120).json():
        a, b = norm_street(r.get("street_name_1")), norm_street(r.get("street_name_2"))
        lat, lon = float(r.get("latitude") or 0), float(r.get("longitude") or 0)
        if a and b and in_sf(lat, lon):
            out.append({"_id": f"x:{r['cnn']}:{a}:{b}"[:500], "kind": "intersection", "name": f"{a} & {b}", "alt": f"{b} & {a}",
                        "location": {"lat": lat, "lon": lon}, "h3_9": cell(lat, lon)})  # fmt: skip
    if addresses:
        offset = 0
        while True:
            rows = http.get(SODA.format("3mea-di5p", "json"), params={"$select": "eas_address_id,address,latitude,longitude,nhood",
                            "$limit": 50000, "$offset": offset, "$order": "eas_address_id"}, timeout=180).json()  # fmt: skip
            for r in rows:
                lat, lon = float(r.get("latitude") or 0), float(r.get("longitude") or 0)
                if r.get("address") and in_sf(lat, lon):
                    out.append({"_id": f"a:{r['eas_address_id']}", "kind": "address", "name": norm_street(r["address"]),
                                "location": {"lat": lat, "lon": lon}, "neighborhood": r.get("nhood"), "h3_9": cell(lat, lon)})  # fmt: skip
            if len(rows) < 50000:
                break
            offset += 50000
    return out


_JOIN = re.compile(r"\s+(and|y|at|&|x|和|與|at the corner of)\s+|\s*[&/@]\s*", re.IGNORECASE)


def normalize_query(q: str) -> str:
    q = re.sub(r",?\s*(san francisco|sf)(,?\s*ca)?\b.*$", "", q.strip(), flags=re.IGNORECASE)
    q = _JOIN.sub(" & ", q)
    q = re.sub(r"\b(street)\b", "st", q, flags=re.IGNORECASE)
    q = re.sub(r"\b(avenue)\b", "ave", q, flags=re.IGNORECASE)
    return norm_street(q)


def geocode(store, q: str) -> dict | None:
    """Address or intersection -> point, from the places index (search_as_you_type, then fuzzy)."""
    nq = normalize_query(q)
    fields = ["name", "name._2gram", "name._3gram", "alt", "alt._2gram", "alt._3gram"]
    kind = {"term": {"kind": "intersection"}} if "&" in nq else {"term": {"kind": "address"}}
    for mm in ({"type": "bool_prefix", "operator": "and"}, {"fuzziness": "AUTO", "operator": "and"}):
        hits = store.search("places", size=1, query={"bool": {"must": {"multi_match": {"query": nq, "fields": fields[:3] + fields[3:] if "&" in nq else fields[:3], **mm}},
                                                             "filter": kind}})["hits"]["hits"]  # fmt: skip
        if hits:
            s = hits[0]["_source"]
            label = re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), s["name"].title())
            return {"lat": s["location"]["lat"], "lon": s["location"]["lon"], "label": label, "neighborhood": s.get("neighborhood")}
    return None
