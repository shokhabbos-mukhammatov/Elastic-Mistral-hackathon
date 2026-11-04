import math

import h3

RES = 9  # one H3 res-9 cell ~= a city block or two
BBOX = (-122.5155, 37.7080, -122.3549, 37.8120)
# Weather sample points; each block uses the nearest one.
POINTS = {"nw": (37.780, -122.480), "sw": (37.735, -122.480), "c": (37.760, -122.435), "ne": (37.790, -122.410), "se": (37.735, -122.395)}


def cell(lat: float, lon: float) -> str:
    return h3.latlng_to_cell(lat, lon, RES)


def center(c: str) -> tuple[float, float]:
    return h3.cell_to_latlng(c)


def disk(c: str, k: int = 1) -> list[str]:
    return list(h3.grid_disk(c, k))


def polygon(c: str) -> dict:
    ring = [[lon, lat] for lat, lon in h3.cell_to_boundary(c)]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def in_sf(lat: float, lon: float) -> bool:
    return BBOX[1] <= lat <= BBOX[3] and BBOX[0] <= lon <= BBOX[2]


def dist_m(a_lat, a_lon, b_lat, b_lon) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lon - a_lon) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(h))


def nearest_point(lat: float, lon: float) -> str:
    return min(POINTS, key=lambda k: dist_m(lat, lon, *POINTS[k]))
