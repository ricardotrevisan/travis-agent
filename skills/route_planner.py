import asyncio
import base64
from datetime import datetime, timezone
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill
from utils import geo_client

import json
import os
import redis as _redis_lib

_FUEL_SEARCH_RADIUS_M = 2000
_FUEL_MAX_DETOUR_M = 3000

def _get_redis():
    return _redis_lib.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )

_ROUTE_TTL_SECONDS = 30 * 60

def _save_last_route(sender: str, data: dict) -> None:
    try:
        _get_redis().set(f"agent:route:last:{sender}", json.dumps(data), ex=_ROUTE_TTL_SECONDS)
    except Exception as e:
        print(f"[route_planner] redis save failed: {e}")

def _load_last_route(sender: str) -> dict:
    try:
        raw = _get_redis().get(f"agent:route:last:{sender}")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


# ── Perfil de busca de POIs ───────────────────────────────────────────────────

# Raio de busca por contexto de trecho (velocidade média como proxy)
def _poi_search_radius_m(avg_speed_kmh: float) -> int:
    if avg_speed_kmh > 90:
        return 8_000   # rodovia rápida
    if avg_speed_kmh >= 50:
        return 15_000  # trecho misto / estrada secundária
    return 5_000       # perímetro urbano

# Critérios de qualidade por categoria
_POI_QUALITY: dict[str, dict] = {
    "natural_feature": {"min_rating": 4.4, "min_reviews": 50},
    "restaurant":      {"min_rating": 4.4, "min_reviews": 50},
    "cafe":            {"min_rating": 4.4, "min_reviews": 50},
    "museum":          {"min_rating": 4.0, "min_reviews": 20},
    "amusement_park":  {"min_rating": 4.4, "min_reviews": 50},
    "park":            {"min_rating": 4.4, "min_reviews": 50},
}

# Desvio máximo por tipo de parada (km)
_POI_DETOUR_LIMITS: dict[str, float] = {
    "natural_feature": 12.0,
    "restaurant":       6.0,
    "cafe":             6.0,
    "museum":           6.0,
    "amusement_park":  12.0,
    "park":            12.0,
}

# Categorias fora do escopo do perfil moto solo exploratório
_POI_BLOCKED_TYPES = {"lodging", "gas_station", "pharmacy", "shopping_mall", "meal_takeaway"}

_POI_SAMPLE_INTERVAL_KM = 20.0


def _search_pois_along_route(
    coordinates: list[tuple[float, float]],
    total_km: float,
    total_minutes: int,
    categories: list[str],
    sample_interval_km: float = _POI_SAMPLE_INTERVAL_KM,
) -> list[dict]:
    """Varre a polyline a cada sample_interval_km e coleta POIs candidatos."""
    seen_place_ids: set[str] = set()
    candidates: list[dict] = []

    km = sample_interval_km
    while km < total_km:
        point = geo_client.point_at_km(coordinates, km, total_minutes, total_km)
        if not point:
            km += sample_interval_km
            continue

        # velocidade média do segmento como proxy de contexto
        elapsed_minutes = point["eta_minutes"] or 1
        avg_speed = (km / elapsed_minutes * 60) if elapsed_minutes > 0 else 80.0
        radius_m = _poi_search_radius_m(avg_speed)

        pois = geo_client.get_pois(point["lat"], point["lon"], radius_m, categories)
        for poi in pois:
            pid = poi.get("place_id", "")
            if not pid or pid in seen_place_ids:
                continue
            place_type = poi.get("type", "")
            if place_type in _POI_BLOCKED_TYPES:
                continue

            rating = poi.get("rating") or 0.0
            reviews = poi.get("user_ratings_total") or 0
            quality = _POI_QUALITY.get(place_type, {"min_rating": 4.4, "min_reviews": 50})
            if rating < quality["min_rating"] or reviews < quality["min_reviews"]:
                continue

            detour = geo_client.detour_km(coordinates, (poi["lat"], poi["lon"]))
            max_detour = _POI_DETOUR_LIMITS.get(place_type, 12.0)
            if detour > max_detour:
                continue

            closest_km = _closest_km((poi["lat"], poi["lon"]), coordinates, total_km)
            seen_place_ids.add(pid)
            candidates.append({
                "place_id": pid,
                "name": poi["name"],
                "type": place_type,
                "lat": poi["lat"],
                "lon": poi["lon"],
                "km_from_origin": closest_km,
                "eta_minutes": _eta(closest_km, total_km, total_minutes),
                "detour_km": round(detour, 1),
                "rating": rating,
                "user_ratings_total": reviews,
            })

        km += sample_interval_km

    candidates.sort(key=lambda c: c["km_from_origin"])
    return candidates


