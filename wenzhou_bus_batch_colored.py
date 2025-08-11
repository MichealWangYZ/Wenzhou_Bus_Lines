#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AMap (Gaode) WebService → per-route GeoJSON + OSM preview

Workflow:
  - For each keyword (e.g., "B1路", "24路"), call:
      1) /v3/bus/linename?city=...&keywords=...&extensions=all
      2) pick the candidate with the smallest numeric `id`
      3) /v3/bus/lineid?city=...&id=<picked>&extensions=all
  - Convert GCJ-02 → WGS-84 for both route polyline and stops.
  - Write:
      route_<Line>.geojson  (LineString, WGS-84)
      stop_<Line>.geojson   (Points, WGS-84)
  - Skip fetching if both files already exist (unless --overwrite).
  - Generate outdir/preview.html with folium (OSM tiles).

Requirements:
  export AMAP_WS_KEY=your_webservice_key
  pip install folium (optional but recommended for preview)

Examples:
  export AMAP_WS_KEY=xxxx
  python wenzhou_bus_batch.py --city 温州 --keywords "B1路,B4路,24路" --outdir out_wz --preview
  python wenzhou_bus_batch.py --city 温州 --file routes.txt --outdir out_wz --overwrite --preview
"""

import os
import json
import math
import time
import pathlib
import argparse
import urllib.parse
import urllib.request
import itertools
from shapely.geometry import LineString
from pyproj import Transformer

AMAP_KEY = os.getenv("AMAP_WS_KEY")  # set env: export AMAP_WS_KEY=xxxx
BASE = "https://restapi.amap.com/v3"

# ---------- HTTP helpers ----------
def http_get(url: str, timeout: int = 20) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8")

def qs(params: dict) -> str:
    # allow commas in polyline
    return urllib.parse.urlencode(params, safe=",")

def api_linename(city: str, keyword: str) -> dict:
    url = f"{BASE}/bus/linename?{qs({'city': city,'keywords': keyword,'extensions':'all','output':'json','key': AMAP_KEY})}"
    return json.loads(http_get(url))

def api_lineid(city: str, line_id: str) -> dict:
    url = f"{BASE}/bus/lineid?{qs({'city': city,'id': line_id,'extensions':'all','output':'json','key': AMAP_KEY})}"
    return json.loads(http_get(url))

# ---------- GCJ-02 → WGS-84 ----------
def _out_of_china(lon, lat):
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)

def _tlat(x, y):
    ret = -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*abs(x)**0.5
    ret += (20.0*math.sin(6.0*x*math.pi) + 20.0*math.sin(2.0*x*math.pi))*2.0/3.0
    ret += (20.0*math.sin(y*math.pi) + 40.0*math.sin(y/3.0*math.pi))*2.0/3.0
    ret += (160.0*math.sin(y/12.0*math.pi) + 320*math.sin(y*math.pi/30.0))*2.0/3.0
    return ret

def _tlon(x, y):
    ret = 300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*abs(x)**0.5
    ret += (20.0*math.sin(6.0*x*math.pi) + 20.0*math.sin(x*math.pi))*2.0/3.0
    ret += (40.0*math.sin(x/3.0*math.pi) + 150.0*math.sin(x/12.0*math.pi) + 300.0*math.sin(x/30.0*math.pi))*2.0/3.0
    return ret

def gcj2wgs(lon, lat):
    if _out_of_china(lon, lat):
        return lon, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    dlon = _tlon(lon - 105.0, lat - 35.0)
    dlat = _tlat(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtMagic = math.sqrt(magic)
    dlon = (dlon * 180.0) / (a / math.cos(radlat) * sqrtMagic * math.pi)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtMagic) * math.pi)
    return lon - dlon, lat - dlat

# ---------- Geo helpers ----------
def parse_polyline(poly: str):
    pts = []
    for seg in poly.split(";"):
        s = seg.strip()
        if not s:
            continue
        x, y = s.split(",")
        pts.append((float(x), float(y)))
    return pts

def to_fc(features):
    return {"type": "FeatureCollection", "features": features}

def feature_line(coords_wgs, props):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords_wgs},
        "properties": props,
    }

def feature_point(lon, lat, props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }

def base_name(kw: str) -> str:
    # "B1路"→"B1", "24路"→"24"
    s = kw.strip().replace("（", "(").replace("）", ")")
    return s[:-1] if s.endswith("路") else s

# ---------- selection: pick min numeric id ----------
def idnum(s: str) -> int:
    try:
        return int(s)
    except Exception:
        return 10**18  # non-numeric ids lose

def pick_best_busline(cands):
    if not cands:
        return None
    return min(cands, key=lambda b: idnum(b.get("id", "")))

# ---------- main ----------
def run(city: str, keywords: list, outdir: pathlib.Path, overwrite: bool, preview: bool, preview_name: str = "preview.html"):
    outdir.mkdir(parents=True, exist_ok=True)
    all_route_feats, all_stop_feats = [], []

    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        base = base_name(kw)
        route_path = outdir / f"route_{base}.geojson"
        stops_path = outdir / f"stop_{base}.geojson"

        # Skip if outputs already exist
        if not overwrite and route_path.exists() and stops_path.exists():
            print(f"[skip] already exists: {route_path.name}, {stops_path.name}")
            # Load for preview accumulation
            try:
                rfc = json.loads(route_path.read_text("utf-8"))
                sfc = json.loads(stops_path.read_text("utf-8"))
                all_route_feats.extend(rfc.get("features", []))
                all_stop_feats.extend(sfc.get("features", []))
            except Exception as e:
                print(f"[warn] failed to load existing outputs for preview: {e}")
            continue

        print(f"[linename] querying: {kw}")
        ln = api_linename(city, kw)
        if ln.get("status") != "1":
            print(f"  ! linename failed: info={ln.get('info')}")
            continue

        best = pick_best_busline(ln.get("buslines", []))
        if not best:
            print("  ! no candidate after selection")
            continue

        line_id = best.get("id")
        print(f"  -> pick id={line_id}, name={best.get('name')}, company={best.get('company')}")
        time.sleep(0.2)  # gentle rate limit

        detail = api_lineid(city, line_id)
        if detail.get("status") != "1" or not detail.get("buslines"):
            print(f"  ! lineid failed: info={detail.get('info')}")
            continue

        L = detail["buslines"][0]
        coords_gcj = parse_polyline(L.get("polyline", ""))
        coords_wgs = [gcj2wgs(x, y) for x, y in coords_gcj]

        # Build features
        route_fc = to_fc([
            feature_line(coords_wgs, {
                "route_id": L.get("id"),
                "name": L.get("name"),
                "type": L.get("type"),
                "company": L.get("company"),
                "origin": L.get("start_stop"),
                "destination": L.get("end_stop"),
            })
        ])

        stop_feats = []
        for s in L.get("busstops", []):
            x, y = map(float, s["location"].split(","))
            wx, wy = gcj2wgs(x, y)
            stop_feats.append(feature_point(wx, wy, {
                "route_id": L.get("id"),
                "route_name": L.get("name"),
                "stop_name": s["name"],
            }))
        stops_fc = to_fc(stop_feats)

        # Write outputs
        route_path.write_text(json.dumps(route_fc, ensure_ascii=False), "utf-8")
        stops_path.write_text(json.dumps(stops_fc, ensure_ascii=False), "utf-8")
        print(f"  ✔ wrote {route_path.name}, {stops_path.name}")

        # Accumulate for preview
        all_route_feats.extend(route_fc["features"])
        all_stop_feats.extend(stop_feats)

    # Quick OSM preview
    if preview:
        try:
            import folium
            m = folium.Map(location=[28.000-0.02, 120.700], zoom_start=12, tiles="OpenStreetMap")
            LON_SHIFT = -0.00075  # ~200 ft west
            # Color palette
            colors = [
                "red", "blue", "green", "purple", "orange", "darkred", "lightred", "beige",
                "darkblue", "darkgreen", "cadetblue", "darkpurple", "pink", "lightblue",
                "lightgreen", "gray", "black", "lightgray"
            ]
            color_cycle = itertools.cycle(colors)
            # Offset values in meters (cycle if more routes)
            offsets = [-4, 0, 4, -8, 8, -12, 12, -16, 16, -20, 20]
            offset_cycle = itertools.cycle(offsets)
            # Set up projection: WGS84 to UTM zone 51N (covers Wenzhou)
            transformer_to_utm = Transformer.from_crs("epsg:4326", "epsg:32651", always_xy=True)
            transformer_to_wgs = Transformer.from_crs("epsg:32651", "epsg:4326", always_xy=True)
            # Map route_id to color for stops
            route_color_map = {}
            for f in all_route_feats:
                coords = f["geometry"]["coordinates"]
                name = f["properties"].get("name", "")
                route_id = f["properties"].get("route_id", None)
                color = next(color_cycle)
                offset = next(offset_cycle)
                if route_id:
                    route_color_map[route_id] = color
                if coords:
                    # Project to UTM
                    utm_coords = [transformer_to_utm.transform(lon + LON_SHIFT, lat) for lon, lat in coords]
                    line = LineString(utm_coords)
                    # Offset line (right side for positive, left for negative)
                    try:
                        offset_line = line.parallel_offset(offset, 'right', join_style=2)
                        # parallel_offset may return MultiLineString if the line is complex
                        if offset_line.geom_type == 'MultiLineString':
                            offset_line = list(offset_line)[0]
                        offset_utm_coords = list(offset_line.coords)
                        # Back to WGS84
                        offset_wgs_coords = [transformer_to_wgs.transform(x, y) for x, y in offset_utm_coords]
                        # Plot
                        m.add_child(folium.PolyLine(
                            [(lat, lon) for lon, lat in offset_wgs_coords],
                            weight=5,
                            color=color,
                            popup=name,
                            tooltip=name
                        ))
                    except Exception as e:
                        # Fallback: plot original if offset fails
                        m.add_child(folium.PolyLine(
                            [(lat, lon + LON_SHIFT) for lon, lat in coords],
                            weight=5,
                            color=color,
                            popup=name,
                            tooltip=name
                        ))
            # Aggregate route info for each stop
            stop_routes = {}  # key: (lon, lat, stop_name), value: set of route names
            stop_colors = {}  # key: (lon, lat, stop_name), value: set of route colors
            for f in all_stop_feats[:2000]:
                lon, lat = f["geometry"]["coordinates"]
                stop_name = f["properties"].get("stop_name", "")
                route_name = f["properties"].get("route_name", "")
                route_id = f["properties"].get("route_id", None)
                color = route_color_map.get(route_id, "black")
                key = (round(lon, 6), round(lat, 6), stop_name)
                stop_routes.setdefault(key, set()).add(route_name)
                stop_colors.setdefault(key, set()).add(color)
            for (lon, lat, stop_name), route_names in stop_routes.items():
                # Use the first color for the border
                color = list(stop_colors[(lon, lat, stop_name)])[0]
                popup_text = f"<b>Stop:</b> {stop_name}<br><b>Routes:</b> {', '.join(sorted(route_names))}"
                m.add_child(folium.CircleMarker(
                    (lat, lon + LON_SHIFT),
                    radius=4,
                    color=color,         # border color
                    fill=True,
                    fill_color="white", # inside color
                    fill_opacity=1,
                    weight=3,
                    popup=folium.Popup(popup_text, max_width=250)
                ))
            out_html = outdir / preview_name
            m.save(str(out_html))
            print(f"[ok] preview: {out_html}")
        except Exception as e:
            print("[skip] preview generation failed:", e)

def main():
    if not AMAP_KEY:
        raise SystemExit("AMAP_WS_KEY is not set. `export AMAP_WS_KEY=your_webservice_key`")

    ap = argparse.ArgumentParser(description="AMap bus → per-route GeoJSON (GCJ→WGS) + OSM preview")
    ap.add_argument("--city", default="温州",
                    help="City name or adcode, default: 温州")
    ap.add_argument("--keywords", help="Comma-separated route keywords, e.g., B1路,24路")
    ap.add_argument("--file", help="Text file with one keyword per line")
    ap.add_argument("--outdir", default="out_wz", help="Output directory")
    ap.add_argument("--overwrite", action="store_true", default=False, help="Force re-fetch even if files exist (default: False)")
    ap.add_argument("--preview", action="store_true", default=True, help="Produce an OSM preview HTML (default: True)")
    ap.add_argument("--preview_name", default="preview.html", help="Filename for OSM preview HTML (default: preview.html)")
    args = ap.parse_args()


    kws = []
    if args.keywords:
        kws += [k.strip() for k in args.keywords.split(",") if k.strip()]
    if args.file:
        kws += [l.strip() for l in open(args.file, "r", encoding="utf-8") if l.strip()]
    # Deduplicate while preserving order
    seen = set()
    kws = [x for x in kws if not (x in seen or seen.add(x))]
    if not kws:
        # default list if nothing provided
        kws = [
            "B1路", "B4路", "B6路", "B109路", "24路", "82路", "75路",
            "131路", "28路",  "59路", "52路", "103路", "47路",
            "43路", "61路", "21路", "48路", "130路", "22路",
        ]


    outdir = pathlib.Path(args.outdir)
    run(args.city, kws, outdir, args.overwrite, args.preview, args.preview_name)

if __name__ == "__main__":
    main()
