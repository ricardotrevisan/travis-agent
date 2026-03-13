from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import redis
import requests
from garth.exc import GarthException, GarthHTTPError
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill

BACKFILL_START_DATE = "2026-01-01"
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "agent")
GARMINTOKENS = os.getenv("GARMINTOKENS", "~/.garminconnect")

RUN_KEYS = {"running", "trail_running", "track_running", "treadmill_running"}
TREADMILL_KEYS = {"treadmill_running"}
STRENGTH_KEYS = {"strength_training"}
ROW_KEYS = {"indoor_rowing", "rowing"}
BIKE_KEYS = {"indoor_cycling", "cycling"}


def today_iso() -> str:
    return date.today().isoformat()


def _safe_json_load(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _date_minus_days(value: str, days: int) -> str:
    return (_parse_date(value) - timedelta(days=days)).strftime("%Y-%m-%d")


def _to_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _meters_to_km(value: Any) -> float | None:
    meters = _safe_float(value)
    if meters is None:
        return None
    return round(meters / 1000.0, 3)


def _normalize_type(type_key: str | None, activity_name: str | None) -> str:
    key = (type_key or "").strip().lower()
    if key in TREADMILL_KEYS:
        return "Treadmill Running"
    if key in RUN_KEYS:
        return "Running"
    if key in STRENGTH_KEYS:
        return "Strength Training"
    if key in ROW_KEYS:
        return "Indoor Rowing"
    if key in BIKE_KEYS:
        return "Indoor Cycling"
    fallback = (activity_name or "").strip()
    return fallback[:80] if fallback else "Unknown"


def _extract_local_date(activity: dict[str, Any]) -> str | None:
    start_local = activity.get("startTimeLocal")
    if isinstance(start_local, str) and len(start_local) >= 10:
        return start_local[:10]
    return None


def _calc_pace_sec_per_km(type_label: str, distance_km: float | None, duration_sec: int | None) -> int | None:
    if type_label not in {"Running", "Treadmill Running"}:
        return None
    if not distance_km or not duration_sec or distance_km <= 0:
        return None
    return int(round(duration_sec / distance_km))


def _compact_strength_sets(raw_sets: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_sets, list):
        return []
    compacted: list[dict[str, Any]] = []
    for item in raw_sets:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "category": item.get("category"),
                "subCategory": item.get("subCategory"),
                "sets": _safe_int(item.get("sets")),
                "reps": _safe_int(item.get("reps")),
                "maxWeight": _safe_float(item.get("maxWeight")),
            }
        )
    return compacted


def _normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    type_key = ((activity.get("activityType") or {}).get("typeKey") or "").strip().lower()
    type_label = _normalize_type(type_key, activity.get("activityName"))
    distance_km = _meters_to_km(activity.get("distance"))
    duration_sec = _safe_int(activity.get("duration"))
    avg_hr = _safe_int(activity.get("averageHR"))
    calories = _safe_float(activity.get("calories"))
    source_id = str(activity.get("activityId") or activity.get("activityUUID") or "")
    normalized = {
        "sourceId": source_id,
        "date": _extract_local_date(activity),
        "startTimeLocal": activity.get("startTimeLocal"),
        "type": type_label,
        "distanceKm": distance_km,
        "durationSec": duration_sec,
        "avgPaceSecPerKm": _calc_pace_sec_per_km(type_label, distance_km, duration_sec),
        "avgHrBpm": avg_hr,
        "calories": calories,
        "trainingEffectAerobic": _safe_float(activity.get("aerobicTrainingEffect")),
        "trainingEffectAnaerobic": _safe_float(activity.get("anaerobicTrainingEffect")),
    }
    if type_label == "Strength Training":
        sets = _compact_strength_sets(activity.get("summarizedExerciseSets"))
        if sets:
            normalized["exerciseSets"] = sets
            normalized["totalSets"] = _safe_int(activity.get("totalSets"))
            normalized["totalReps"] = _safe_int(activity.get("totalReps"))
    if type_label == "Indoor Rowing":
        normalized["strokes"] = _safe_int(activity.get("strokes"))
        normalized["maxStrokeCadence"] = _safe_int(activity.get("maxStrokeCadence"))
    if type_label == "Indoor Cycling":
        normalized["avgPower"] = _safe_int(activity.get("avgPower"))
        normalized["maxFtp"] = _safe_int(activity.get("maxFtp"))
    return normalized


