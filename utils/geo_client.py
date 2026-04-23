import math
import os
from typing import Any

import requests

_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_GOOGLE_PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
_OSRM_URL = "http://router.project-osrm.org/route/v1/driving"
_GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

_TIMEOUT = 10
_geocode_cache: dict[str, tuple[float, float]] = {}

_CATEGORY_PLACES: dict[str, str] = {
    "restaurante": "restaurant",
    "lanchonete": "meal_takeaway",
    "cafe": "cafe",
    "posto": "gas_station",
    "posto de gasolina": "gas_station",
    "hotel": "lodging",
    "pousada": "lodging",
    "farmacia": "pharmacy",
    "parque": "park",
    "museu": "museum",
    "praia": "natural_feature",
    "mirante": "natural_feature",
    "visual panoramico": "natural_feature",
}


def geocode(location: str) -> tuple[float, float]:
    key = location.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]
    resp = requests.get(
        _GOOGLE_GEOCODE_URL,
        params={"address": location, "key": _GOOGLE_MAPS_KEY},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        raise ValueError(f"Localização não encontrada: {location!r}")
    loc = data["results"][0]["geometry"]["location"]
    coords = (float(loc["lat"]), float(loc["lng"]))
    _geocode_cache[key] = coords
    return coords


def get_route(
    origin: tuple[float, float],
    destination: tuple[float, float],
    waypoints: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    coords = [origin] + (waypoints or []) + [destination]
    coord_str = ";".join(f"{lon},{lat}" for lat, lon in coords)
    resp = requests.get(
        f"{_OSRM_URL}/{coord_str}",
        params={"overview": "full", "geometries": "geojson", "steps": "false"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("OSRM não encontrou rota entre os pontos informados.")
    route = data["routes"][0]
    total_km = round(route["distance"] / 1000, 1)
    total_minutes = round(route["duration"] / 60)
    raw_coords: list[list[float]] = route["geometry"]["coordinates"]
    return {
        "total_km": total_km,
        "total_minutes": total_minutes,
        "coordinates": [(lat, lon) for lon, lat in raw_coords],
    }


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def sample_waypoints(
    coordinates: list[tuple[float, float]],
    total_km: float,
    interval_km: float,
    total_minutes: int = 0,
) -> list[dict[str, Any]]:
    if not coordinates or interval_km <= 0:
        return []
    result: list[dict[str, Any]] = []
    accumulated = 0.0
    next_stop = interval_km
    for i in range(1, len(coordinates)):
        accumulated += _haversine_km(coordinates[i - 1], coordinates[i])
        if accumulated >= next_stop:
            eta = round(accumulated / total_km * total_minutes) if total_km > 0 and total_minutes > 0 else 0
            result.append({
                "lat": coordinates[i][0],
                "lon": coordinates[i][1],
                "km_from_origin": round(accumulated, 1),
                "eta_minutes": eta,
            })
            next_stop += interval_km
    return result


def reverse_geocode(lat: float, lon: float) -> str:
    try:
        resp = requests.get(
            _GOOGLE_GEOCODE_URL,
            params={"latlng": f"{lat},{lon}", "key": _GOOGLE_MAPS_KEY, "result_type": "locality|administrative_area_level_2", "language": "pt-BR"},
            timeout=_TIMEOUT,
        )
        data = resp.json()
        if data.get("status") == "OK" and data.get("results"):
            return data["results"][0].get("formatted_address", "").split(",")[0].strip()
    except Exception:
        pass
    return f"{lat:.4f}, {lon:.4f}"


def get_pois(
    lat: float,
    lon: float,
    radius_m: int,
    categories: list[str],
) -> list[dict[str, Any]]:
    pois: list[dict[str, Any]] = []
    seen: set[str] = set()
    types = list({_CATEGORY_PLACES.get(c.lower().strip()) for c in categories if _CATEGORY_PLACES.get(c.lower().strip())})
    if not types:
        types = ["gas_station"]
    for place_type in types[:3]:
        try:
            resp = requests.get(
                _GOOGLE_PLACES_URL,
                params={"location": f"{lat},{lon}", "radius": radius_m, "type": place_type, "key": _GOOGLE_MAPS_KEY, "language": "pt-BR"},
                timeout=_TIMEOUT,
            )
            if not resp.ok:
                continue
            for r in resp.json().get("results", [])[:3]:
                name = r.get("name", "")
                if not name or name in seen:
                    continue
                seen.add(name)
                loc = r.get("geometry", {}).get("location", {})
                pois.append({"name": name, "type": place_type, "lat": loc.get("lat"), "lon": loc.get("lng")})
        except Exception:
            continue
    return pois


def detour_km(
    route_coords: list[tuple[float, float]],
    point: tuple[float, float],
) -> float:
    if not route_coords:
        return 999.0
    return min(_haversine_km(c, point) for c in route_coords)
