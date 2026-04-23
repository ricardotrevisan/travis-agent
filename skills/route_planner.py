import asyncio
import base64
from datetime import datetime, timezone
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill
from utils import geo_client

# último output de rota por sender — persiste no Redis
import json
import os
import redis as _redis_lib

def _get_redis():
    return _redis_lib.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
    )

def _save_last_route(sender: str, data: dict) -> None:
    try:
        _get_redis().setex(f"agent:route:last:{sender}", 86400, json.dumps(data))
    except Exception as e:
        print(f"[route_planner] redis save failed: {e}")

def _load_last_route(sender: str) -> dict:
    try:
        raw = _get_redis().get(f"agent:route:last:{sender}")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


class RoutePlannerSkill(BaseSkill):
    name = "route_planner"
    description = (
        "Planejar rotas de moto entre dois endereços ou cidades, com paradas de descanso, "
        "postos de abastecimento e pontos de interesse. Usar quando o usuário pedir rota, "
        "trajeto ou itinerário. Modo padrão: moto. Nunca perguntar sobre meio de transporte. "
        "action=plan (padrão): calcular rota. "
        "action=gpx: usar SOMENTE quando o usuário pedir explicitamente o arquivo GPX — "
        "nesse caso não incluir stops nos args, a skill recupera a rota automaticamente."
    )
    enabled = True
    planner_visible = True

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        action = (args.get("action") or "plan").strip().lower()
        if action == "gpx":
            return self._run_gpx(ctx, args)
        return self._run_plan(ctx, args)

    def _run_gpx(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        # tenta recuperar do cache local, senão usa args
        cached = _load_last_route(ctx.sender)
        stops = cached.get("stops") or args.get("stops") or []
        origin_str = cached.get("origin") or (args.get("origin") or "").strip()
        destination_str = cached.get("destination") or (args.get("destination") or "").strip()
        if not stops or not origin_str or not destination_str:
            return SkillResult(ok=False, user_visible_text="Não encontrei uma rota recente. Peça a rota primeiro e depois solicite o GPX.")

        gpx_bytes = _build_gpx(origin_str, destination_str, stops)
        link = _upload_gpx_to_drive(gpx_bytes, origin_str, destination_str)
        if not link:
            return SkillResult(ok=False, user_visible_text="Não consegui subir o GPX para o Drive. Tente novamente.")

        return SkillResult(
            ok=True,
            output={"gpx_link": link},
            user_visible_text=f"📎 GPX da rota {origin_str} → {destination_str} pronto:\n{link}",
        )

    def _run_plan(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        origin_str = (args.get("origin") or args.get("from") or args.get("start") or "").strip()
        destination_str = (args.get("destination") or args.get("to") or args.get("dest") or "").strip()
        if not origin_str or not destination_str:
            return SkillResult(
                ok=False,
                user_visible_text="Preciso de origem e destino para planejar a rota. Exemplo: 'rota de São Paulo para Florianópolis'.",
            )

        mode = (args.get("mode") or "car").strip()
        stop_interval_hours: float | None = _to_float(args.get("stop_interval_hours"))
        stop_interval_km: float = _to_float(args.get("stop_interval_km")) or 150.0
        max_stops: int = int(args.get("max_stops") or 4)
        preferences: list[str] = args.get("preferences") or []
        fixed_waypoint_strs: list[str] = args.get("fixed_waypoints") or []
        fixed_pois_args: list[dict] = args.get("fixed_pois") or []
        fuel_args: dict = args.get("fuel") or {}
        if not fuel_args:
            fuel_args = {"enabled": True, "max_interval_km": 180, "tank_km_remaining": 200}

        # 1. Geocodificar todos os pontos
        try:
            origin_coords = geo_client.geocode(origin_str)
        except Exception as exc:
            return SkillResult(ok=False, user_visible_text=f"Não consegui localizar a origem '{origin_str}'. Tente um nome mais específico.")

        try:
            destination_coords = geo_client.geocode(destination_str)
        except Exception as exc:
            return SkillResult(ok=False, user_visible_text=f"Não consegui localizar o destino '{destination_str}'. Tente um nome mais específico.")

        fixed_waypoint_coords: list[tuple[float, float]] = []
        for wp in fixed_waypoint_strs:
            try:
                fixed_waypoint_coords.append(geo_client.geocode(wp))
            except Exception:
                pass  # waypoint não encontrado — ignorar silenciosamente

        fixed_poi_coords: list[tuple[tuple[float, float], dict]] = []
        for poi in fixed_pois_args:
            try:
                coords = geo_client.geocode(poi.get("location") or poi.get("name") or "")
                fixed_poi_coords.append((coords, poi))
            except Exception:
                pass

        # 2. Calcular rota
        try:
            route = geo_client.get_route(origin_coords, destination_coords, fixed_waypoint_coords)
        except Exception as exc:
            return SkillResult(ok=False, user_visible_text=f"Não consegui calcular a rota. Verifique os nomes das cidades e tente novamente.")

        total_km: float = route["total_km"]
        total_minutes: int = route["total_minutes"]
        coordinates: list[tuple[float, float]] = route["coordinates"]

        # 3. Calcular intervalo de parada
        if stop_interval_hours is not None and stop_interval_hours > 0:
            speed_kmh = total_km / (total_minutes / 60) if total_minutes > 0 else 80.0
            effective_interval_km = stop_interval_hours * speed_kmh
        else:
            effective_interval_km = stop_interval_km

        # 4. Montar paradas sugeridas de descanso
        rest_points = geo_client.sample_waypoints(coordinates, total_km, effective_interval_km, total_minutes)
        stops: list[dict[str, Any]] = []

        for i, wp_str in enumerate(fixed_waypoint_strs):
            coords = fixed_waypoint_coords[i] if i < len(fixed_waypoint_coords) else None
            if not coords:
                continue
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

        # 5. Inserir fixed_pois verificando desvio
        omitted_pois: list[str] = []
        for coords, poi_def in fixed_poi_coords:
            max_detour = float(poi_def.get("max_detour_km") or 15)
            real_detour = geo_client.detour_km(coordinates, coords)
            if real_detour > max_detour:
                omitted_pois.append(poi_def.get("name") or "POI")
                continue
            km = _closest_km(coords, coordinates, total_km)
            stops.append({
                "type": "poi_fixed",
                "name": poi_def.get("name") or "Ponto de interesse",
                "lat": coords[0],
                "lon": coords[1],
                "km_from_origin": km,
                "eta_minutes": _eta(km, total_km, total_minutes),
                "detour_km": round(real_detour, 1),
                "pois": [],
            })

        # 6. Paradas de abastecimento
        fuel_stops: list[dict[str, Any]] = []
        if fuel_args.get("enabled"):
            fuel_interval = float(fuel_args.get("max_interval_km") or 200)
            tank_remaining = float(fuel_args.get("tank_km_remaining") or fuel_interval)
            fuel_brands = fuel_args.get("preferred_brands") or []
            fuel_categories = ["posto de gasolina"] + [b.lower() for b in fuel_brands]
            fuel_points = geo_client.sample_waypoints(coordinates, total_km, min(fuel_interval, tank_remaining * 0.85), total_minutes)
            for fp in fuel_points:
                nearby = geo_client.get_pois(fp["lat"], fp["lon"], 5000, fuel_categories)
                if nearby:
                    best = nearby[0]
                    fuel_stops.append({
                        "type": "fuel",
                        "name": best["name"],
                        "lat": best["lat"],
                        "lon": best["lon"],
                        "km_from_origin": fp["km_from_origin"],
                        "eta_minutes": fp["eta_minutes"],
                        "detour_km": 0,
                        "pois": [],
                    })

        # 7. Paradas de descanso com POIs e nome legível via reverse geocoding
        rest_stops: list[dict[str, Any]] = []
        for rp in rest_points[:max_stops]:
            pois: list[dict] = []
            if preferences:
                try:
                    pois = geo_client.get_pois(rp["lat"], rp["lon"], 3000, preferences)
                except Exception:
                    pass
            location_name = geo_client.reverse_geocode(rp["lat"], rp["lon"])
            rest_stops.append({
                "type": "rest",
                "name": location_name,
                "lat": rp["lat"],
                "lon": rp["lon"],
                "km_from_origin": rp["km_from_origin"],
                "eta_minutes": rp["eta_minutes"],
                "detour_km": None,
                "pois": pois,
            })

        all_stops = sorted(stops + fuel_stops + rest_stops, key=lambda s: s["km_from_origin"])

        # 8. Formatar texto
        display = _format_whatsapp(
            origin_str, destination_str, total_km, total_minutes, all_stops, omitted_pois
        )

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
                "fixed_pois_omitted": omitted_pois,
            },
            user_visible_text=display,
        )
        _save_last_route(ctx.sender, {"stops": all_stops, "origin": origin_str, "destination": destination_str})
        return result


