import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from runtime.models import RequestContext, SkillResult
from skills.base import BaseSkill

_ISO_8601_WITH_TZ_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})\b"
)
_ISO_8601_NO_TZ_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?\b"
)
_DMY_TIME_RE = re.compile(
    r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})(?:[/-](?P<year>\d{2,4}))?"
    r"\s*(?:as|às)?\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\b",
    re.IGNORECASE,
)
_RELATIVE_TIME_RE = re.compile(
    r"\b(?P<token>hoje|amanh[ãa])(?:\s*(?:as|às)?\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?)?\b",
    re.IGNORECASE,
)
_JID_RE = re.compile(r"^\d{8,16}@s\.whatsapp\.net$")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


class N8NScheduleAlertSkill(BaseSkill):
    name = "n8n_schedule_alert"
    description = "Criar, listar e remover alertas agendados no n8n para o remetente atual."

    def __init__(self) -> None:
        self.webhook_url = (os.getenv("N8N_SCHEDULE_WEBHOOK_URL") or "").strip()
        self.timeout_seconds = float(os.getenv("N8N_SCHEDULE_TIMEOUT", "15"))
        self.schedule_timezone = os.getenv("SCHEDULE_TIMEZONE") or os.getenv("TZ") or "America/Sao_Paulo"

    @staticmethod
    def _safe_tz(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            # Fallback for containers without tzdata installed.
            return timezone(timedelta(hours=-3))

    @staticmethod
    def _looks_like_email(text: str) -> bool:
        raw = (text or "").strip().lower()
        if raw.endswith("@s.whatsapp.net"):
            return False
        return bool(_EMAIL_RE.search(raw))

    @staticmethod
    def _is_valid_sender_jid(sender: str) -> bool:
        return bool(_JID_RE.fullmatch((sender or "").strip().lower()))

    def _extract_run_at_iso(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text:
            return None

        iso_match = _ISO_8601_WITH_TZ_RE.search(text)
        if iso_match:
            raw = iso_match.group(0)
            if raw.endswith("Z"):
                return raw
            try:
                parsed = datetime.fromisoformat(raw)
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                return raw

        iso_no_tz = _ISO_8601_NO_TZ_RE.search(text)
        if iso_no_tz:
            raw = iso_no_tz.group(0).replace(" ", "T")
            try:
                parsed = datetime.fromisoformat(raw)
                tz = self._safe_tz(self.schedule_timezone)
                localized = parsed.replace(tzinfo=tz)
                return localized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                return None

        tz = self._safe_tz(self.schedule_timezone)
        now = datetime.now(tz)

        rel = _RELATIVE_TIME_RE.search(text)
        if rel:
            token = (rel.group("token") or "").lower()
            hour = rel.group("hour")
            minute = rel.group("minute")
            if hour is None:
                return None
            target_day = now.date()
            if token.startswith("amanh"):
                target_day = target_day + timedelta(days=1)
            local_dt = datetime(
                target_day.year,
                target_day.month,
                target_day.day,
                int(hour),
                int(minute or "0"),
                tzinfo=tz,
            )
            return local_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        dmy = _DMY_TIME_RE.search(text)
        if dmy:
            day = int(dmy.group("day"))
            month = int(dmy.group("month"))
            year_raw = dmy.group("year")
            hour = dmy.group("hour")
            minute = dmy.group("minute")
            if year_raw is None:
                year = now.year
            else:
                year = int(year_raw)
                if year < 100:
                    year += 2000
            try:
                local_dt = datetime(
                    year,
                    month,
                    day,
                    int(hour),
                    int(minute or "0"),
                    tzinfo=tz,
                )
                return local_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except ValueError:
                return None

        return None

    @staticmethod
    def _extract_message_text(user_text: str) -> str:
        text = (user_text or "").strip()
        quoted = re.findall(r"\"([^\"]+)\"|'([^']+)'", text)
        for pair in quoted:
            candidate = (pair[0] or pair[1] or "").strip()
            if candidate:
                return candidate

        cleaned = re.sub(
            r"\b(agendar|agende|agendar|lembrete|alerta|schedule|remind|n8n|no n8n|para)\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(_ISO_8601_WITH_TZ_RE, "", cleaned)
        cleaned = re.sub(_DMY_TIME_RE, "", cleaned)
        cleaned = re.sub(_RELATIVE_TIME_RE, "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")
        return cleaned or text

    def _build_payload(self, ctx: RequestContext, run_at: str, user_text: str) -> dict[str, Any]:
        message_text = self._extract_message_text(user_text)
        title = (message_text[:80] or "Lembrete Travis").strip()
        return {
            "action": "create",
            "data": {
                "title": title,
                "run_at": run_at,
                "payload": {
                    "message": message_text,
                    "target": {
                        "sender": ctx.sender,
                        "instance": ctx.instance_name,
                    },
                },
            },
        }

    @staticmethod
    def _detect_action(args: dict[str, Any]) -> str:
        arg_action = str((args or {}).get("action") or "").strip().lower()
        return arg_action if arg_action in {"create", "list", "delete"} else ""

    @staticmethod
    def _extract_task_id(user_text: str, args: dict[str, Any]) -> str:
        for key in ("task_id", "idTask", "id"):
            value = (args or {}).get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        text = user_text or ""
        uuid_match = _UUID_RE.search(text)
        if uuid_match:
            return uuid_match.group(0)
        idtask_match = re.search(r"idtask\s*[:=]?\s*([A-Za-z0-9\-_]+)", text, re.IGNORECASE)
        if idtask_match:
            return idtask_match.group(1).strip()
        return ""

    @staticmethod
    def _extract_task_response(data: Any) -> dict[str, str]:
        if not isinstance(data, dict):
            return {}

        candidates = [data]
        for key in ("result", "data", "task", "webhookResponse"):
            value = data.get(key)
            if isinstance(value, dict):
                candidates.append(value)

        for parent in candidates:
            nested = parent.get("task")
            if isinstance(nested, dict):
                candidates.append(nested)

        task_id = ""
        run_at = ""
        status = ""
        for item in candidates:
            if not task_id:
                task_id = str(item.get("idTask") or item.get("task_id") or item.get("id") or "").strip()
            if not run_at:
                run_at = str(item.get("run_at") or item.get("runAt") or "").strip()
            if not status:
                status = str(item.get("status") or item.get("state") or "").strip()

        out: dict[str, str] = {}
        if task_id:
            out["task_id"] = task_id
        if run_at:
            out["run_at"] = run_at
        if status:
            out["status"] = status
        return out

    @staticmethod
    def _format_success_text(task_info: dict[str, str]) -> str:
        parts = []
        if task_info.get("task_id"):
            parts.append("Alerta agendado com sucesso no n8n.")
            parts.append(f"idTask: {task_info['task_id']}.")
        else:
            parts.append(
                "Recebi resposta do n8n, mas sem idTask. "
                "Nao consigo confirmar a criacao do alerta; verifique no n8n."
            )
        if task_info.get("run_at"):
            parts.append(f"run_at: {task_info['run_at']}.")
        if task_info.get("status"):
            parts.append(f"status: {task_info['status']}.")
        return " ".join(parts)

    @staticmethod
    def _format_list_text(data: Any) -> str:
        if not isinstance(data, dict):
            return "Consulta de agendamentos executada."
        buckets: list[list[Any]] = []
        for key in ("tasks", "items", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                buckets.append(value)
            if isinstance(value, dict):
                inner = value.get("tasks") or value.get("items") or value.get("results")
                if isinstance(inner, list):
                    buckets.append(inner)
        tasks = buckets[0] if buckets else []
        count = len(tasks)
        if count == 0:
            return "Consulta de agendamentos executada. Total encontrado: 0."

        lines = [f"Consulta de agendamentos executada. Total encontrado: {count}."]
        for idx, item in enumerate(tasks, start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "-").strip() or "-"
            run_at = str(item.get("run_at") or item.get("runAt") or "-").strip() or "-"
            status = str(item.get("status") or item.get("state") or "-").strip() or "-"
            created_at = str(item.get("created_at") or item.get("createdAt") or "-").strip() or "-"
            lines.append(
                f"{idx}. title: {title} | run_at: {run_at} | status: {status} | created_at: {created_at}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_delete_text(task_id: str, status: str) -> str:
        if status:
            return f"Tarefa removida no n8n. idTask: {task_id}. status: {status}."
        return f"Tarefa removida no n8n. idTask: {task_id}."

    def _clarification_text(self) -> str:
        return (
            "Preciso de data e horário exatos para agendar no n8n. "
            "Exemplo: 2026-03-10 16:30 (vou assumir GMT-03:00)."
        )

    def run(self, ctx: RequestContext, args: dict[str, Any]) -> SkillResult:
        if not self.webhook_url:
            return SkillResult(
                ok=True,
                output={"error_stage": "config"},
                user_visible_text="N8N_SCHEDULE_WEBHOOK_URL não está configurada.",
            )

        if not self._is_valid_sender_jid(ctx.sender) or self._looks_like_email(ctx.sender):
            return SkillResult(
                ok=True,
                output={"error_stage": "validation"},
                user_visible_text="Não consegui agendar porque o sender do WhatsApp está inválido.",
            )

        action = self._detect_action(args)
        if not action:
            return SkillResult(
                ok=True,
                output={"needs_clarification": True, "error_stage": "action"},
                user_visible_text="Confirme a ação no n8n: create, list ou delete.",
            )
        payload: dict[str, Any]
        if action == "create":
            raw_dt = str(args.get("run_at") or args.get("datetime") or args.get("time") or "").strip()
            if not raw_dt:
                for v in args.values():
                    if isinstance(v, str) and (
                        _ISO_8601_WITH_TZ_RE.search(v) or _ISO_8601_NO_TZ_RE.search(v)
                    ):
                        raw_dt = v.strip()
                        break
            run_at = self._extract_run_at_iso(raw_dt) if raw_dt else self._extract_run_at_iso(ctx.user_text)
            if not run_at:
                return SkillResult(
                    ok=True,
                    output={"needs_clarification": True, "action": action},
                    user_visible_text=self._clarification_text(),
                )
            payload = self._build_payload(ctx=ctx, run_at=run_at, user_text=ctx.user_text)
        elif action == "list":
            payload = {
                "action": "list",
                "data": {
                    "payload": {
                        "target": {
                            "sender": ctx.sender,
                            "instance": ctx.instance_name,
                        }
                    }
                },
            }
        else:
            task_id = self._extract_task_id(ctx.user_text, args)
            if not task_id:
                return SkillResult(
                    ok=True,
                    output={"needs_clarification": True, "action": action},
                    user_visible_text="Para excluir no n8n, preciso do idTask da tarefa.",
                )
            payload = {
                "action": "delete",
                "data": {
                    "idTask": task_id,
                    "task_id": task_id,
                    "payload": {
                        "target": {
                            "sender": ctx.sender,
                            "instance": ctx.instance_name,
                        }
                    },
                },
            }
        try:
            resp = requests.post(
                self.webhook_url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return SkillResult(
                ok=True,
                output={"error_stage": "request", "error": str(exc)},
                user_visible_text=f"Falha ao conectar no n8n para agendar alerta: {exc}",
            )

        if not resp.ok:
            body = (resp.text or "").strip()
            if len(body) > 300:
                body = body[:300] + "...<trunc>"
            return SkillResult(
                ok=True,
                output={"error_stage": "response", "status_code": resp.status_code, "body": body},
                user_visible_text=f"N8N retornou erro ao criar alerta (HTTP {resp.status_code}).",
            )

        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {"raw": (resp.text or "").strip()}

        task_info = self._extract_task_response(data)
        if action == "create":
            text = self._format_success_text(task_info)
        elif action == "list":
            text = self._format_list_text(data)
        else:
            resolved_task_id = str(payload.get("data", {}).get("idTask") or task_info.get("task_id") or "-")
            text = self._format_delete_text(resolved_task_id, task_info.get("status", ""))
        return SkillResult(
            ok=True,
            output={"action": action, "payload": payload, "n8n_response": data, "task": task_info},
            user_visible_text=text,
        )