class RoutePlannerSkill(BaseSkill):
    name = "route_planner"
    description = (
        "Planejar rotas de moto entre dois endereços ou cidades, com paradas de descanso e "
        "postos de abastecimento. Usar quando o usuário pedir rota, trajeto ou itinerário. "
        "A viagem é SEMPRE de moto — não existe escolha de meio de transporte. Nunca perguntar "
        "sobre veículo, modo ou tipo de transporte: assuma moto e calcule direto. "
        "action=plan (padrão): calcular rota. "
        "action=gpx: usar SOMENTE quando o usuário pedir explicitamente o arquivo GPX. "
        "action=poi_search: buscar pontos de interesse ao longo da rota já calculada — "
        "usar quando o usuário perguntar o que tem no caminho, quiser explorar paradas "
        "ou pedir sugestões de lugares. Não requer nova origem/destino. "
        "action=add_pois: inserir POIs escolhidos pelo usuário na rota existente — "
        "usar quando o usuário confirmar quais pontos quer adicionar após o poi_search. "
        "Para add_pois, passar OBRIGATORIAMENTE o campo 'indices' com lista de inteiros "
        "(números que o usuário escolheu da lista, ex: [3, 7, 12]). "
        "Nunca inventar outros nomes de campo (poi_indices, pois_to_add, pois, ids). Apenas 'indices'. "
        "fixed_waypoints: lista ordenada de paradas obrigatórias entre origem e destino."
    )
    enabled = True
    planner_visible = True

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        action = (args.get("action") or "plan").strip().lower()
        if action == "gpx":
            return self._run_gpx(ctx, args)
        if action == "poi_search":
            return self._run_poi_search(ctx, args)
        if action == "add_pois":
            return self._run_add_pois(ctx, args)
        return self._run_plan(ctx, args)

    # ── action=gpx ────────────────────────────────────────────────────────────

    def _run_gpx(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        cached = _load_last_route(ctx.sender)
        stops = cached.get("stops") or args.get("stops") or []
        origin_str = cached.get("origin") or (args.get("origin") or "").strip()
        destination_str = cached.get("destination") or (args.get("destination") or "").strip()
        coordinates = cached.get("coordinates") or []
        if not stops or not origin_str or not destination_str:
            return SkillResult(ok=False, user_visible_text="Não encontrei uma rota recente. Peça a rota primeiro e depois solicite o GPX.")

        gpx_bytes = _build_gpx(origin_str, destination_str, stops, coordinates)
        link = _upload_gpx_to_drive(gpx_bytes, origin_str, destination_str)
        if not link:
            return SkillResult(ok=False, user_visible_text="Não consegui subir o GPX para o Drive. Tente novamente.")

        return SkillResult(
            ok=True,
            output={"gpx_link": link},
            user_visible_text=f"📎 GPX da rota {origin_str} → {destination_str} pronto:\n{link}",
        )

    # ── action=plan ───────────────────────────────────────────────────────────

    def _run_plan(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        origin_str = (args.get("origin") or args.get("from") or args.get("start") or "").strip()
        destination_str = (args.get("destination") or args.get("to") or args.get("dest") or "").strip()
        if not origin_str or not destination_str:
            return SkillResult(
                ok=False,
                user_visible_text="Preciso de origem e destino para planejar a rota. Exemplo: 'rota de São Paulo para Florianópolis'.",
            )

        stop_interval_hours: float | None = _to_float(args.get("stop_interval_hours"))
        stop_interval_km: float = _to_float(args.get("stop_interval_km")) or 150.0
        max_stops: int = int(args.get("max_stops") or 4)
        raw_waypoints = args.get("fixed_waypoints") or []
        if isinstance(raw_waypoints, str):
            raw_waypoints = [raw_waypoints]
        fixed_waypoint_strs: list[str] = [w for w in raw_waypoints if isinstance(w, str) and w.strip()]
        fuel_args: dict = args.get("fuel") or {}
        if not fuel_args:
            fuel_args = {"enabled": True, "max_interval_km": 180, "tank_km_remaining": 200}

        # 1. Geocodificar
        try:
            origin_coords = geo_client.geocode(origin_str)
        except Exception:
            return SkillResult(ok=False, user_visible_text=f"Não consegui localizar a origem '{origin_str}'. Tente um nome mais específico.")

        try:
            destination_coords = geo_client.geocode(destination_str)
        except Exception:
            return SkillResult(ok=False, user_visible_text=f"Não consegui localizar o destino '{destination_str}'. Tente um nome mais específico.")

        fixed_waypoint_coords: list[tuple[float, float]] = []
        for wp in fixed_waypoint_strs:
            try:
                fixed_waypoint_coords.append(geo_client.geocode(wp))
            except Exception:
                return SkillResult(
                    ok=False,
                    user_visible_text=f"Não consegui localizar a parada obrigatória '{wp}'. Use um nome mais específico (cidade + estado) e tente de novo.",
                )

        # 2. Calcular rota
        try:
            route = geo_client.get_route(origin_coords, destination_coords, fixed_waypoint_coords)
        except Exception:
            return SkillResult(ok=False, user_visible_text="Não consegui calcular a rota. Verifique os nomes das cidades e tente novamente.")

        total_km: float = route["total_km"]
        total_minutes: int = route["total_minutes"]
        coordinates: list[tuple[float, float]] = route["coordinates"]

        # 3. Intervalo de parada
        if stop_interval_hours is not None and stop_interval_hours > 0:
            speed_kmh = total_km / (total_minutes / 60) if total_minutes > 0 else 80.0
            effective_interval_km = stop_interval_hours * speed_kmh
        else:
            effective_interval_km = stop_interval_km

        # 4. Waypoints fixos
        stops: list[dict[str, Any]] = []
        for wp_str, coords in zip(fixed_waypoint_strs, fixed_waypoint_coords):
            km = _closest_km(coords, coordinates, total_km)
            stops.append({
                "type": "waypoint_fixed",
                "name": wp_str,
                "lat": coords[0],
                "lon": coords[1],
                "km_from_origin": km,
                "eta_minutes": _eta(km, total_km, total_minutes),
                "detour_km": None,
                "pois": [],
            })

        # 5. Abastecimento
        fuel_stops: list[dict[str, Any]] = []
        fuel_gaps: list[float] = []
        if fuel_args.get("enabled"):
            fuel_interval = float(fuel_args.get("max_interval_km") or 200)
            tank_remaining = float(fuel_args.get("tank_km_remaining") or fuel_interval)
            fuel_brands = fuel_args.get("preferred_brands") or []
            fuel_categories = ["posto de gasolina"] + [b.lower() for b in fuel_brands]
            fuel_limit_km = min(fuel_interval, tank_remaining * 0.85)

            last_refuel_km = 0.0
            while last_refuel_km + fuel_limit_km < total_km:
                limit_km = last_refuel_km + fuel_limit_km
                if geo_client.point_at_km(coordinates, limit_km, total_minutes, total_km) is None:
                    break

                best_detour, best, best_point = None, None, None
                scan_km = limit_km
                while scan_km > last_refuel_km + 10:
                    cand = geo_client.point_at_km(coordinates, scan_km, total_minutes, total_km)
                    if cand:
                        pois = geo_client.get_pois(cand["lat"], cand["lon"], _FUEL_SEARCH_RADIUS_M, fuel_categories)
                        scored = sorted(
                            [(geo_client.driving_distance_m((cand["lat"], cand["lon"]), (p["lat"], p["lon"])), p) for p in pois],
                            key=lambda x: x[0],
                        )
                        valid = [(det, p) for det, p in scored if det <= _FUEL_MAX_DETOUR_M]
                        branded = [(det, p) for det, p in valid if p.get("has_brand")]
                        if branded:
                            best_detour, best = branded[0]
                            best_point = cand
                            break
                        for det, p in valid:
                            details = geo_client.get_place_details(p.get("place_id", ""))
                            if any(term in details for term in geo_client._FUEL_WEBSITE_ALLOWLIST):
                                best_detour, best = det, p
                                best_point = cand
                                break
                        if best:
                            break
                    scan_km -= 5

                if best:
                    fuel_stops.append({
                        "type": "fuel",
                        "name": best["name"],
                        "lat": best["lat"],
                        "lon": best["lon"],
                        "km_from_origin": best_point["km_from_origin"],
                        "eta_minutes": best_point["eta_minutes"],
                        "detour_km": round(best_detour / 1000, 1),
                        "pois": [],
                    })
                    last_refuel_km = best_point["km_from_origin"]
                else:
                    fuel_gaps.append(limit_km)
                    last_refuel_km = limit_km

        # 6. Paradas de descanso — só localidade, sem POIs
        rest_points = geo_client.sample_waypoints(coordinates, total_km, effective_interval_km, total_minutes)
        rest_stops: list[dict[str, Any]] = []
        for rp in rest_points[:max_stops]:
            location_name = geo_client.reverse_geocode(rp["lat"], rp["lon"])
            rest_stops.append({
                "type": "rest",
                "name": location_name,
                "lat": rp["lat"],
                "lon": rp["lon"],
                "km_from_origin": rp["km_from_origin"],
                "eta_minutes": rp["eta_minutes"],
                "detour_km": None,
                "pois": [],
            })

        all_stops = sorted(stops + fuel_stops + rest_stops, key=lambda s: s["km_from_origin"])

        last_fuel_km = max((s["km_from_origin"] for s in fuel_stops), default=None)
        display = _format_whatsapp(origin_str, destination_str, total_km, total_minutes, all_stops, fuel_gaps, last_fuel_km)

        result = SkillResult(
            ok=True,
            output={
                "route": {"total_km": total_km, "total_minutes": total_minutes},
                "stops": all_stops,
                "origin": origin_str,
                "destination": destination_str,
                "total_km": total_km,
                "estimated_hours": round(total_minutes / 60, 1),
                "fuel_stops_count": len(fuel_stops),
                "fuel_gaps_km": fuel_gaps,
            },
            user_visible_text=display,
        )
        _save_last_route(ctx.sender, {
            "stops": all_stops,
            "origin": origin_str,
            "destination": destination_str,
            "coordinates": coordinates,
            "total_km": total_km,
            "total_minutes": total_minutes,
        })
        return result

    # ── action=poi_search ─────────────────────────────────────────────────────

    def _run_poi_search(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        cached = _load_last_route(ctx.sender)
        if not cached:
            return SkillResult(
                ok=False,
                user_visible_text="Não encontrei uma rota recente. Calcule a rota primeiro e depois peça os pontos de interesse.",
            )

        coordinates = cached.get("coordinates") or []
        total_km = cached.get("total_km") or 0.0
        total_minutes = cached.get("total_minutes") or 0
        origin_str = cached.get("origin", "")
        destination_str = cached.get("destination", "")

        raw_categories = args.get("categories") or ["natureza", "gastronomia regional", "cultura", "adrenalina"]
        if isinstance(raw_categories, str):
            raw_categories = [raw_categories]
        sample_interval_km = float(args.get("sample_interval_km") or _POI_SAMPLE_INTERVAL_KM)

        candidates = _search_pois_along_route(
            coordinates, total_km, total_minutes, raw_categories, sample_interval_km
        )

        if not candidates:
            return SkillResult(
                ok=True,
                output={"candidates": []},
                user_visible_text="Não encontrei pontos de interesse com nota suficiente ao longo desta rota.",
            )

        # persiste candidatos no Redis para que add_pois resolva por índice
        _save_last_route(ctx.sender, {**cached, "poi_candidates": candidates})

        display = _format_poi_candidates(candidates, origin_str, destination_str)
        return SkillResult(
            ok=True,
            output={"candidates": candidates},
            user_visible_text=display,
        )

    # ── action=add_pois ───────────────────────────────────────────────────────

    def _run_add_pois(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        cached = _load_last_route(ctx.sender)
        if not cached:
            return SkillResult(
                ok=False,
                user_visible_text="Não encontrei uma rota recente. Calcule a rota primeiro.",
            )

        coordinates = cached.get("coordinates") or []
        total_km = cached.get("total_km") or 0.0
        total_minutes = cached.get("total_minutes") or 0
        origin_str = cached.get("origin", "")
        destination_str = cached.get("destination", "")
        existing_stops: list[dict] = cached.get("stops") or []
        poi_candidates: list[dict] = cached.get("poi_candidates") or []

        # coleta índices de todas as variações de campo que o planner pode inventar
        raw_indices: list[int] = []
        for field in ("indices", "poi_indices"):
            val = args.get(field)
            if val:
                if isinstance(val, list):
                    raw_indices += [int(v) for v in val if str(v).isdigit() or isinstance(v, int)]
                break

        # coleta dicts de todas as variações de campo com objetos
        raw_pois_dicts: list[dict] = []
        for field in ("pois", "pois_to_add"):
            val = args.get(field)
            if val and isinstance(val, list):
                raw_pois_dicts = val
                break

        # extrai índices embutidos nos dicts ({id: N} ou {index: N})
        for item in raw_pois_dicts:
            for key in ("id", "index", "number"):
                v = item.get(key)
                if v is not None:
                    try:
                        raw_indices.append(int(v))
                    except (ValueError, TypeError):
                        pass
                    break

        # resolve índices (1-based) nos candidatos salvos
        pois_to_add: list[dict] = []
        seen_ids: set[str] = set()
        for idx in raw_indices:
            if poi_candidates:
                try:
                    resolved = poi_candidates[idx - 1]
                    pid = resolved.get("place_id", str(idx))
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        pois_to_add.append(resolved)
                except IndexError:
                    pass

        # fallback: dicts com lat/lon direto (sem índice resolúvel)
        for item in raw_pois_dicts:
            has_index = any(item.get(k) is not None for k in ("id", "index", "number"))
            if not has_index and item.get("lat") is not None and item.get("lon") is not None:
                pid = item.get("place_id", item.get("name", ""))
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    pois_to_add.append(item)

        omitted: list[str] = []
        new_stops: list[dict] = []

        for poi in pois_to_add:
            lat, lon = poi.get("lat"), poi.get("lon")
            if lat is None or lon is None:
                omitted.append(poi.get("name", "POI sem coordenadas"))
                continue

            place_type = poi.get("type", "natural_feature")
            max_detour = float(poi.get("max_detour_km") or _POI_DETOUR_LIMITS.get(place_type, 12.0))
            detour = geo_client.detour_km(coordinates, (lat, lon))
            if detour > max_detour:
                omitted.append(poi.get("name", "POI"))
                continue

            km = _closest_km((lat, lon), coordinates, total_km)
            new_stops.append({
                "type": "poi_fixed",
                "name": poi.get("name", "Ponto de interesse"),
                "lat": lat,
                "lon": lon,
                "km_from_origin": km,
                "eta_minutes": _eta(km, total_km, total_minutes),
                "detour_km": round(detour, 1),
                "rating": poi.get("rating"),
                "user_ratings_total": poi.get("user_ratings_total"),
                "pois": [],
            })

        all_stops = sorted(existing_stops + new_stops, key=lambda s: s["km_from_origin"])

        fuel_stops = [s for s in all_stops if s["type"] == "fuel"]
        last_fuel_km = max((s["km_from_origin"] for s in fuel_stops), default=None)
        display = _format_whatsapp(origin_str, destination_str, total_km, total_minutes, all_stops, [], last_fuel_km)

        if omitted:
            display += f"\n\n⚠️ Não incluí: {', '.join(omitted)} (desvio excede o limite configurado)."

        _save_last_route(ctx.sender, {
            "stops": all_stops,
            "origin": origin_str,
            "destination": destination_str,
            "coordinates": coordinates,
            "total_km": total_km,
            "total_minutes": total_minutes,
            "poi_candidates": poi_candidates,
        })

        return SkillResult(
            ok=True,
            output={
                "stops": all_stops,
                "pois_added": len(new_stops),
                "pois_omitted": omitted,
            },
            user_visible_text=display,
        )


# ── GPX ──────────────────────────────────────────────────────────────────────

def _build_gpx(origin: str, destination: str, stops: list[dict], coordinates: list = None) -> bytes:
    gpx = Element("gpx", {"version": "1.1", "creator": "travis-agent", "xmlns": "http://www.topografix.com/GPX/1/1"})
    meta = SubElement(gpx, "metadata")
    SubElement(meta, "name").text = f"{origin} → {destination}"
    SubElement(meta, "time").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if coordinates:
        trk = SubElement(gpx, "trk")
        SubElement(trk, "name").text = f"{origin} → {destination}"
        trkseg = SubElement(trk, "trkseg")
        for lat, lon in coordinates:
            SubElement(trkseg, "trkpt", {"lat": str(lat), "lon": str(lon)})

    for stop in stops:
        wpt = SubElement(gpx, "wpt", {"lat": str(stop["lat"]), "lon": str(stop["lon"])})
        label = _STOP_LABELS.get(stop["type"], "Parada")
        SubElement(wpt, "name").text = stop["name"]
        SubElement(wpt, "desc").text = f"{label} — km {stop['km_from_origin']:.0f}"
        SubElement(wpt, "sym").text = _GPX_SYMS.get(stop["type"], "Waypoint")

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(gpx, encoding="unicode").encode("utf-8")


def _upload_gpx_to_drive(gpx_bytes: bytes, origin: str, destination: str) -> str | None:
    import os, re
    from skills.mcp_tools import _run_async, _call_tool
    from mcp.types import CallToolResult

    email = os.getenv("MCP_GMAIL_USER_EMAIL", "")
    filename = f"rota_{origin.split(',')[0].strip()}_{destination.split(',')[0].strip()}.gpx".replace(" ", "_")

    def _text(result) -> str:
        if isinstance(result, CallToolResult) and result.content:
            return result.content[0].text or ""
        return ""

    try:
        result = _run_async(_call_tool("create_drive_file", {
            "user_google_email": email,
            "file_name": filename,
            "mime_type": "application/gpx+xml",
            "content": gpx_bytes.decode("utf-8"),
        }))
        text = _text(result)
        print(f"[route_planner] create_drive_file: {text[:200]}")

        m = re.search(r"/d/([a-zA-Z0-9_-]+)", text)
        if not m:
            return None
        file_id = m.group(1)

        _run_async(_call_tool("set_drive_file_permissions", {
            "user_google_email": email,
            "file_id": file_id,
            "role": "reader",
            "type": "anyone",
        }))

        return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    except Exception as exc:
        print(f"[route_planner] gpx upload failed: {exc}")
        return None


# ── helpers ──────────────────────────────────────────────────────────────────

def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _closest_km(
    point: tuple[float, float],
    coordinates: list[tuple[float, float]],
    total_km: float,
) -> float:
    if not coordinates:
        return 0.0
    idx = min(range(len(coordinates)), key=lambda i: geo_client._haversine_km(coordinates[i], point))
    return round(total_km * idx / max(len(coordinates) - 1, 1), 1)


def _eta(km_from_origin: float, total_km: float, total_minutes: int) -> int:
    if total_km <= 0:
        return 0
    return round(km_from_origin / total_km * total_minutes)


_GPX_SYMS = {
    "waypoint_fixed": "Flag, Blue",
    "poi_fixed": "Star",
    "rest": "Scenic Area",
    "fuel": "Gas Station",
}

_STOP_ICONS = {
    "waypoint_fixed": "📌",
    "poi_fixed": "⭐",
    "rest": "🛑",
    "fuel": "⛽",
}

_STOP_LABELS = {
    "waypoint_fixed": "Waypoint fixo",
    "poi_fixed": "Ponto de interesse",
    "rest": "Parada sugerida",
    "fuel": "Abastecimento",
}

_POI_TYPE_LABEL = {
    "natural_feature": "🌿 Natureza",
    "restaurant":      "🍽️ Gastronomia",
    "cafe":            "☕ Gastronomia",
    "museum":          "🏛️ Cultura",
    "amusement_park":  "🏁 Adrenalina",
    "park":            "🌳 Natureza",
}

_POI_ICONS = {
    "restaurant": "🍽️",
    "fuel": "⛽",
    "cafe": "☕",
    "hotel": "🏨",
    "pharmacy": "💊",
    "park": "🌳",
    "natural_feature": "🌿",
    "museum": "🏛️",
    "amusement_park": "🏁",
}


def _format_eta(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m:02d}min"
    if h:
        return f"{h}h"
    return f"{m}min"


_MAPS_MAX_WAYPOINTS = 9

def _maps_link(origin: str, destination: str, stops: list[dict]) -> tuple[str, int]:
    from urllib.parse import quote
    # Google Maps URL API aceita no máximo 9 waypoints intermediários.
    # Prioridade: waypoints fixos e postos primeiro (segurança), POIs só nas vagas restantes.
    fixed = [s for s in stops if s["type"] == "waypoint_fixed"]
    fuel = [s for s in stops if s["type"] == "fuel"]
    pois = [s for s in stops if s["type"] == "poi_fixed"]
    # prioridade de inclusão: fixed+fuel primeiro, POIs nas vagas restantes
    priority = sorted(fixed + fuel, key=lambda s: s["km_from_origin"])
    remaining = _MAPS_MAX_WAYPOINTS - len(priority)
    selected_pois = sorted(pois, key=lambda s: s["km_from_origin"])[:max(0, remaining)]
    waypoints = sorted(priority + selected_pois, key=lambda s: s["km_from_origin"])
    pois_omitted_from_link = max(0, len(pois) - len(selected_pois))
    wp_param = ""
    if waypoints:
        wp_str = "|".join(f"{s['lat']},{s['lon']}" for s in waypoints)
        wp_param = f"&waypoints={quote(wp_str)}"
    url = f"https://www.google.com/maps/dir/?api=1&origin={quote(origin)}&destination={quote(destination)}{wp_param}&travelmode=driving"
    return url, pois_omitted_from_link


def _format_whatsapp(
    origin: str,
    destination: str,
    total_km: float,
    total_minutes: int,
    stops: list[dict],
    fuel_gaps: list[float] | None = None,
    last_fuel_km: float | None = None,
) -> str:
    lines = [
        f"🗺️ *Rota: {origin} → {destination}*",
        f"Distância total: ~{total_km:.0f} km | Tempo estimado: ~{_format_eta(total_minutes)}",
        "",
    ]

    for stop in stops:
        icon = _STOP_ICONS.get(stop["type"], "📍")
        label = _STOP_LABELS.get(stop["type"], "Parada")
        km = stop["km_from_origin"]
        eta = _format_eta(stop["eta_minutes"])
        lines.append(f"{icon} *{label} — km {km:.0f} | ~{eta} de {origin}*")
        lines.append(f"📍 {stop['name']}")
        if stop.get("detour_km"):
            lines.append(f"↪️ Desvio: ~{stop['detour_km']} km da rota principal")
        if stop.get("rating"):
            lines.append(f"⭐ {stop['rating']} ({stop.get('user_ratings_total', 0)} avaliações)")
        lines.append("")

    lines.append(f"🏁 *{destination}* — chegada estimada em ~{_format_eta(total_minutes)}")

    if fuel_gaps:
        lines.append("")
        kms = ", ".join(f"km {g:.0f}" for g in fuel_gaps)
        lines.append(f"⛽⚠️ Sem posto mapeado perto de: {kms}. Abasteça antes — trecho pode exceder a autonomia.")

    if last_fuel_km is not None:
        trecho_final = total_km - last_fuel_km
        if trecho_final > 0:
            lines.append("")
            lines.append(f"ℹ️ Trecho final: ~{trecho_final:.0f} km sem posto previsto a partir do km {last_fuel_km:.0f}.")

    maps_url, pois_omitted_from_link = _maps_link(origin, destination, stops)
    lines.append("")
    lines.append(f"🔗 {maps_url}")
    if pois_omitted_from_link > 0:
        lines.append("")
        lines.append(f"⚠️ O link do Maps atingiu o limite de {_MAPS_MAX_WAYPOINTS} paradas intermediárias. "
                     f"{pois_omitted_from_link} ponto(s) de interesse não {'foi incluído' if pois_omitted_from_link == 1 else 'foram incluídos'} no link "
                     f"(postos e waypoints têm prioridade). Todos aparecem no itinerário acima.")
    lines.append("")
    lines.append("Quer explorar pontos de interesse ao longo da rota? É só pedir.")
    return "\n".join(lines)


def _format_poi_candidates(candidates: list[dict], origin: str, destination: str) -> str:
    from itertools import groupby

    lines = [
        f"🔍 *Pontos de interesse — {origin} → {destination}*",
        "",
    ]

    # agrupa por categoria preservando o índice real (1-based) de cada candidato
    # O índice exibido DEVE ser o mesmo usado no add_pois — nunca reiniciar por grupo
    by_type: dict[str, list[tuple[int, dict]]] = {}
    for real_idx, c in enumerate(candidates, 1):
        t = c.get("type", "")
        by_type.setdefault(t, []).append((real_idx, c))

    for place_type, items in by_type.items():
        label = _POI_TYPE_LABEL.get(place_type, "📍 Outros")
        lines.append(f"*{label}*")
        for real_idx, item in items:
            lines.append(
                f"{real_idx}. {item['name']} — km {item['km_from_origin']:.0f} | "
                f"desvio ~{item['detour_km']}km | "
                f"⭐ {item['rating']} ({item['user_ratings_total']} avaliações)"
            )

        lines.append("")

    lines.append("Quais quer adicionar à rota? Responda com os números (ex: \"1, 3 e 5\").")
    return "\n".join(lines)