# ── GPX ──────────────────────────────────────────────────────────────────────

def _build_gpx(origin: str, destination: str, stops: list[dict]) -> bytes:
    gpx = Element("gpx", {"version": "1.1", "creator": "travis-agent", "xmlns": "http://www.topografix.com/GPX/1/1"})
    meta = SubElement(gpx, "metadata")
    SubElement(meta, "name").text = f"{origin} → {destination}"
    SubElement(meta, "time").text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for stop in stops:
        wpt = SubElement(gpx, "wpt", {"lat": str(stop["lat"]), "lon": str(stop["lon"])})
        label = _STOP_LABELS.get(stop["type"], "Parada")
        SubElement(wpt, "name").text = stop["name"]
        SubElement(wpt, "desc").text = f"{label} — km {stop['km_from_origin']:.0f}"
        SubElement(wpt, "sym").text = "Waypoint"

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

        # extrai file_id do texto de retorno
        m = re.search(r"/d/([a-zA-Z0-9_-]+)", text)
        if not m:
            return None
        file_id = m.group(1)

        # tornar público
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

_POI_ICONS = {
    "restaurant": "🍽️",
    "fuel": "⛽",
    "cafe": "☕",
    "fast_food": "🍔",
    "hotel": "🏨",
    "guest_house": "🏠",
    "pharmacy": "💊",
    "toilets": "🚻",
    "park": "🌳",
    "viewpoint": "🏞️",
    "museum": "🏛️",
    "beach": "🏖️",
}