def _quality_score(activity: dict[str, Any]) -> int:
    score = 0
    duration = activity.get("durationSec")
    if isinstance(duration, int):
        score += min(duration // 300, 20)
    if activity.get("avgHrBpm") is not None:
        score += 2
    if activity.get("calories") is not None:
        score += 1
    if activity.get("distanceKm") not in (None, 0):
        score += 2
    if activity.get("type") == "Strength Training" and activity.get("exerciseSets"):
        score += 4
    if activity.get("type") == "Indoor Cycling" and activity.get("avgPower") is not None:
        score += 2
    if activity.get("type") == "Indoor Rowing" and activity.get("strokes") is not None:
        score += 2
    return score


def dedupe_activities(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not activities:
        return []

    def parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    ordered = sorted(activities, key=lambda x: (x.get("startTimeLocal") or "", x.get("sourceId") or ""))
    kept: list[dict[str, Any]] = []
    for candidate in ordered:
        cand_dt = parse_dt(candidate.get("startTimeLocal"))
        merged = False
        for idx, existing in enumerate(kept):
            if candidate.get("date") != existing.get("date") or candidate.get("type") != existing.get("type"):
                continue
            ex_dt = parse_dt(existing.get("startTimeLocal"))
            if not cand_dt or not ex_dt:
                continue
            delta = abs((cand_dt - ex_dt).total_seconds())
            if delta > 300:
                continue
            cand_dur = candidate.get("durationSec") or 0
            ex_dur = existing.get("durationSec") or 0
            if candidate.get("type") == "Strength Training":
                if cand_dur < 90 and ex_dur >= 300:
                    merged = True
                    break
                if ex_dur < 90 and cand_dur >= 300:
                    kept[idx] = candidate
                    merged = True
                    break
            if abs(cand_dur - ex_dur) <= 180 or cand_dur == 0 or ex_dur == 0:
                if _quality_score(candidate) > _quality_score(existing):
                    kept[idx] = candidate
                merged = True
                break
        if not merged:
            kept.append(candidate)
    return kept


def recompute_summary(history: list[dict[str, Any]]) -> dict[str, Any]:
    activities_count = 0
    total_distance_km = 0.0
    total_duration_sec = 0
    total_calories = 0.0
    by_type: dict[str, int] = defaultdict(int)
    for day in history:
        for activity in day.get("activities", []):
            activities_count += 1
            activity_type = activity.get("type") or "Unknown"
            by_type[activity_type] += 1
            if isinstance(activity.get("distanceKm"), (int, float)):
                total_distance_km += float(activity["distanceKm"])
            if isinstance(activity.get("durationSec"), int):
                total_duration_sec += activity["durationSec"]
            if isinstance(activity.get("calories"), (int, float)):
                total_calories += float(activity["calories"])
    return {
        "activitiesCount": activities_count,
        "totalDistanceKm": round(total_distance_km, 2),
        "totalDurationSec": total_duration_sec,
        "totalCalories": round(total_calories, 2),
        "byType": dict(sorted(by_type.items(), key=lambda kv: kv[0])),
    }


def _normalize_daily_summary(item: dict[str, Any]) -> dict[str, Any]:
    date_value = item.get("calendarDate") or item.get("date")
    return {
        "date": date_value,
        "steps": _safe_int(item.get("totalSteps")),
        "calories": _safe_float(item.get("activeKilocalories")),
        "restingHeartRate": _safe_int(item.get("restingHeartRate")),
        "sleepingSeconds": _safe_int(item.get("sleepingSeconds")),
        "moderateIntensityMinutes": _safe_int(item.get("moderateIntensityMinutes")),
        "vigorousIntensityMinutes": _safe_int(item.get("vigorousIntensityMinutes")),
    }


def _normalize_planned_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"raw": item}
    out = {
        "date": item.get("date") or item.get("calendarDate") or item.get("startDate"),
        "title": item.get("workoutName") or item.get("title") or item.get("name"),
        "type": item.get("workoutType") or item.get("sportType") or item.get("type"),
    }
    if item.get("estimatedDistanceInMeters") is not None:
        out["distanceKm"] = _meters_to_km(item.get("estimatedDistanceInMeters"))
    elif item.get("distance") is not None:
        out["distance"] = item.get("distance")
    if item.get("estimatedDurationInSecs") is not None:
        out["durationSec"] = _safe_int(item.get("estimatedDurationInSecs"))
    elif item.get("duration") is not None:
        out["duration"] = item.get("duration")
    if item.get("description") is not None:
        out["description"] = item.get("description")
    clean = {k: v for k, v in out.items() if v not in (None, "", [], {})}
    return clean if clean else {"raw": item}


def _summarize_training_plan(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"raw": item}
    fields = (
        "trainingPlanId",
        "planId",
        "planName",
        "name",
        "description",
        "startDate",
        "endDate",
        "goal",
        "level",
        "coachName",
        "category",
    )
    out = {k: item.get(k) for k in fields if item.get(k) is not None}
    return out if out else {"raw": item}


def _build_history(activities_raw: list[dict[str, Any]], daily_raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = defaultdict(lambda: {"date": None, "activities": []})
    for raw in activities_raw:
        if not isinstance(raw, dict):
            continue
        activity = _normalize_activity(raw)
        day = activity.get("date")
        if not day:
            continue
        by_date[day]["date"] = day
        by_date[day]["activities"].append(activity)
    for _, entry in by_date.items():
        entry["activities"] = dedupe_activities(entry["activities"])
        entry["activities"].sort(key=lambda x: x.get("startTimeLocal") or "")
    for row in daily_raw:
        if not isinstance(row, dict):
            continue
        date_value = row.get("date")
        summary = row.get("summary")
        if not date_value or not isinstance(summary, dict):
            continue
        by_date[date_value]["date"] = date_value
        by_date[date_value]["dailySummary"] = _normalize_daily_summary(summary)
    return sorted(by_date.values(), key=lambda x: x["date"])


def _normalize_training_plans(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [_summarize_training_plan(item) for item in raw]
    if isinstance(raw, dict):
        out: list[dict[str, Any]] = []
        for key, value in raw.items():
            if isinstance(value, list):
                out.extend(_summarize_training_plan(item) for item in value)
            elif isinstance(value, dict):
                out.append(_summarize_training_plan(value))
            else:
                out.append({str(key): value})
        return out
    return []


def transform(payload: dict[str, Any]) -> dict[str, Any]:
    activities_raw = payload.get("activities_completed") or []
    daily_raw = payload.get("daily_summaries") or []
    planned_raw = payload.get("scheduled_or_planned") or []
    training_plans_raw = payload.get("training_plans") or []
    history = _build_history(activities_raw, daily_raw)
    upcoming = []
    if isinstance(planned_raw, list):
        for item in planned_raw:
            upcoming.append(_normalize_planned_item(item) if isinstance(item, dict) else {"raw": item})
    result = {
        "lastUpdate": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sourceRange": payload.get("range"),
        "sources": payload.get("sources"),
        "history": history,
        "upcoming": upcoming,
        "trainingPlans": _normalize_training_plans(training_plans_raw),
    }
    result["summary"] = recompute_summary(history)
    return result


def _garmin_iso_dates(start: str, end: str) -> list[str]:
    start_dt = _to_date(start)
    end_dt = _to_date(end)
    if start_dt > end_dt:
        raise ValueError("start_date cannot be greater than end_date")
    days: list[str] = []
    cur = start_dt
    while cur <= end_dt:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _garmin_try_call(api: Garmin, names: list[str], *args: Any, **kwargs: Any) -> tuple[str | None, Any]:
    for name in names:
        fn = getattr(api, name, None)
        if callable(fn):
            try:
                return name, fn(*args, **kwargs)
            except Exception:
                continue
    return None, None


def _garmin_extract_completed_activities(api: Garmin, start: str, end: str) -> dict[str, Any]:
    method, data = _garmin_try_call(api, ["get_activities_by_date", "get_activities"], start, end)
    if method == "get_activities_by_date":
        return {"method": method, "items": data or []}
    if method == "get_activities" and isinstance(data, list):
        return {"method": method, "items": data}
    return {"method": None, "items": []}


def _garmin_extract_daily_summaries(api: Garmin, start: str, end: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    used_method = None
    for day in _garmin_iso_dates(start, end):
        fn = getattr(api, "get_user_summary", None)
        if callable(fn):
            for call in (
                lambda: fn(day),
                lambda: fn(cdate=day),
                lambda: fn(calendarDate=day),
            ):
                try:
                    summary = call()
                    used_method = "get_user_summary"
                    items.append({"date": day, "summary": summary})
                    break
                except Exception:
                    continue
    return {"method": used_method, "items": items}


def _garmin_extract_scheduled(api: Garmin, start: str, end: str) -> dict[str, Any]:
    candidates = ["get_calendar_data", "get_calendar", "get_scheduled_workouts", "get_workouts"]
    for name in candidates:
        fn = getattr(api, name, None)
        if not callable(fn):
            continue
        for call in (
            lambda: fn(start, end),
            lambda: fn(startdate=start, enddate=end),
            lambda: fn(cdate=start),
            lambda: fn(),
        ):
            try:
                return {"method": name, "items": call()}
            except Exception:
                continue
    return {"method": None, "items": []}


def _garmin_extract_training_plans(api: Garmin) -> dict[str, Any]:
    method, data = _garmin_try_call(api, ["get_training_plans", "get_training_plan"])
    return {"method": method, "items": data or []}


def _garmin_build_output(api: Garmin, start: str, end: str) -> dict[str, Any]:
    activities = _garmin_extract_completed_activities(api, start, end)
    daily = _garmin_extract_daily_summaries(api, start, end)
    scheduled = _garmin_extract_scheduled(api, start, end)
    training_plans = _garmin_extract_training_plans(api)
    return {
        "lastUpdate": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "range": {"start": start, "end": end},
        "sources": {
            "activities": activities["method"],
            "dailySummaries": daily["method"],
            "scheduled": scheduled["method"],
            "trainingPlans": training_plans["method"],
        },
        "activities_completed": activities["items"],
        "daily_summaries": daily["items"],
        "scheduled_or_planned": scheduled["items"],
        "training_plans": training_plans["items"],
    }


def init_api_token_only(token_dir: str) -> Garmin:
    token_path = Path(token_dir).expanduser()
    if not token_path.exists():
        raise RuntimeError(f"garmin token directory not found: {token_path}")
    try:
        api = Garmin()
        api.login(str(token_path))
        return api
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
        GarthHTTPError,
        GarthException,
        requests.exceptions.RequestException,
    ) as exc:
        raise RuntimeError(f"garmin token login failed: {exc}") from exc


def bootstrap_token_login(token_dir: str, email: str, password: str, mfa_code: str | None = None) -> str:
    token_path = Path(token_dir).expanduser()
    try:
        api = Garmin(email=email, password=password, is_cn=False, return_on_mfa=True)
        result1, result2 = api.login()
        if result1 == "needs_mfa":
            if not mfa_code:
                raise RuntimeError("mfa required but no mfa_code provided")
            api.resume_login(result2, mfa_code.strip())
        token_path.mkdir(parents=True, exist_ok=True)
        api.garth.dump(str(token_path))
        return str(token_path)
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
        GarthHTTPError,
        GarthException,
        requests.exceptions.RequestException,
    ) as exc:
        raise RuntimeError(f"garmin credential login failed: {exc}") from exc


def fetch_normalized_payload(token_dir: str, start: str, end: str) -> dict[str, Any]:
    api = init_api_token_only(token_dir)
    raw = _garmin_build_output(api, start, end)
    return transform(raw)


def _format_duration(duration_sec: int | None) -> str | None:
    if not isinstance(duration_sec, int) or duration_sec <= 0:
        return None
    hours, rem = divmod(duration_sec, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _build_last_7_days_activities_text(payload: dict[str, Any], end_date: str) -> list[str]:
    try:
        end_dt = _to_date(end_date)
    except Exception:
        end_dt = date.today()
    start_dt = end_dt - timedelta(days=6)

    selected: list[tuple[str, str, dict[str, Any]]] = []
    for day in payload.get("history") or []:
        day_str = day.get("date")
        if not isinstance(day_str, str):
            continue
        try:
            day_dt = _to_date(day_str)
        except Exception:
            continue
        if day_dt < start_dt or day_dt > end_dt:
            continue
        for activity in day.get("activities") or []:
            if isinstance(activity, dict):
                selected.append((day_str, str(activity.get("startTimeLocal") or ""), activity))

    selected.sort(key=lambda row: (row[0], row[1]), reverse=True)
    lines = ["Done activities (last 7 days):"]
    if not selected:
        lines.append("- None")
        return lines

    for day_str, _, activity in selected:
        parts = [day_str, str(activity.get("type") or "Unknown")]
        duration_label = _format_duration(activity.get("durationSec"))
        if duration_label:
            parts.append(duration_label)
        distance_km = activity.get("distanceKm")
        if isinstance(distance_km, (int, float)) and distance_km > 0:
            parts.append(f"{distance_km:.2f} km")
        lines.append("- " + " | ".join(parts))
    return lines


def _build_sync_text(metrics: dict[str, Any], sync: dict[str, Any], payload: dict[str, Any] | None = None) -> str:
    by_type = metrics.get("byType") or {}
    by_type_text = ", ".join([f"{k}: {v}" for k, v in list(by_type.items())[:5]]) or "n/a"
    lines = [
        f"Garmin sync ({sync.get('mode')}) {sync.get('effective_start')} -> {sync.get('effective_end')}",
        f"Atividades: {metrics.get('activitiesCount', 0)}",
        f"Distância total: {metrics.get('totalDistanceKm', 0)} km",
        f"Duração total: {metrics.get('totalDurationSec', 0)} s",
        f"Calorias: {metrics.get('totalCalories', 0)}",
        f"Próximos treinos: {metrics.get('upcomingCount', 0)}",
        f"Tipos: {by_type_text}",
    ]
    if payload:
        lines.append("")
        lines.extend(_build_last_7_days_activities_text(payload, str(sync.get("effective_end") or today_iso())))
    return "\n".join(lines)


def _dedupe_identity(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not activities:
        return []
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in activities:
        source_id = str(item.get("sourceId") or "")
        date_value = str(item.get("date") or "")
        start_local = str(item.get("startTimeLocal") or "")
        key = (source_id, date_value, start_local)
        if key == ("", "", ""):
            key = (str(item.get("type") or ""), date_value, start_local)
        by_key[key] = item
    return list(by_key.values())


def _merge_history(prev_history: list[dict[str, Any]], new_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for entry in prev_history:
        date_value = entry.get("date")
        if not date_value:
            continue
        by_date[date_value] = {
            "date": date_value,
            "activities": list(entry.get("activities") or []),
            "dailySummary": entry.get("dailySummary"),
        }
    for entry in new_history:
        date_value = entry.get("date")
        if not date_value:
            continue
        if date_value not in by_date:
            by_date[date_value] = {"date": date_value, "activities": []}
        existing = by_date[date_value]
        merged_activities = (existing.get("activities") or []) + (entry.get("activities") or [])
        merged_activities = _dedupe_identity(merged_activities)
        merged_activities = dedupe_activities(merged_activities)
        merged_activities.sort(key=lambda x: x.get("startTimeLocal") or "")
        existing["activities"] = merged_activities
        if entry.get("dailySummary") is not None:
            existing["dailySummary"] = entry.get("dailySummary")
    return [by_date[d] for d in sorted(by_date.keys())]


def _merge_payload(prev_payload: dict[str, Any], new_payload: dict[str, Any]) -> dict[str, Any]:
    merged_history = _merge_history(prev_payload.get("history") or [], new_payload.get("history") or [])
    merged = {
        "lastUpdate": new_payload.get("lastUpdate"),
        "sourceRange": new_payload.get("sourceRange"),
        "sources": new_payload.get("sources"),
        "history": merged_history,
        "upcoming": new_payload.get("upcoming") or prev_payload.get("upcoming") or [],
        "trainingPlans": new_payload.get("trainingPlans") or prev_payload.get("trainingPlans") or [],
    }
    merged["summary"] = recompute_summary(merged_history)
    return merged


class GarminTrackingSkill(BaseSkill):
    name = "garmin_tracking"
    description = "Fetch Garmin activities, daily summaries, upcoming workouts, and training plans."

    def __init__(self) -> None:
        self.redis = self._init_redis()

    def _init_redis(self):
        try:
            r = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                password=REDIS_PASSWORD,
                decode_responses=True,
            )
            r.ping()
            return r
        except Exception as exc:
            print(f"[skill=garmin_tracking] redis_unavailable err={exc}")
            return None

    def _key_sync_state(self, sender: str) -> str:
        return f"{REDIS_PREFIX}:v2:garmin:sync_state:{sender}"

    def _key_last_payload(self, sender: str) -> str:
        return f"{REDIS_PREFIX}:v2:garmin:last_payload:{sender}"

    def _resolve_range(self, sender: str, args: dict[str, Any]) -> tuple[str, str, str]:
        explicit_start = (args.get("start_date") or "").strip()
        explicit_end = (args.get("end_date") or "").strip()
        today = today_iso()
        if explicit_start:
            return "manual_range", explicit_start, explicit_end or today
        sync_state = _safe_json_load(self.redis.get(self._key_sync_state(sender)) if self.redis else None) or {}
        last_end = sync_state.get("last_success_end_date")
        if last_end:
            return "incremental", _date_minus_days(last_end, 1), today
        return "backfill", BACKFILL_START_DATE, today

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        if not self.redis:
            return SkillResult(ok=False, error="garmin redis unavailable")

        mode, start, end = self._resolve_range(ctx.sender, args)
        print(f"[skill=garmin_tracking] sync_start mode={mode} sender={ctx.sender} start={start} end={end}")
        try:
            new_payload = fetch_normalized_payload(GARMINTOKENS, start, end)
        except Exception as exc:
            print(f"[skill=garmin_tracking] fetch_failed err={exc}")
            return SkillResult(ok=False, error=f"garmin session unavailable: {exc}")

        prev_payload = _safe_json_load(self.redis.get(self._key_last_payload(ctx.sender))) or {}
        merged_payload = _merge_payload(prev_payload, new_payload) if prev_payload else new_payload
        summary = merged_payload.get("summary") or {}
        metrics = {
            "activitiesCount": int(summary.get("activitiesCount") or 0),
            "totalDistanceKm": float(summary.get("totalDistanceKm") or 0),
            "totalDurationSec": int(summary.get("totalDurationSec") or 0),
            "totalCalories": float(summary.get("totalCalories") or 0),
            "byType": summary.get("byType") or {},
            "upcomingCount": len(merged_payload.get("upcoming") or []),
        }
        sync = {
            "mode": mode,
            "effective_start": start,
            "effective_end": end,
            "last_sync_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        }
        state = {
            "last_success_start_date": start,
            "last_success_end_date": end,
            "last_sync_at": sync["last_sync_at"],
            "last_status": "ok",
            "mode": mode,
        }
        self.redis.set(self._key_last_payload(ctx.sender), json.dumps(merged_payload, ensure_ascii=False))
        self.redis.set(self._key_sync_state(ctx.sender), json.dumps(state, ensure_ascii=False))
        print(
            "[skill=garmin_tracking] sync_ok "
            f"mode={mode} activities={metrics['activitiesCount']} upcoming={metrics['upcomingCount']}"
        )
        return SkillResult(
            ok=True,
            output={"raw": merged_payload, "metrics": metrics, "sync": sync},
            user_visible_text=_build_sync_text(metrics, sync, merged_payload),
        )
