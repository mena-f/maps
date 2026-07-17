from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
import folium, requests, re, os, math

# Load environment variables from .env file (keeps API keys out of code)
load_dotenv()

# app.root_path defaults to the folder this file lives in, so index.html
# and static/ are found no matter what directory you launch python from.
app = Flask(__name__, static_folder="static", static_url_path="/static")

# ── API endpoints ──
NOM         = "https://nominatim.openstreetmap.org/search"   # Address search
NOM_REVERSE = "https://nominatim.openstreetmap.org/reverse"  # Coords → containing city boundary
OSRM        = "https://router.project-osrm.org/route/v1"     # Driving routes
ORS         = "https://api.openrouteservice.org/v2/directions" # Walking/cycling routes
ORS_KEY     = os.getenv("ORS_KEY")                            # Loaded from .env
HDR         = {"User-Agent": "DestinationMapApp/1.0"}

# ── Route colors: (primary, alternative) ──
PROFILES = {
    "driving": ("#0057FF", "#7aaeff"),
    "walking": ("#00A86B", "#7ad4b0"),
    "cycling": ("#FF6600", "#ffaa66"),
}

# ── ORS profile names for walking/cycling ──
ORS_PROFILE = {
    "walking": "foot-walking",
    "cycling": "cycling-regular",
}

# ── Common abbreviations expanded before geocoding ──
ABBREVS = {
    r"\buofm\b":    "University of Michigan",
    r"\bu of m\b":  "University of Michigan",
    r"\bumich\b":   "University of Michigan",
    r"\bMSU\b":     "Michigan State University",
    r"\bWSU\b":     "Wayne State University",
    r"\bDTW\b":     "Detroit Metropolitan Airport",
    r"\bst\b\.?":   "Street",
    r"\bave?\b\.?": "Avenue",
    r"\bblvd\b\.?": "Boulevard",
    r"\bdr\b\.?":   "Drive",
    r"\brd\b\.?":   "Road",
    r"\bpkwy\b\.?": "Parkway",
    r"\bhwy\b\.?":  "Highway",
}

def expand(q):
    """Replace shorthand abbreviations with full words before searching."""
    for pat, rep in ABBREVS.items():
        q = re.sub(pat, rep, q, flags=re.IGNORECASE)
    return q.strip()

def geocode(q):
    """Convert an address string to (lat, lon) using Nominatim."""
    r = requests.get(NOM, params={"q": expand(q), "format": "json", "limit": 1}, headers=HDR, timeout=10)
    d = r.json()
    return (float(d[0]["lat"]), float(d[0]["lon"])) if d else None

def main_road(legs):
    """Given a route's legs (each with turn-by-turn steps that have a name/distance),
    return whichever named road covers the most distance — used as a short 'via X' label.
    Returns None if no leg/step data is available or every step is unnamed."""
    totals = {}
    for leg in legs or []:
        for step in leg.get("steps") or []:
            name = (step.get("name") or "").strip()
            if not name:
                continue
            totals[name] = totals.get(name, 0) + (step.get("distance") or 0)
    return max(totals, key=totals.get) if totals else None

def rank_times(routes):
    """Label each route 'fast' (fastest), 'slow' (slowest), or 'mid' (anything else), for
    color-coding. Returns all None when there's no real time spread (e.g. only one route)."""
    mins_list = [rt["mins"] for rt in routes]
    fastest, slowest = min(mins_list), max(mins_list)
    if fastest == slowest:
        return [None] * len(routes)
    return [
        "fast" if m == fastest else "slow" if m == slowest else "mid"
        for m in mins_list
    ]

def haversine_km(p1, p2):
    """Great-circle distance in km between two (lat, lon) points."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def _point_in_ring(lat, lon, ring):
    """Standard ray-casting point-in-polygon test. `ring` is a list of [lon, lat] pairs."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]  # lon, lat
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside

def point_in_geojson(lat, lon, geometry):
    """True if (lat, lon) falls inside a GeoJSON Polygon or MultiPolygon geometry."""
    if not geometry:
        return False
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        return False
    for poly in polygons:
        if not poly:
            continue
        outer, holes = poly[0], poly[1:]
        if _point_in_ring(lat, lon, outer) and not any(_point_in_ring(lat, lon, h) for h in holes):
            return True
    return False

def get_city_boundary(lat, lon):
    """Look up the administrative boundary polygon for whichever city contains (lat, lon),
    using Nominatim's reverse geocoder. Returns a GeoJSON geometry dict, or None if no
    city-level boundary could be found (e.g. a rural point with no defined city polygon)."""
    try:
        r = requests.get(
            NOM_REVERSE,
            params={
                "lat": lat, "lon": lon, "format": "json",
                "zoom": 10, "polygon_geojson": 1,
            },
            headers=HDR, timeout=10,
        )
        data = r.json()
        return data.get("geojson")
    except Exception:
        return None

def compute_legal_distance(rt, boundary):
    """Km/minutes from wherever this route first leaves the origin city's boundary,
    to the destination. Returns (0, 0) if no boundary is known, or the route never
    leaves it — per spec, this must default to 0 rather than the full route distance."""
    if not boundary:
        return 0.0, 0
    coords = rt["coords"]
    exit_idx = None
    for i, (lat, lon) in enumerate(coords):
        if not point_in_geojson(lat, lon, boundary):
            exit_idx = i
            break
    if exit_idx is None:
        return 0.0, 0
    remaining = coords[exit_idx:]
    dist_km = round(sum(haversine_km(remaining[i], remaining[i + 1]) for i in range(len(remaining) - 1)), 2)
    # Approximate legal duration proportionally, based on the share of the route's
    # total distance that falls after the city-boundary exit point.
    frac = dist_km / rt["dist"] if rt["dist"] else 0
    legal_min = round(rt["mins"] * frac)
    return dist_km, legal_min

