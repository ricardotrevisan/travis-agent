import json
import os
import re
import traceback
from typing import Any, Callable, Dict, Optional

import redis
from dotenv import load_dotenv
from openai import OpenAI

from agent.tools.search_tool import WebSearchTool
from agent.tools.summarize_tool import SummarizeURLTool
from agent.tools.mcp_adapter import load_mcp_tools

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

MODEL = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5-mini")
ALLOWED_WHATSAPP_NUMBERS = {
    re.sub(r"\D", "", item)
    for item in os.getenv("ALLOWED_WHATSAPP_NUMBERS", "").split(",")
    if re.sub(r"\D", "", item)
}

MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "20"))
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "4"))

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "agent")
WEBHOOK_DEDUPE_TTL_SECONDS = int(os.getenv("WEBHOOK_DEDUPE_TTL_SECONDS", "300"))

AGENT_INSTRUCTIONS = (
    "Você é um assistente objetivo. "
    "Responda diretamente ao pedido do usuário. "
    "Use tools quando necessário. "
    "Não invente resultados de tools. "
    "Nunca escreva chamadas de tool como texto. "
    "Se uma tool falhar, explique isso em linguagem natural e encerre. "
    "Não tente repetir a mesma tool após falha. "
    "Não ofereça várias opções desnecessárias."
)


# -------------------------------------------------
# Tools
# -------------------------------------------------

_summarize_url_tool = SummarizeURLTool()
_web_search_tool = WebSearchTool()


def web_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "missing query"}

    try:
        result = _web_search_tool.run({"query": query})
        return {"ok": True, "data": result}
    except Exception as exc:
        return {"ok": False, "error": f"web_search failed: {exc}"}


def summarize_url(args: Dict[str, Any]) -> Dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "missing url"}

    try:
        result = _summarize_url_tool.run({"url": url})
        return {"ok": True, "data": result}
    except Exception as exc:
        return {"ok": False, "error": f"summarize_url failed: {exc}"}


TOOLS = {
    "web_search": web_search,
    "summarize_url": summarize_url,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "web_search",
        "description": "Search the web",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "summarize_url",
        "description": "Summarize a webpage",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    },
]

_mcp_tool_calls, _mcp_tool_schemas = load_mcp_tools()
if _mcp_tool_calls:
    print(f"[mcp] loaded {len(_mcp_tool_calls)} tools from MCP_URL")
    TOOLS.update(_mcp_tool_calls)
    TOOL_SCHEMAS.extend(_mcp_tool_schemas)


def normalize_whatsapp_number(value: str) -> str:
    if not value:
        return ""
    base = value.split("@", 1)[0]
    return re.sub(r"\D", "", base)


