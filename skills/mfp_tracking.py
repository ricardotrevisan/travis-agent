from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import redis
from bs4 import BeautifulSoup

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill

MFP_USERNAME = os.getenv("MFP_USERNAME", "")
MFP_PASSWORD = os.getenv("MFP_PASSWORD", "")
MFP_DIARY_URL = "https://www.myfitnesspal.com/food/diary"
MFP_LOGIN_URL = "https://www.myfitnesspal.com/pt/user/login"
MFP_BACKFILL_DAYS = int(os.getenv("MFP_BACKFILL_DAYS", "7"))
MFP_SESSION_TTL = int(os.getenv("MFP_SESSION_TTL", str(7 * 24 * 3600)))  # 7 days
MFP_PAGE_DELAY = float(os.getenv("MFP_PAGE_DELAY", "1.5"))

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "agent")

_MONTH_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FoodEntry:
    food_name: str
    brand: str = ""
    serving_size: str = ""
    calories: float = 0.0
    macros: dict[str, float] = field(default_factory=dict)


@dataclass
class MealSection:
    name: str
    entries: list[FoodEntry] = field(default_factory=list)
    meal_totals: dict[str, float] = field(default_factory=dict)


@dataclass
class DayDiary:
    date: str
    meals: list[MealSection] = field(default_factory=list)
    day_totals: dict[str, float] = field(default_factory=dict)
    day_goals: dict[str, float] = field(default_factory=dict)
    water_ml: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    return date.today().isoformat()


def _date_minus_days(iso: str, days: int) -> str:
    return (date.fromisoformat(iso) - timedelta(days=days)).isoformat()