def _format_eta(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h{m:02d}min"
    if h:
        return f"{h}h"
    return f"{m}min"


def _maps_link(
    origin: str,
    destination: str,
    stops: list[dict],
) -> str:
    from urllib.parse import quote
    waypoints = [s for s in stops if s["type"] in ("waypoint_fixed", "poi_fixed", "fuel")]
    wp_param = ""
    if waypoints:
        wp_str = "|".join(f"{s['lat']},{s['lon']}" for s in waypoints)
        wp_param = f"&waypoints={quote(wp_str)}"
    origin_enc = quote(origin)
    dest_enc = quote(destination)
    return f"https://www.google.com/maps/dir/?api=1&origin={origin_enc}&destination={dest_enc}{wp_param}&travelmode=driving"


def _format_whatsapp(
    origin: str,
    destination: str,
    total_km: float,
    total_minutes: int,
    stops: list[dict],
    omitted_pois: list[str],
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
        for poi in stop.get("pois") or []:
            poi_icon = _POI_ICONS.get(poi.get("type") or "", "•")
            lines.append(f"  {poi_icon} {poi['name']}")
        lines.append("")

    lines.append(f"🏁 *{destination}* — chegada estimada em ~{_format_eta(total_minutes)}")

    if omitted_pois:
        lines.append("")
        lines.append(f"⚠️ Não incluí: {', '.join(omitted_pois)} (desvio excede o limite configurado).")

    lines.append("")
    lines.append(f"🔗 {_maps_link(origin, destination, stops)}")
    lines.append("")
    lines.append("Quer ajustar alguma parada, adicionar um ponto fixo ou mudar o intervalo?")
    return "\n".join(lines)