def has_tool_error(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is False


def parse_tool_args(raw_args: Any) -> Dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if not raw_args:
        return {}
    try:
        parsed = json.loads(raw_args)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def build_tool_error_message(error_text: str) -> str:
    text = (error_text or "").lower()

    if "missing url" in text:
        return "Faltou a URL para eu analisar."
    if "missing query" in text:
        return "Faltou o termo de busca."
    if "timeout" in text:
        return "Nao consegui concluir a acao porque a requisicao expirou."
    if "summarize_url failed" in text:
        return "Nao consegui ler esse link agora."
    if "web_search failed" in text:
        return "Nao consegui fazer a busca agora."
    if "invalid tool arguments" in text:
        return "A tool recebeu argumentos invalidos."
    if "too many tool rounds" in text:
        return "Nao consegui concluir essa acao agora."

    return "Nao consegui concluir essa acao agora."


def sanitize_reply(text: str, fallback_error: Optional[str] = None) -> str:
    cleaned = (text or "").strip()
    lowered = cleaned.lower()

    blocked_patterns = (
        "(to=",
        "to=functions.",
        "functions.web_search",
        "functions.summarize_url",
        "function_call",
        "function_call_output",
        '"type": "function"',
        '"type":"function"',
    )

    if any(pattern in lowered for pattern in blocked_patterns):
        return build_tool_error_message(fallback_error or "")

    if not cleaned and fallback_error:
        return build_tool_error_message(fallback_error)

    return cleaned


# -------------------------------------------------
# AgentEngine
# -------------------------------------------------


class AgentEngine:
    def __init__(self, send_media_callback=None):
        self.send_media_callback = send_media_callback
        self.histories: Dict[str, list[Dict[str, str]]] = {}
        self.image_memory: Dict[str, str] = {}
        self.pdf_memory: Dict[str, str] = {}
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
            print(f"[redis] unavailable: {exc}")
            return None

    def _redis_key(self, kind: str, value: str) -> str:
        return f"{REDIS_PREFIX}:{kind}:{value}"

    def _mark_message_processed(self, message_id: str) -> bool:
        if not message_id:
            return False

        if not self.redis:
            return False

        key = self._redis_key("msg", message_id)
        try:
            created = self.redis.set(key, "1", ex=WEBHOOK_DEDUPE_TTL_SECONDS, nx=True)
            return bool(created)
        except Exception as exc:
            print(f"[redis] dedupe error: {exc}")
            return False

    def ingest_media(
        self,
        sender: str,
        image_b64: Optional[str] = None,
        pdf_b64: Optional[str] = None,
    ):
        if image_b64:
            self.image_memory[sender] = image_b64
        if pdf_b64:
            self.pdf_memory[sender] = pdf_b64

    def reset_session(self, sender: str):
        self.histories.pop(sender, None)
        self.image_memory.pop(sender, None)
        self.pdf_memory.pop(sender, None)

    def is_allowed_sender(self, sender: str) -> bool:
        if not ALLOWED_WHATSAPP_NUMBERS:
            return True
        return normalize_whatsapp_number(sender) in ALLOWED_WHATSAPP_NUMBERS

    def _trim_history(self, sender: str):
        history = self.histories.setdefault(sender, [])
        if len(history) > MAX_HISTORY_TURNS:
            self.histories[sender] = history[-MAX_HISTORY_TURNS:]

    def ask(self, user_message: str, sender: str = "default") -> str:
        history = self.histories.setdefault(sender, [])
        history.append({"role": "user", "content": user_message})
        self._trim_history(sender)

        response = client.responses.create(
            model=MODEL,
            instructions=AGENT_INSTRUCTIONS,
            input=history,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )

        round_count = 0
        last_tool_error: Optional[str] = None

        while True:
            tool_calls = [
                item for item in (response.output or [])
                if getattr(item, "type", "") == "function_call"
            ]

            if not tool_calls:
                break
            
            tool_calls_debug = []
            for call in tool_calls:
                tool_calls_debug.append(
                    {
                        "name": getattr(call, "name", "") or "",
                        "call_id": getattr(call, "call_id", "") or "",
                        "args": parse_tool_args(getattr(call, "arguments", None)),
                    }
                )
            print(
                f"[tool_calls] round={round_count} count={len(tool_calls)} calls={json.dumps(tool_calls_debug, ensure_ascii=False)}"
            )
            
            if round_count >= MAX_TOOL_ROUNDS:
                last_tool_error = "too many tool rounds"
                break

            outputs = []
            saw_tool_error = False

            for call in tool_calls:
                name = getattr(call, "name", "") or ""
                call_id = getattr(call, "call_id", "") or ""
                args = parse_tool_args(getattr(call, "arguments", None))

                if not call_id:
                    continue

                if name not in TOOLS:
                    result = {"ok": False, "error": f"unknown tool {name}"}
                elif not isinstance(args, dict):
                    result = {"ok": False, "error": "invalid tool arguments"}
                else:
                    result = TOOLS[name](args)

                if has_tool_error(result):
                    saw_tool_error = True
                    last_tool_error = str(result.get("error") or "tool failed")

                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )

            if not outputs:
                last_tool_error = last_tool_error or "tool output missing"
                break

            response = client.responses.create(
                model=MODEL,
                instructions=AGENT_INSTRUCTIONS,
                previous_response_id=response.id,
                input=outputs,
                tools=[] if saw_tool_error else TOOL_SCHEMAS,
                tool_choice="none" if saw_tool_error else "auto",
            )

            round_count += 1

            if saw_tool_error:
                break

        raw_reply = response.output_text or ""
        reply = sanitize_reply(raw_reply, fallback_error=last_tool_error)

        if not reply and last_tool_error:
            reply = build_tool_error_message(last_tool_error)

        if not reply:
            reply = "Nao consegui responder agora."

        history.append({"role": "assistant", "content": reply})
        self._trim_history(sender)
        return reply

    def respond(
        self,
        sender: str,
        user_message: str,
        instance_name: str,
        image_b64: Optional[str] = None,
        pdf_b64: Optional[str] = None,
    ) -> str:
        self.ingest_media(sender, image_b64=image_b64, pdf_b64=pdf_b64)
        return self.ask(user_message, sender=sender)

    def handle_inbound_whatsapp(
        self,
        wa_event: Dict[str, Any],
        instance_name: str,
        send_text: Callable[[str, str, str], Any],
        send_media: Callable[[str, str, str, str], Any],
        media_downloader: Callable[[str, Dict[str, Any]], str],
    ) -> str:
        try:
            if wa_event.get("event") != "messages.upsert":
                return ""

            data = wa_event.get("data") or {}
            msg = data.get("message") or {}
            key = data.get("key") or {}
            sender = key.get("remoteJid")
            message_id = key.get("id")

            if not sender or bool(key.get("fromMe")):
                return ""

            if not self.is_allowed_sender(sender):
                return ""

            if message_id:
                created = self._mark_message_processed(message_id)
                if self.redis and not created:
                    print(f"[dedupe] duplicate message ignored: {message_id}")
                    return ""

            web_message_info = {
                "key": key,
                "message": msg,
                "pushName": data.get("pushName"),
                "messageTimestamp": data.get("messageTimestamp"),
                "instanceId": wa_event.get("instanceId") or wa_event.get("instance"),
                "source": wa_event.get("source", "web"),
            }

            image_b64 = None
            pdf_b64 = None
            user_message = ""

            if "imageMessage" in msg:
                image_b64 = (
                    msg.get("imageMessage", {}).get("base64")
                    or media_downloader(instance_name, web_message_info)
                )
                caption = msg.get("imageMessage", {}).get("caption") or ""
                user_message = "O usuário enviou uma imagem."
                if caption:
                    user_message += f" Legenda: {caption}"

            elif "documentMessage" in msg:
                file_b64 = media_downloader(instance_name, web_message_info)
                mimetype = msg["documentMessage"].get("mimetype", "")
                filename = msg["documentMessage"].get("fileName", "arquivo")

                if "pdf" in mimetype.lower() or filename.lower().endswith(".pdf"):
                    pdf_b64 = file_b64
                    user_message = f"O usuário enviou um PDF ({filename})."
                else:
                    user_message = f"O usuário enviou um documento ({filename}, {mimetype})."

            else:
                user_message = (
                    msg.get("conversation")
                    or msg.get("text", {}).get("body")
                    or ""
                )

            if not user_message.strip():
                return ""

            if user_message.strip().lower() == "/new":
                self.reset_session(sender)
                reply_text = "Sessao resetada. Contexto anterior limpo."
                send_text(sender, reply_text, instance_name)
                return reply_text

            reply_text = self.respond(
                sender=sender,
                user_message=user_message,
                instance_name=instance_name,
                image_b64=image_b64,
                pdf_b64=pdf_b64,
            )

            if reply_text:
                send_text(sender, reply_text, instance_name)

            return reply_text

        except Exception:
            traceback.print_exc()
            return ""


if __name__ == "__main__":
    agent = AgentEngine()

    while True:
        user = input("> ").strip()
        if user == "exit":
            break
        print(agent.ask(user))