def _date_range(start_iso: str, end_iso: str) -> list[str]:
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _safe_json_load(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _parse_float(val: str | None) -> float:
    if not val:
        return 0.0
    cleaned = val.replace("\xa0", "").strip()
    # Remove unit suffixes like "g", "mg", "kcal", "%"
    for suffix in ("kcal", "mg", "g", "%"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    # Handle thousands separators: "1,205" (en) or "1.205" (pt)
    # If there's a comma followed by exactly 3 digits at end → thousands separator
    import re as _re
    if _re.search(r',\d{3}$', cleaned):
        cleaned = cleaned.replace(",", "")
    elif _re.search(r'\.\d{3}$', cleaned) and cleaned.count('.') == 1:
        cleaned = cleaned.replace(".", "")
    else:
        # Regular decimal: replace comma with dot
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Playwright session management
# ---------------------------------------------------------------------------

def _get_playwright_and_browser():
    """Lazy import to avoid hard failure when playwright is not installed."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    return sync_playwright


def _login_and_get_cookies() -> list[dict]:
    """Open a headless browser, log in to MFP and return session cookies."""
    sync_playwright = _get_playwright_and_browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        try:
            page.goto(MFP_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)

            # Dismiss privacy/consent overlay if present (blocks button clicks)
            try:
                page.evaluate(
                    'document.querySelectorAll("[id*=sp_message_container]").forEach(el => el.remove())'
                )
            except Exception:
                pass

            # MFP login form uses name="email" and name="password"
            page.fill('input[name="email"]', MFP_USERNAME)
            page.fill('input[name="password"]', MFP_PASSWORD)
            time.sleep(1)
            page.click('button[type="submit"]', force=True)
            time.sleep(5)

            # Verify login succeeded: diary page must not redirect to login
            page.goto(MFP_DIARY_URL, wait_until="networkidle", timeout=20_000)
            if "login" in page.url:
                raise RuntimeError("login failed — still on login page after submit")

            cookies = ctx.cookies()
            return cookies
        finally:
            browser.close()


def _fetch_diary_html(cookies: list[dict], diary_date: str) -> str:
    """Navigate to the diary for a given date and return the rendered HTML."""
    sync_playwright = _get_playwright_and_browser()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        try:
            url = f"{MFP_DIARY_URL}?date={diary_date}"
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(1)
            if "login" in page.url:
                raise RuntimeError("session_expired")
            return page.content()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------

_NUTRIENT_LABEL_MAP = {
    "calories": "calories",
    "calorias": "calories",
    "carbs": "carbohydrates_g",
    "carbohydrates": "carbohydrates_g",
    "carboidratos": "carbohydrates_g",
    "fat": "fat_g",
    "gordura": "fat_g",
    "protein": "protein_g",
    "proteína": "protein_g",
    "proteina": "protein_g",
    "fiber": "fiber_g",
    "fibra": "fiber_g",
    "sugar": "sugar_g",
    "açúcar": "sugar_g",
    "acucar": "sugar_g",
    "sodium": "sodium_mg",
    "sódio": "sodium_mg",
    "sodio": "sodium_mg",
    "potassium": "potassium_mg",
    "potássio": "potassium_mg",
    "potassio": "potassium_mg",
    "cholesterol": "cholesterol_mg",
    "colesterol": "cholesterol_mg",
    "vitamin a": "vitamin_a_pct",
    "vitamin c": "vitamin_c_pct",
    "calcium": "calcium_pct",
    "cálcio": "calcium_pct",
    "calcio": "calcium_pct",
    "iron": "iron_pct",
    "ferro": "iron_pct",
}


def _build_col_map(header_row) -> dict[int, str]:
    """
    Build {col_index: nutrient_key} from a meal_header <tr>.
    Col 0 is the food name column — skipped.
    Nutrient columns have class "nutrient-column" or "alt".
    """
    mapping: dict[int, str] = {}
    cells = header_row.find_all("td")
    for idx, cell in enumerate(cells):
        if idx == 0:
            continue  # food name column
        # Get text of the cell, ignoring the subtitle div (unit)
        subtitle = cell.find("div", class_="subtitle")
        if subtitle:
            subtitle.extract()
        label = cell.get_text(strip=True).lower()
        key = _NUTRIENT_LABEL_MAP.get(label)
        if key:
            mapping[idx] = key
    return mapping


def _parse_nutrient_cells(cells: list, col_map: dict[int, str]) -> dict[str, float]:
    """Extract nutrient values from a row's cells using col_map."""
    nutrients: dict[str, float] = {}
    for idx, key in col_map.items():
        if idx >= len(cells):
            continue
        cell = cells[idx]
        # Values inside macro cells are in <span class="macro-value">; plain cells are direct text
        macro_span = cell.find("span", class_="macro-value")
        raw = macro_span.get_text(strip=True) if macro_span else cell.get_text(strip=True)
        nutrients[key] = _parse_float(raw)
    return nutrients


def _parse_diary_html(html: str, diary_date: str) -> DayDiary:
    """
    Parse MFP diary HTML into a DayDiary.

    Real MFP structure (confirmed from live HTML):
    - Single <table id="diary-table"> contains everything
    - Meals delimited by <tr class="meal_header">
      - first <td class="first alt"> = meal name
      - remaining <td class="alt nutrient-column"> = nutrient column headers (first meal only)
    - Food entry rows: plain <tr> with <td class="first alt"><a>name</a></td>
    - Meal totals: <tr class="bottom">
    - Day totals: <tr class="total">
    - Daily goals: <tr class="total alt">
    """
    soup = BeautifulSoup(html, "html.parser")
    day = DayDiary(date=diary_date)

    table = soup.find("table", id="diary-table")
    if not table:
        return day

    rows = table.find_all("tr")

    # Build column map from the first meal_header that has nutrient columns
    col_map: dict[int, str] = {}
    for row in rows:
        classes = row.get("class") or []
        if "meal_header" in classes:
            candidate = _build_col_map(row)
            if candidate:
                col_map = candidate
                break

    current_meal: MealSection | None = None

    for row in rows:
        classes = set(row.get("class") or [])

        # --- meal header row ---
        if "meal_header" in classes:
            if current_meal is not None:
                day.meals.append(current_meal)
            first_td = row.find("td", class_="first")
            meal_name = first_td.get_text(strip=True) if first_td else ""
            current_meal = MealSection(name=meal_name) if meal_name else None
            continue

        # --- meal totals row ---
        if "bottom" in classes and current_meal is not None:
            cells = row.find_all("td")
            nutrients = _parse_nutrient_cells(cells, col_map)
            calories = nutrients.pop("calories", 0.0)
            current_meal.meal_totals = {"calories": calories, **nutrients}
            continue

        # --- day totals row ---
        if classes == {"total"}:
            cells = row.find_all("td")
            nutrients = _parse_nutrient_cells(cells, col_map)
            calories = nutrients.pop("calories", 0.0)
            day.day_totals = {"calories": calories, **nutrients}
            continue

        # --- daily goals row ---
        if "total" in classes and "alt" in classes:
            cells = row.find_all("td")
            nutrients = _parse_nutrient_cells(cells, col_map)
            calories = nutrients.pop("calories", 0.0)
            day.day_goals = {"calories": calories, **nutrients}
            continue

        # --- food entry row ---
        if current_meal is None:
            continue
        first_td = row.find("td", class_="first")
        if not first_td:
            continue
        link = first_td.find("a", class_="js-show-edit-food")
        if not link:
            continue

        food_name = link.get_text(strip=True)
        cells = row.find_all("td")
        nutrients = _parse_nutrient_cells(cells, col_map)
        calories = nutrients.pop("calories", 0.0)

        current_meal.entries.append(FoodEntry(
            food_name=food_name,
            calories=calories,
            macros=nutrients,
        ))

    # Don't forget the last meal
    if current_meal is not None:
        day.meals.append(current_meal)

    return day

    return meal


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _merge_days(existing: dict[str, Any], new_days: list[DayDiary]) -> dict[str, Any]:
    """Merge new DayDiary objects into the stored payload dict (keyed by date)."""
    days_dict: dict[str, Any] = existing.get("days") or {}
    for day in new_days:
        days_dict[day.date] = asdict(day)
    today = _today_iso()
    return {
        "days": days_dict,
        "lastUpdate": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        "sourceRange": {
            "start": min(days_dict.keys()) if days_dict else today,
            "end": max(days_dict.keys()) if days_dict else today,
        },
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _fmt_date_pt(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} de {_MONTH_PT[d.month]} de {d.year}"


def _fmt_pct(actual: float, goal: float) -> str:
    if goal <= 0:
        return ""
    return f" ({int(actual / goal * 100)}%)"


def _build_day_text(day: DayDiary) -> str:
    lines: list[str] = [
        f"Diário MFP — {_fmt_date_pt(day.date)}",
        "─" * 36,
    ]
    for meal in day.meals:
        total_cal = meal.meal_totals.get("calories", 0.0)
        lines.append(f"{meal.name:<20} {int(total_cal):>5} kcal")

    lines.append("")
    totals = day.day_totals
    goals = day.day_goals

    cal = totals.get("calories", 0.0)
    cal_goal = goals.get("calories", 0.0)
    lines.append(f"Total: {int(cal)} / {int(cal_goal)} kcal{_fmt_pct(cal, cal_goal)}")

    for key, label, unit in [
        ("protein_g", "Proteína", "g"),
        ("carbohydrates_g", "Carbo", "g"),
        ("fat_g", "Gordura", "g"),
    ]:
        val = totals.get(key, 0.0)
        goal_val = goals.get(key, 0.0)
        goal_str = f" / {int(goal_val)}{unit}" if goal_val else ""
        pct_str = _fmt_pct(val, goal_val) if goal_val else ""
        lines.append(f"{label:<12} {int(val)}{unit}{goal_str}{pct_str}")

    return "\n".join(lines)


def _build_sync_text(days: list[DayDiary], sync: dict[str, Any]) -> str:
    if not days:
        return "Nenhum dado encontrado para o período solicitado."
    blocks = [_build_day_text(d) for d in days]
    header = f"[sync: {sync['mode']} {sync['effective_start']}→{sync['effective_end']}]"
    return "\n\n---\n\n".join(blocks) + f"\n\n_{header}_"


def _build_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    days_dict = payload.get("days") or {}
    if not days_dict:
        return {"daysCount": 0}
    cals = [d.get("day_totals", {}).get("calories", 0.0) for d in days_dict.values()]
    prots = [d.get("day_totals", {}).get("protein_g", 0.0) for d in days_dict.values()]
    return {
        "daysCount": len(days_dict),
        "avgCalories": round(sum(cals) / len(cals), 1) if cals else 0,
        "avgProtein_g": round(sum(prots) / len(prots), 1) if prots else 0,
    }


# ---------------------------------------------------------------------------
# Skill class
# ---------------------------------------------------------------------------

class MFPTrackingSkill(BaseSkill):
    name = "mfp_tracking"
    description = (
        "Buscar o diário alimentar do MyFitnessPal: refeições, calorias, macros e metas do dia. "
        "Aceita start_date e end_date (YYYY-MM-DD). Sem datas: busca incremental desde a última sync."
    )

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
            print(f"[skill=mfp_tracking] redis_unavailable {exc}")
            return None

    def _key_payload(self, sender: str) -> str:
        return f"{REDIS_PREFIX}:v2:mfp:payload:{sender}"

    def _key_sync_state(self, sender: str) -> str:
        return f"{REDIS_PREFIX}:v2:mfp:sync_state:{sender}"

    def _key_session(self, sender: str) -> str:
        return f"{REDIS_PREFIX}:v2:mfp:session:{sender}"

    def _resolve_range(self, sender: str, args: dict[str, Any]) -> tuple[str, str, str]:
        explicit_start = (args.get("start_date") or "").strip()
        explicit_end = (args.get("end_date") or "").strip()
        today = _today_iso()
        if explicit_start:
            return "manual_range", explicit_start, explicit_end or today
        sync_state = _safe_json_load(self.redis.get(self._key_sync_state(sender)) if self.redis else None) or {}
        last_end = sync_state.get("last_success_end_date")
        if last_end:
            return "incremental", _date_minus_days(last_end, 1), today
        backfill_start = _date_minus_days(today, MFP_BACKFILL_DAYS - 1)
        return "backfill", backfill_start, today

    def _load_cookies(self, sender: str) -> list[dict] | None:
        if not self.redis:
            return None
        raw = self.redis.get(self._key_session(sender))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else None
        except Exception:
            return None

    def _save_cookies(self, sender: str, cookies: list[dict]) -> None:
        if not self.redis:
            return
        self.redis.set(
            self._key_session(sender),
            json.dumps(cookies, ensure_ascii=False),
            ex=MFP_SESSION_TTL,
        )

    def _ensure_cookies(self, sender: str) -> list[dict]:
        """Return valid cookies, re-logging in if necessary."""
        cookies = self._load_cookies(sender)
        if cookies:
            # Quick validity check: try fetching today's diary
            try:
                html = _fetch_diary_html(cookies, _today_iso())
                if "login" not in html[:500].lower():
                    return cookies
            except RuntimeError as exc:
                if "session_expired" not in str(exc):
                    raise
            print(f"[skill=mfp_tracking] session_expired sender={sender} — re-logging in")

        if not MFP_USERNAME or not MFP_PASSWORD:
            raise RuntimeError("MFP_USERNAME or MFP_PASSWORD not set")

        print(f"[skill=mfp_tracking] login sender={sender}")
        cookies = _login_and_get_cookies()
        self._save_cookies(sender, cookies)
        return cookies

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        if not self.redis:
            return SkillResult(ok=False, error="mfp redis unavailable")

        mode, start, end = self._resolve_range(ctx.sender, args)
        print(f"[skill=mfp_tracking] sync_start mode={mode} sender={ctx.sender} start={start} end={end}")

        try:
            cookies = self._ensure_cookies(ctx.sender)
        except Exception as exc:
            print(f"[skill=mfp_tracking] session_error {exc}")
            return SkillResult(ok=False, error=f"mfp login failed: {exc}")

        dates = _date_range(start, end)
        fetched_days: list[DayDiary] = []

        for diary_date in dates:
            try:
                html = _fetch_diary_html(cookies, diary_date)
                day = _parse_diary_html(html, diary_date)
                fetched_days.append(day)
                print(f"[skill=mfp_tracking] fetched date={diary_date} meals={len(day.meals)}")
            except RuntimeError as exc:
                if "session_expired" in str(exc):
                    # Re-login once and retry
                    try:
                        cookies = _login_and_get_cookies()
                        self._save_cookies(ctx.sender, cookies)
                        html = _fetch_diary_html(cookies, diary_date)
                        day = _parse_diary_html(html, diary_date)
                        fetched_days.append(day)
                    except Exception as retry_exc:
                        print(f"[skill=mfp_tracking] fetch_retry_failed date={diary_date} err={retry_exc}")
                        return SkillResult(ok=False, error=f"mfp session error: {retry_exc}")
                else:
                    print(f"[skill=mfp_tracking] fetch_failed date={diary_date} err={exc}")
                    return SkillResult(ok=False, error=f"mfp fetch error: {exc}")
            except Exception as exc:
                print(f"[skill=mfp_tracking] fetch_failed date={diary_date} err={exc}")
                return SkillResult(ok=False, error=f"mfp fetch error: {exc}")

            if len(dates) > 1:
                time.sleep(MFP_PAGE_DELAY)

        # Merge with stored payload
        prev_payload = _safe_json_load(self.redis.get(self._key_payload(ctx.sender))) or {}
        merged_payload = _merge_days(prev_payload, fetched_days)

        sync = {
            "mode": mode,
            "effective_start": start,
            "effective_end": end,
            "last_sync_at": datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat(),
        }
        state = {
            "last_success_start_date": start,
            "last_success_end_date": end,
            "last_sync_at": sync["last_sync_at"],
            "last_status": "ok",
            "mode": mode,
        }

        self.redis.set(self._key_payload(ctx.sender), json.dumps(merged_payload, ensure_ascii=False))
        self.redis.set(self._key_sync_state(ctx.sender), json.dumps(state, ensure_ascii=False))

        metrics = _build_metrics(merged_payload)
        user_text = _build_sync_text(fetched_days, sync)

        print(
            f"[skill=mfp_tracking] sync_ok mode={mode} days={len(fetched_days)} "
            f"avgCal={metrics.get('avgCalories')}"
        )
        return SkillResult(
            ok=True,
            output={"raw": merged_payload, "metrics": metrics, "sync": sync},
            user_visible_text=user_text,
        )
