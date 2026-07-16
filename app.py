from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
import folium, requests, re, os

# Load environment variables from .env file (keeps API keys out of code)
load_dotenv()

# app.root_path defaults to the folder this file lives in, so index.html
# and static/ are found no matter what directory you launch python from.
app = Flask(__name__, static_folder="static", static_url_path="/static")

# ── API endpoints ──
NOM     = "https://nominatim.openstreetmap.org/search"   # Address search
OSRM    = "https://router.project-osrm.org/route/v1"     # Driving routes
ORS     = "https://api.openrouteservice.org/v2/directions" # Walking/cycling routes
ORS_KEY = os.getenv("ORS_KEY")                            # Loaded from .env
HDR     = {"User-Agent": "DestinationMapApp/1.0"}

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

def get_routes_driving(s, e):
    """Fetch up to 3 driving route alternatives from OSRM."""
    r = requests.get(
        f"{OSRM}/driving/{s[1]},{s[0]};{e[1]},{e[0]}",
        params={"overview": "full", "geometries": "geojson", "alternatives": "true", "steps": "false"},
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
        }
        for rt in data["routes"]
    ]

def get_routes_ors(s, e, mode):
    """Fetch a walking or cycling route, plus a couple of longer alternatives, from OpenRouteService."""
    r = requests.post(
        f"{ORS}/{ORS_PROFILE[mode]}/geojson",
        headers={"Authorization": ORS_KEY, "Content-Type": "application/json"},
        json={
            "coordinates": [[s[1], s[0]], [e[1], e[0]]],
            # Ask for up to 2 extra alternatives, even if somewhat longer than the best route
            "alternative_routes": {"target_count": 3, "weight_factor": 1.6, "share_factor": 0.6},
        },
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
        }
        for ft in data["features"]
    ]
    # Fastest route first, alternatives after (matches how OSRM's driving routes are ordered)
    routes.sort(key=lambda rt: rt["mins"])
    return routes

def get_routes(s, e, mode):
    """Route dispatcher — driving uses OSRM, walking/cycling use ORS."""
    return get_routes_driving(s, e) if mode == "driving" else get_routes_ors(s, e, mode)

def build_map(start=None, end=None, routes=None, mode="driving"):
    """Build and return a Folium map as an HTML string."""
    m = folium.Map(location=start or [20, 0], zoom_start=13 if start else 2)

    # ESRI World Street Map — shows labels, roads, and terrain at all zoom levels
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles &copy; Esri &mdash; Source: Esri, HERE, DeLorme, USGS, Intermap, NRCAN, METI, TomTom",
        name="ESRI Street",
    ).add_to(m)

    # Slightly desaturate tiles so route lines stand out clearly
    m.get_root().html.add_child(folium.Element(
        "<style>.leaflet-tile { filter: saturate(0.75) brightness(1.02); }</style>"
    ))

    if routes:
        color, alt_color = PROFILES[mode]
        all_pts = []

        # Draw alternative routes first (behind the best route)
        for rt in routes[1:]:
            folium.PolyLine(rt["coords"], color=alt_color, weight=4, opacity=0.6,
                tooltip=f"Alt: {round(rt['dist'] * 0.621371, 2)} mi, {rt['mins']} min").add_to(m)
            all_pts += rt["coords"]

        # Draw best route on top
        best = routes[0]
        folium.PolyLine(best["coords"], color=color, weight=6, opacity=0.9,
            tooltip=f"Best: {round(best['dist'] * 0.621371, 2)} mi, {best['mins']} min").add_to(m)
        all_pts += best["coords"]

        # Auto-fit map bounds to show the full route
        m.fit_bounds([[min(p[0] for p in all_pts), min(p[1] for p in all_pts)],
                      [max(p[0] for p in all_pts), max(p[1] for p in all_pts)]])

    # Add start and end markers
    if start:
        folium.Marker(start, tooltip="Start",
            icon=folium.Icon(color="blue", icon="play", prefix="fa")).add_to(m)
    if end:
        folium.Marker(end, tooltip="Destination",
            icon=folium.Icon(color="red", icon="flag", prefix="fa")).add_to(m)

    return m._repr_html_()

@app.route("/")
def index():
    """Serve the standalone index.html directly (no templates/ folder, no Jinja)."""
    return send_from_directory(app.root_path, "index.html")

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

@app.route("/route", methods=["POST"])
def route():
    """Receive origin/destination/mode, return map HTML + route stats."""
    d    = request.get_json()
    mode = d.get("mode", "driving")
    if mode not in PROFILES:
        return jsonify({"error": "Invalid travel mode."}), 400

    def resolve(coord_key, text_key):
        """Use pre-resolved coords from autocomplete if available, else geocode."""
        c = d.get(coord_key)
        return (float(c["lat"]), float(c["lon"])) if c else geocode(d.get(text_key, "").strip())

    start = resolve("origin_coords", "origin")
    if not start: return jsonify({"error": f"Could not find: '{d.get('origin')}'"}), 404
    end   = resolve("destination_coords", "destination")
    if not end:   return jsonify({"error": f"Could not find: '{d.get('destination')}'"}), 404

    routes = get_routes(start, end, mode)
    if not routes: return jsonify({"error": "Could not calculate route."}), 500

    best = routes[0]
    return jsonify({
        "map_html":     build_map(start, end, routes, mode),
        "distance_mi":  round(best["dist"] * 0.621371, 2),  # Convert km to miles
        "distance_km":  best["dist"],
        "duration_min": best["mins"],
        "alt_count":    len(routes) - 1,
    })

if __name__ == "__main__":
    app.run(debug=True)
