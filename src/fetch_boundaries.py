"""Fetch Australian state boundaries and reduce them to clean silhouettes.

The source file has 800+ polygon parts across the states — mostly tiny offshore
islands that add noise at dashboard scale. This keeps only the mainland polygon
for each state (plus Tasmania's main island) and writes a small local GeoJSON.

Run once:  python src/fetch_boundaries.py
"""

import json
import urllib.request
from pathlib import Path

URL = ("https://raw.githubusercontent.com/rowanhogan/australian-states/"
       "master/states.min.geojson")

OUT = Path(__file__).resolve().parents[1] / "data" / "reference" / "au_states.geojson"

# STATE_NAME -> NEM region id. ACT is inside NSW1; WA and NT are outside the NEM.
REGION_OF = {
    "New South Wales": "NSW1",
    "Australian Capital Territory": "NSW1",
    "Victoria": "VIC1",
    "Queensland": "QLD1",
    "South Australia": "SA1",
    "Tasmania": "TAS1",
    "Western Australia": None,
    "Northern Territory": None,
}


def ring_area(ring):
    """Shoelace, unsigned. Good enough for picking the biggest polygon."""
    a = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def largest_part(geometry):
    """Return a Polygon geometry containing only the biggest part."""
    if geometry["type"] == "Polygon":
        return geometry
    parts = geometry["coordinates"]
    biggest = max(parts, key=lambda p: ring_area(p[0]))
    return {"type": "Polygon", "coordinates": biggest}


def main():
    print(f"fetching {URL}")
    src = json.loads(urllib.request.urlopen(URL, timeout=120).read())

    features = []
    for f in src["features"]:
        name = f["properties"]["STATE_NAME"]
        geom = largest_part(f["geometry"])
        features.append({
            "type": "Feature",
            "properties": {
                "state": name,
                "region_id": REGION_OF.get(name),
                "in_nem": REGION_OF.get(name) is not None,
            },
            "geometry": geom,
        })
        pts = len(geom["coordinates"][0])
        print(f"  {name:<30} {pts:>5} points")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)

    print(f"\nwrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