def get_routes_driving(points):
    """Fetch driving route(s) from OSRM through every point in order (start, any stops, end).
    OSRM only returns real alternatives for a direct start→end trip — once stops are added,
    there's just one route through all the waypoints."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    r = requests.get(
        f"{OSRM}/driving/{coords}",
        params={
            # steps=true so each route includes road names, used to show "via <road>"
            "overview": "full", "geometries": "geojson", "steps": "true",
            "alternatives": "true" if len(points) == 2 else "false",
        },
        timeout=10
    )
    data = r.json()
    if data.get("code") != "Ok":
        return None
    return [
        {
            "coords": [(p[1], p[0]) for p in rt["geometry"]["coordinates"]],
            "dist":   round(rt["distance"] / 1000, 2),
            "mins":   round(rt["duration"] / 60),
            "road":   main_road(rt.get("legs")),
        }
        for rt in data["routes"]
    ]

def get_routes_ors(points, mode):
    """Fetch a walking/cycling route from OpenRouteService through every point in order.
    Alternatives are only requested for a direct start→end trip — ORS doesn't support
    alternative_routes once there are more than two waypoints."""
    body = {"coordinates": [[lon, lat] for lat, lon in points]}
    if len(points) == 2:
        # Ask for up to 2 extra alternatives, even if somewhat longer than the best route
        body["alternative_routes"] = {"target_count": 3, "weight_factor": 1.6, "share_factor": 0.6}
    r = requests.post(
        f"{ORS}/{ORS_PROFILE[mode]}/geojson",
        headers={"Authorization": ORS_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=15
    )
    data = r.json()
    if "features" not in data or not data["features"]:
        return None
    routes = [
        {
            "coords": [(p[1], p[0]) for p in ft["geometry"]["coordinates"]],
            "dist":   round(ft["properties"]["summary"]["distance"] / 1000, 2),
            "mins":   round(ft["properties"]["summary"]["duration"] / 60),
            "road":   main_road(ft.get("properties", {}).get("segments")),
        }
        for ft in data["features"]
    ]
    # Fastest route first, alternatives after (matches how OSRM's driving routes are ordered)
    routes.sort(key=lambda rt: rt["mins"])
    return routes

def get_routes(points, mode):
    """Route dispatcher — driving uses OSRM, walking/cycling use ORS. `points` is the
    full ordered list of (lat, lon): start, any stops in between, then the destination."""
    return get_routes_driving(points) if mode == "driving" else get_routes_ors(points, mode)

def build_map(points=None, routes=None, mode="driving"):
    """Build and return a Folium map as an HTML string. `points` is the full ordered
    list of (lat, lon) — start, any stops in between, then the destination."""
    start = points[0] if points else None
    m = folium.Map(location=start or [20, 0], zoom_start=13 if start else 2)

    # ESRI World Street Map — shows labels, roads, and terrain at all zoom levels
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Source: Esri, HERE, DeLorme, USGS, Intermap, NRCAN, METI, TomTom",
        name="ESRI Street",
    ).add_to(m)

    # Slightly desaturate tiles so route lines stand out clearly, and style the
    # persistent time/distance badges drawn on each route (Google-Maps-style pills).
    m.get_root().html.add_child(folium.Element("""
        <style>
        .leaflet-tile { filter: saturate(0.75) brightness(1.02); }
        .route-badge {
            background:#fff; color:#202124; border-radius:12px; padding:12px 18px;
            font-family:"Segoe UI",sans-serif; font-size:1rem; line-height:1.35;
            text-align:center; white-space:nowrap; cursor:pointer; min-width:76px;
            box-shadow:0 2px 8px rgba(0,0,0,.4); transform:translate(-50%,-100%);
        }
        .route-badge-time { font-weight:700; font-size:1.05rem; }
        .route-badge-dist { font-size:.85rem; color:#5f6368; margin-top:2px; }
        .route-badge.rank-fast .route-badge-time { color:#188038; }
        .route-badge.rank-slow .route-badge-time { color:#d93025; }
        .route-badge.rank-mid  .route-badge-time { color:#5f6368; }
        </style>
    """))

    if routes:
        color, alt_color = PROFILES[mode]
        ranks = rank_times(routes)
        all_pts = []
        layer_names = [None] * len(routes)  # JS variable name of each route's line, by route index
        badge_names = [None] * len(routes)  # ...and of each route's time/distance badge marker

        # Draw alternatives first so the selected route ends up on top, but track each
        # line's original index so it lines up with the `routes` stats sent to the browser.
        for i in range(len(routes) - 1, -1, -1):
            rt = routes[i]
            is_best = (i == 0)
            line = folium.PolyLine(
                rt["coords"],
                color=color if is_best else alt_color,
                weight=6 if is_best else 4,
                opacity=0.9 if is_best else 0.6,
            )
            line.add_to(m)
            layer_names[i] = line.get_name()
            all_pts += rt["coords"]

            # A persistent time/distance pill at the route's midpoint (time first, then
            # distance, per the site-wide ordering), color-coded fastest/slowest/middle.
            mid = rt["coords"][len(rt["coords"]) // 2]
            rank_class = f"rank-{ranks[i]}" if ranks[i] else "rank-mid"
            dist_mi = round(rt["dist"] * 0.621371, 2)
            badge_html = (
                f'<div class="route-badge {rank_class}">'
                f'<div class="route-badge-time">{rt["mins"]} min</div>'
                f'<div class="route-badge-dist">{dist_mi} mi</div>'
                f'</div>'
            )
            badge = folium.Marker(
                location=mid,
                icon=folium.DivIcon(html=badge_html, icon_size=(0, 0), icon_anchor=(0, 0)),
            )
            badge.add_to(m)
            badge_names[i] = badge.get_name()

        # Auto-fit map bounds to show every route
        m.fit_bounds([[min(p[0] for p in all_pts), min(p[1] for p in all_pts)],
                      [max(p[0] for p in all_pts), max(p[1] for p in all_pts)]])

        # Thicken every route line automatically when zoomed out (so it stays easy to spot
        # and hover/click), and — when there's more than one route — let clicking any line
        # or badge select it. Wrapped in setTimeout so it runs after Folium's own map/layer
        # setup code, regardless of exactly where in the page that code ends up.
        m.get_root().script.add_child(folium.Element(f"""
            setTimeout(function() {{
                var layers = [{",".join(layer_names)}];
                var badges = [{",".join(badge_names)}];
                var mainColor = {color!r}, altColor = {alt_color!r};
                var selectable = layers.length > 1;
                var selected = 0;

                function weightFor(zoom, isSelected) {{
                    var base = isSelected ? 6 : 4;
                    var extra = zoom < 13 ? (13 - zoom) * 0.7 : 0;
                    return Math.min(base + extra, base + 10);
                }}

                function restyle() {{
                    var zoom = {m.get_name()}.getZoom();
                    layers.forEach(function(layer, i) {{
                        var isSelected = (i === selected);
                        layer.setStyle({{
                            color: isSelected ? mainColor : altColor,
                            weight: weightFor(zoom, isSelected),
                            opacity: isSelected ? 0.9 : 0.6,
                        }});
                        if (isSelected) layer.bringToFront();
                    }});
                }}

                // Exposed so the parent page's sidebar route list can also select a route
                window.__selectRoute = function(i) {{
                    if (!selectable || i < 0 || i >= layers.length) return;
                    selected = i;
                    restyle();
                }};

                if (selectable) {{
                    function onPick(i) {{
                        window.__selectRoute(i);
                        if (window.parent) {{
                            window.parent.postMessage({{type: "routeSelected", index: i}}, "*");
                        }}
                    }}
                    layers.forEach(function(layer, i) {{ layer.on('click', function() {{ onPick(i); }}); }});
                    badges.forEach(function(badge, i) {{ badge.on('click', function() {{ onPick(i); }}); }});
                }}

                {m.get_name()}.on('zoomend', restyle);
                restyle();
            }}, 0);
        """))

    # Add start, stop, and destination markers
    if points:
        folium.Marker(points[0], tooltip="Start",
            icon=folium.Icon(color="blue", icon="play", prefix="fa")).add_to(m)
        # Any waypoints between start and destination get their own numbered marker
        for i, pt in enumerate(points[1:-1], start=1):
            folium.Marker(pt, tooltip=f"Stop {i}",
                icon=folium.Icon(color="orange", icon="map-pin", prefix="fa")).add_to(m)
        if len(points) > 1:
            folium.Marker(points[-1], tooltip="Destination",
                icon=folium.Icon(color="red", icon="flag", prefix="fa")).add_to(m)

    return m._repr_html_()

@app.route("/")
def index():
    """Serve the standalone index.html directly (no templates/ folder, no Jinja) — but
    with the CSS/JS version placeholders swapped for each file's real last-modified time,
    so the browser is forced to fetch fresh static files instead of a stale cached copy
    whenever style.css or translations.js actually change."""
    with open(os.path.join(app.root_path, "index.html"), encoding="utf-8") as f:
        html = f.read()
    css_mtime = int(os.path.getmtime(os.path.join(app.root_path, "static", "style.css")))
    js_mtime  = int(os.path.getmtime(os.path.join(app.root_path, "static", "translations.js")))
    html = html.replace("{{CSS_VERSION}}", str(css_mtime)).replace("{{JS_VERSION}}", str(js_mtime))
    return html

@app.route("/map")
def map_default():
    """Return the default world-view map HTML (used on initial page load and Clear)."""
    return build_map()

@app.route("/suggest")
def suggest():
    """Return up to 5 clean address suggestions for autocomplete dropdown."""
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify([])
    try:
        r = requests.get(NOM, params={"q": expand(q), "format": "json", "limit": 5, "addressdetails": 1}, headers=HDR, timeout=10)
        seen, out = set(), []
        for item in r.json():
            a = item.get("address", {})
            parts = []
            # Build a clean label: house number + road, city, state
            if a.get("house_number") and a.get("road"):
                parts.append(f"{a['house_number']} {a['road']}")
            elif a.get("road"):
                parts.append(a["road"])
            city = next((a.get(k) for k in ("city","town","suburb","city_district","neighbourhood","village","municipality") if a.get(k)), None)
            if city: parts.append(city)
            if a.get("state"): parts.append(a["state"])
            if a.get("country_code","").upper() != "US" and a.get("country"): parts.append(a["country"])
            label = ", ".join(parts) or item["display_name"]
            # Deduplicate suggestions
            if label not in seen:
                seen.add(label)
                out.append({"label": label, "lat": item["lat"], "lon": item["lon"]})
        return jsonify(out)
    except Exception:
        return jsonify([])

def resolve_point(text, coords):
    """Resolve a point from pre-fetched autocomplete coords if available, else geocode the raw text."""
    if coords:
        return (float(coords["lat"]), float(coords["lon"]))
    text = (text or "").strip()
    return geocode(text) if text else None

@app.route("/route", methods=["POST"])
def route():
    """Receive origin/destination/mode/stops, return map HTML + route stats."""
    d    = request.get_json()
    mode = d.get("mode", "driving")
    if mode not in PROFILES:
        return jsonify({"error": "Invalid travel mode."}), 400

    start = resolve_point(d.get("origin"), d.get("origin_coords"))
    if not start: return jsonify({"error": f"Could not find: '{d.get('origin')}'"}), 404
    end   = resolve_point(d.get("destination"), d.get("destination_coords"))
    if not end:   return jsonify({"error": f"Could not find: '{d.get('destination')}'"}), 404

    # Resolve any in-between stops, in the order the user entered them
    stop_points = []
    for i, stop in enumerate(d.get("stops") or [], start=1):
        text, coords = stop.get("text"), stop.get("coords")
        if not (text or "").strip() and not coords:
            continue  # skip a blank/unused stop row
        pt = resolve_point(text, coords)
        if not pt:
            return jsonify({"error": f"Could not find stop {i}: '{text}'"}), 404
        stop_points.append(pt)

    points = [start] + stop_points + [end]
    routes = get_routes(points, mode)
    if not routes: return jsonify({"error": "Could not calculate route."}), 500

    # Legal Distance: miles/minutes from where the route exits the origin city's
    # boundary to the destination. Boundary is looked up once per request (not per
    # route) since every route shares the same origin.
    city_boundary = get_city_boundary(*start)
    for rt in routes:
        legal_km, legal_min = compute_legal_distance(rt, city_boundary)
        rt["legal_km"] = legal_km
        rt["legal_mi"] = round(legal_km * 0.621371, 2)
        rt["legal_min"] = legal_min

    best = routes[0]
    return jsonify({
        "map_html":     build_map(points, routes, mode),
        "distance_mi":  round(best["dist"] * 0.621371, 2),  # Convert km to miles
        "distance_km":  best["dist"],
        "duration_min": best["mins"],
        "legal_mi":     best["legal_mi"],
        "legal_km":     best["legal_km"],
        "legal_min":    best["legal_min"],
        "alt_count":    len(routes) - 1,
        # Stats for every route option, in the same order as the lines drawn on the map,
        # so the sidebar route list can show/switch between them without another request.
        "routes": [
            {
                "distance_mi":  round(rt["dist"] * 0.621371, 2),
                "distance_km":  rt["dist"],
                "duration_min": rt["mins"],
                "legal_mi":     rt["legal_mi"],
                "legal_km":     rt["legal_km"],
                "legal_min":    rt["legal_min"],
                "road":         rt.get("road"),   # main road for a short "via <road>" label
                "rank":         rank,             # "fast" / "slow" / "mid" / null, for coloring
            }
            for rt, rank in zip(routes, rank_times(routes))
        ],
    })

if __name__ == "__main__":
    app.run(debug=True)
