# AgentEngine with centralized media handling (image/PDF) and
# WhatsApp event ingestion (handle_inbound_whatsapp).

import os
import re
import json
import traceback
from collections import OrderedDict
from time import time
from typing import Any, Dict, List, Optional, Callable
import redis

from dotenv import load_dotenv
from openai import OpenAI

from agent.tools import (
    WebSearchTool,
    SummarizeURLTool,
    GenerateImageTool,
    OCRImageTool,
    PDFExtractorTool,
)
from utils.text_summary import summarize_text_locally

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_BETA_HEADER = os.getenv("OPENAI_BETA_HEADER", "responses-2024-12-17")

MAX_TOOL_CONTEXT_CHARS = int(os.getenv("MAX_TOOL_CONTEXT_CHARS", "12000"))
SESSION_MAX_STORED_TURNS = int(os.getenv("SESSION_MAX_STORED_TURNS", "40"))
SESSION_RECENT_TURNS_FOR_PROMPT = int(os.getenv("SESSION_RECENT_TURNS_FOR_PROMPT", "10"))
SESSION_SUMMARY_MAX_CHARS = int(os.getenv("SESSION_SUMMARY_MAX_CHARS", "1200"))
SESSION_SUMMARY_MAX_TOKENS = int(os.getenv("SESSION_SUMMARY_MAX_TOKENS", "200"))
SESSION_SUMMARIZER_MODE = os.getenv("SESSION_SUMMARIZER_MODE", "local")  # local|openai
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "3"))
SESSION_MAX_USERS = int(os.getenv("SESSION_MAX_USERS", "500"))
MEDIA_TTL_SECONDS = int(os.getenv("MEDIA_TTL_SECONDS", "900"))
RESPONSES_MODEL = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5-mini")
RESPONSES_MODEL_SECOND = os.getenv("OPENAI_RESPONSES_MODEL_SECOND", RESPONSES_MODEL)
OPENAI_SUMMARY_MODEL = os.getenv("OPENAI_SUMMARY_MODEL", RESPONSES_MODEL)
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "agent")

openai_client = (
    OpenAI(
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_BASE_URL,
        default_headers={"x-openai-beta": OPENAI_BETA_HEADER},
    )
    if OPENAI_API_KEY
    else None
)

# -----------------------------------------------------------------
# Helper utilities
# -----------------------------------------------------------------


def _get(obj: Any, key: str, default=None):
    if hasattr(obj, key):
        return getattr(obj, key, default)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def extract_tool_calls(response: Any) -> List[Any]:
    calls: List[Any] = []

    required = _get(response, "required_action", None)
    if required:
        submit = _get(required, "submit_tool_outputs", None)
        tool_calls = _get(submit, "tool_calls", []) if submit else []
        calls.extend(tool_calls)

    for item in _get(response, "output", []) or []:
        if _get(item, "type") in ("function_call", "function_tool_call"):
            calls.append(item)

    return calls


def extract_output_text(response: Any) -> Optional[str]:
    txt = _get(response, "output_text")
    if txt:
        return str(txt).strip()

    outputs = _get(response, "output", []) or []
    for item in outputs:
        if _get(item, "type") == "message":
            content = _get(item, "content", [])
            if isinstance(content, list):
                combined = " ".join(
                    [_get(c, "text", "") for c in content if _get(c, "text")]
                )
                if combined:
                    return combined.strip()

    return None


def clamp_tool_context(text: str, max_chars: int = MAX_TOOL_CONTEXT_CHARS) -> str:
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[conteúdo truncado]"


def extract_urls(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"https?://\S+", text)


def format_search_bullets(results: List[Dict[str, str]], limit: int = 3) -> str:
    limited = results[:limit]
    bullets = []
    for r in limited:
        title = (r.get("title") or r.get("url") or "").strip()
        url = r.get("url", "").strip()
        snippet = (r.get("snippet") or "").strip()
        snippet = snippet[:220] + ("..." if len(snippet) > 220 else "")
        parts = [p for p in [title, url, snippet] if p]
        bullets.append(" • ".join(parts))
    return "\n".join(f"- {b}" for b in bullets if b)


def log_tool_calls(response: Any, prefix: str = "[Responses]"):
    try:
        status = _get(response, "status")
        required = _get(_get(response, "required_action", {}), "submit_tool_outputs", {})
        required_calls = _get(required, "tool_calls", []) or []
        print(f"{prefix} status={status} required_calls={len(required_calls)}")

        for idx, call in enumerate(required_calls, 1):
            func = getattr(call, "function", None) or _get(call, "function", None)
            name = getattr(func, "name", "") or _get(func, "name", "")
            raw = getattr(func, "arguments", "") or _get(func, "arguments", "") or "{}"
            trimmed = raw[:400] + ("...<trunc>" if len(raw) > 400 else "")
            print(f"{prefix} required_call #{idx}: name={name} args={trimmed}")

        for item in _get(response, "output", []) or []:
            if _get(item, "type") in ("function_call", "function_tool_call"):
                name = _get(item, "name", "")
                raw = _get(item, "arguments", "") or ""
                trimmed = raw[:400] + ("...<trunc>" if len(raw) > 400 else "")
                print(f"{prefix} output_call: name={name} args={trimmed}")

    except Exception as exc:
        print(f"{prefix} erro ao logar: {exc}")


# -----------------------------------------------------------------
# AgentEngine
# -----------------------------------------------------------------


class AgentEngine:
    def __init__(self, send_media_callback=None):
        if not openai_client:
            raise RuntimeError("OPENAI_API_KEY ausente.")

        self.client = openai_client
        self.send_media_callback = send_media_callback
        self.redis = self._init_redis()

        # user -> [{role, content}]
        self.conversations: Dict[str, List[Dict[str, str]]] = {}
        self.session_summaries: Dict[str, str] = {}
        self.user_order: "OrderedDict[str, float]" = OrderedDict()

        # memória de mídias por usuário (com timestamp)
        self.image_memory: Dict[str, Dict[str, Any]] = {}
        self.pdf_memory: Dict[str, Dict[str, Any]] = {}

        # tools disponíveis
        self.tools = [
            WebSearchTool(),
            SummarizeURLTool(),
            GenerateImageTool(),
            OCRImageTool(),
            PDFExtractorTool(),
        ]

        self.tool_map = {t.name: t for t in self.tools}
        self.tool_schemas = [t.as_schema() for t in self.tools]

    # ---------------------------------------------------------------
    # Redis helpers
    # ---------------------------------------------------------------
    def _init_redis(self):
        if not REDIS_HOST:
            return None
        try:
            client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                password=REDIS_PASSWORD,
                decode_responses=True,
            )
            client.ping()
            print(f"[redis] conectado em {REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")
            return client
        except Exception as exc:
            print(f"[redis] falha ao conectar: {exc}")
            return None

    def _redis_key(self, sender: str, suffix: str) -> str:
        return f"{REDIS_PREFIX}:{sender}:{suffix}"

    def _redis_set_json(self, key: str, value: Any, ex: Optional[int] = None):
        if not self.redis:
            return
        try:
            payload = value if isinstance(value, str) else json.dumps(value)
            self.redis.set(key, payload, ex=ex)
        except Exception as exc:
            print(f"[redis] erro set {key}: {exc}")

    def _redis_get_json(self, key: str) -> Optional[Any]:
        if not self.redis:
            return None
        try:
            raw = self.redis.get(key)
            if raw is None:
                return None
            try:
                return json.loads(raw)
            except Exception:
                return raw
        except Exception as exc:
            print(f"[redis] erro get {key}: {exc}")
            return None

    def _load_user_state(self, sender: str):
        if not self.redis:
            return

        if sender not in self.conversations:
            conv = self._redis_get_json(self._redis_key(sender, "conversation"))
            if isinstance(conv, list):
                self.conversations[sender] = conv

        if sender not in self.session_summaries:
            summary = self._redis_get_json(self._redis_key(sender, "summary"))
            if isinstance(summary, str):
                self.session_summaries[sender] = summary

    # ---------------------------------------------------------------
    # Ingestão de mídia centralizada
    # ---------------------------------------------------------------
    def ingest_media(self, sender: str, image_b64: Optional[str], pdf_b64: Optional[str]):
        if image_b64:
            self.image_memory[sender] = {"b64": image_b64, "ts": time()}
            print(f"[debug] imagem armazenada para {sender}, b64_len={len(image_b64)}")
            self._redis_set_json(self._redis_key(sender, "image"), self.image_memory[sender], ex=MEDIA_TTL_SECONDS)
        if pdf_b64:
            self.pdf_memory[sender] = {"b64": pdf_b64, "ts": time()}
            print(f"[debug] pdf armazenado para {sender}, b64_len={len(pdf_b64)}")
            self._redis_set_json(self._redis_key(sender, "pdf"), self.pdf_memory[sender], ex=MEDIA_TTL_SECONDS)

    def _get_media_b64(self, store: Dict[str, Dict[str, Any]], sender: str) -> Optional[str]:
        item = store.get(sender)
        if not item:
            # tenta recuperar do Redis
            key = "image" if store is self.image_memory else "pdf"
            cached = self._redis_get_json(self._redis_key(sender, key))
            if cached:
                store[sender] = cached
                item = cached
            else:
                return None
        if time() - item.get("ts", 0) > MEDIA_TTL_SECONDS:
            store.pop(sender, None)
            return None
        return item.get("b64")

    def _prune_media(self):
        now = time()
        for store in (self.image_memory, self.pdf_memory):
            expired = [s for s, meta in store.items() if now - meta.get("ts", 0) > MEDIA_TTL_SECONDS]
            for sender in expired:
                store.pop(sender, None)

    def _touch_user(self, sender: str):
        # Move para o fim para manter ordem LRU
        if sender in self.user_order:
            self.user_order.pop(sender, None)
        self.user_order[sender] = time()

    def _evict_old_users(self):
        while len(self.user_order) > SESSION_MAX_USERS:
            oldest_sender, _ = self.user_order.popitem(last=False)
            self.conversations.pop(oldest_sender, None)
            self.session_summaries.pop(oldest_sender, None)
            self.image_memory.pop(oldest_sender, None)
            self.pdf_memory.pop(oldest_sender, None)
            print(f"[debug] LRU evict user={oldest_sender}")

    def _format_turns(self, turns: List[Dict[str, str]]) -> str:
        parts: List[str] = []
        for msg in turns:
            role = msg.get("role")
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            prefix = "Usuário" if role == "user" else "Assistente" if role == "assistant" else (role or "")
            parts.append(f"{prefix}: {content}".strip())
        return "\n".join(parts).strip()

    def _summarize_history(self, existing_summary: str, new_text: str) -> str:
        if not new_text and not existing_summary:
            return ""

        blocks = []
        if existing_summary:
            blocks.append(f"[Resumo anterior]\n{existing_summary}")
        if new_text:
            blocks.append(f"[Novas interações]\n{new_text}")
        source_text = "\n\n".join(blocks)

        if SESSION_SUMMARIZER_MODE.lower() == "openai":
            try:
                resp = self.client.responses.create(
                    model=OPENAI_SUMMARY_MODEL,
                    instructions=(
                        "Resuma em português, destacando fatos, pedidos e decisões. "
                        f"Limite a cerca de {SESSION_SUMMARY_MAX_CHARS} caracteres. "
                        "Inclua datas ou valores quando aparecerem."
                    ),
                    input=source_text,
                    max_output_tokens=SESSION_SUMMARY_MAX_TOKENS,
                    tools=[],
                    tool_choice="none",
                    metadata={"source": "session_summary"},
                    reasoning={"effort": "minimal"},
                )
                txt = extract_output_text(resp)
                if txt:
                    return txt[:SESSION_SUMMARY_MAX_CHARS]
            except Exception as exc:
                print(f"[summary] fallback local: {exc}")

        return summarize_text_locally(source_text, max_chars=SESSION_SUMMARY_MAX_CHARS)

    # ---------------------------------------------------------------
    def _build_history_text(self, sender: str) -> str:
        history = self.conversations.get(sender, [])
        summary = self.session_summaries.get(sender, "")

        recent = history[-SESSION_RECENT_TURNS_FOR_PROMPT:]
        parts = []

        if summary:
            parts.append(f"[Resumo acumulado]\n{summary}\n")

        for msg in recent:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "user":
                parts.append(f"Usuário: {content}")
            elif role == "assistant":
                parts.append(f"Assistente: {content}")

        return "\n".join(parts).strip()

    # ---------------------------------------------------------------
    def _run_tool_call(self, call: Any, sender: str) -> Dict[str, Any]:
        func = getattr(call, "function", None) or _get(call, "function", None)
        name = (
            getattr(func, "name", "")
            or _get(func, "name", "")
            or getattr(call, "name", "")
            or _get(call, "name", "")
        )

        raw = (
            getattr(func, "arguments", "")
            or _get(func, "arguments", "")
            or getattr(call, "arguments", "")
            or _get(call, "arguments", "")
            or "{}"
        )

        try:
            args = json.loads(raw)
        except Exception:
            args = {}

        # injeta mídias corretas de acordo com a ferramenta
        if name == "ocr_image":
            b64 = self._get_media_b64(self.image_memory, sender)
            if b64:
                args["image_b64"] = b64
                if args.get("image_path") in ("", None, "internal"):
                    args.pop("image_path", None)

        if name == "pdf_extract":
            b64 = self._get_media_b64(self.pdf_memory, sender)
            if b64:
                print(f"[debug] pdf_extract usando b64 len={len(b64)}")
                args["source_b64"] = b64
                args.setdefault("source", "internal_pdf")

        tool = self.tool_map.get(name)
        print(f"tool name {name} - tool {tool}")
        if not tool:
            return {"error": f"Ferramenta desconhecida: {name}"}

        return tool.run(args)

    # ---------------------------------------------------------------
    def respond(
        self,
        sender: str,
        user_message: str,
        instance_name: str,
        image_b64: Optional[str] = None,
        pdf_b64: Optional[str] = None,
    ) -> str:
        try:
            self._touch_user(sender)
            self._evict_old_users()
            self._prune_media()
            self._load_user_state(sender)

            # atualiza memórias de mídia se chegaram direto por parâmetro
            self.ingest_media(sender, image_b64=image_b64, pdf_b64=pdf_b64)

            history = self.conversations.get(sender, [])
            history_text = self._build_history_text(sender)
            urls_in_message = extract_urls(user_message)

            full_input = (
                (history_text + "\n") if history_text else ""
            ) + f"Usuário: {user_message}\nAssistente:"

            # -----------------------------------------------------------
            # PRIMEIRA RODADA
            # -----------------------------------------------------------
            response1 = self.client.responses.create(
                model=RESPONSES_MODEL,
                instructions=(
                    "Você é um agente multimodal especializado em resolver problemas e análises com base em texto do usuário, "
                    "URLs fornecidas, imagens recebidas e PDFs internos disponíveis. "
                    "Seu foco é utilidade imediata, assertividade e execução precisa das ferramentas. "

                    "REGRAS GERAIS: "
                    "Nunca peça confirmação para executar uma tool. "
                    "Nunca ofereça opções de fluxo, formatos ou métodos. "
                    "Nunca pergunte preferências. "
                    "Nunca faça follow-ups desnecessários. "
                    "Nunca revele caminhos internos, nomes de arquivos ou lógica interna. "
                    "Priorize agir, não perguntar. "

                    "PDF: "
                    "Se existir PDF disponível internamente, execute pdf_extract imediatamente e extraia o conteúdo completo. "
                    "Nunca peça autorização nem pergunte o formato. "

                    "IMAGENS: "
                    "Se houver imagem associada à conversa, utilize OCR automaticamente quando relevante, usando ocr_image. "
                    "Não pergunte ao usuário se ele quer OCR. "

                    "URLs: "
                    "Se o usuário fornecer uma URL, use summarize_url para análise de conteúdo. "
                    "Use web_search apenas quando o usuário pedir busca ou investigação explícita. "
                    "Nunca pergunte qual ferramenta ele prefere. "

                    "GERAÇÃO DE IMAGEM: "
                    "Se o usuário pedir uma imagem, use generate_image imediatamente. "

                    "RACIOCÍNIO: "
                    "Responda sempre de forma direta ao pedido principal. "
                    "Priorize o uso da tool apropriada sempre que aplicável. "
                    "Sem etapas intermediárias, sem hesitação: aja."
                ),
                input=full_input,
                max_output_tokens=2048,
                tools=self.tool_schemas,
                tool_choice="auto",
                parallel_tool_calls=True,
                metadata={"source": "evolution_agent"},
                reasoning={"effort": "minimal"},
            )

            log_tool_calls(response1, "[Primeira rodada]")

            tool_calls = extract_tool_calls(response1)

            # -----------------------------------------------------------
            # SEGUNDA RODADA (se houver tools)
            # -----------------------------------------------------------
            if tool_calls or urls_in_message:
                search_chunks: List[Dict[str, Any]] = []
                image_chunks: List[Dict[str, Any]] = []
                summary_chunks: List[Dict[str, Any]] = []
                ocr_chunks: List[Dict[str, Any]] = []
                ocr_texts: List[str] = []
                pdf_texts: List[str] = []

                for call in tool_calls:
                    result = self._run_tool_call(call, sender)
                    tool_name = (
                        _get(call, "name")
                        or _get(_get(call, "function", {}), "name")
                    )

                    if tool_name == "web_search":
                        search_chunks.append(result)
                    elif tool_name == "generate_image":
                        image_chunks.append(result)
                    elif tool_name == "summarize_url":
                        summary_chunks.append(result)
                    elif tool_name == "ocr_image":
                        ocr_chunks.append(result)
                    elif tool_name == "pdf_extract":
                        summary_chunks.append(result)
                    else:
                        search_chunks.append(result)

                # -------------------------------------------------------
                # Contexto de busca
                # -------------------------------------------------------
                flat = []
                for chunk in search_chunks:
                    flat.extend(chunk.get("results", []))
                bullets = format_search_bullets(flat, limit=SEARCH_MAX_RESULTS)

                search_context = (
                    "Contexto da busca:\n" + (bullets or "Nenhum resultado encontrado.")
                )

                # -------------------------------------------------------
                # Contexto de imagens geradas
                # -------------------------------------------------------
                image_context = ""
                if image_chunks:
                    lines = []
                    for c in image_chunks:
                        if c.get("error"):
                            lines.append(f"- Erro: {c['error']}")
                        else:
                            lines.append(
                                f"- Prompt: {c.get('prompt')} | Arquivos: {', '.join(c.get('files', []))}"
                            )
                    image_context = "Imagens geradas:\n" + "\n".join(lines)

                # -------------------------------------------------------
                # Contexto de resumos (URL + PDF)
                # -------------------------------------------------------
                summary_context = ""
                local_summaries: List[str] = []
                html_sections: List[str] = []

                for chunk in summary_chunks:
                    if chunk.get("error"):
                        summary_context += f"[Resumo-erro] {chunk.get('source')}: {chunk.get('error')}\n"
                        continue

                    if chunk.get("content"):
                        summary_context += (
                            f"[PDF] {chunk.get('source') or 'arquivo'}:\n"
                            f"{chunk.get('content')}\n"
                        )
                        if chunk.get("content"):
                            pdf_texts.append(chunk["content"])
                        continue

                    summary_context += (
                        f"- {chunk.get('title') or 'Sem título'} ({chunk.get('source')}) "
                        f"{(chunk.get('summary') or '')[:280]}...\n"
                    )

                    if chunk.get("local_summary"):
                        local_summaries.append(chunk["local_summary"])

                    if chunk.get("raw_html"):
                        html_sections.append(f"[HTML]\n{chunk.get('raw_html')}")

                summary_html_context = clamp_tool_context("\n\n".join(html_sections))

                # -------------------------------------------------------
                # Contexto de OCR
                # -------------------------------------------------------
                ocr_context = ""
                if ocr_chunks:
                    ocr_lines = []
                    for chunk in ocr_chunks:
                        if chunk.get("error"):
                            ocr_lines.append(f"[OCR-erro] {chunk['error']}")
                        else:
                            t = (chunk.get("ocr_text") or "").strip()
                            if t:
                                ocr_texts.append(t)
                            ocr_lines.append(
                                f"[OCR]\n{t}" if t else "[OCR] Nenhum texto encontrado."
                            )
                    ocr_context = "\n".join(ocr_lines)

                enriched_context = "\n".join(
                    p
                    for p in [
                        search_context,
                        image_context,
                        summary_context,
                        "\n".join(local_summaries),
                        summary_html_context,
                        ocr_context,
                    ]
                    if p
                )

                second_input = (
                    full_input
                    + "\n[Contexto das ferramentas]\n"
                    + enriched_context
                    + "\nAssistente:"
                )

                response2 = self.client.responses.create(
                    model=RESPONSES_MODEL_SECOND,
                    instructions=(
                        "Produza apenas a resposta final ao usuário com base no contexto fornecido. "
                        "Não execute ferramentas. "
                        "Não sugira ferramentas. "
                        "Não peça confirmação. "
                        "Não ofereça opções. "
                        "Use diretamente o conteúdo presente nos blocos de contexto "
                        "(OCR, PDF, buscas, resumos, HTML ou imagens). "
                        "Se houver bloco [OCR], ele é prioritário para extração textual. "
                        "Se houver conteúdo de PDF, trate como texto fornecido. "
                        "Se houver resultados de busca ou resumos, use-os para fundamentar a resposta. "
                        "Responda de forma objetiva, direta e completa, em até ~3000 tokens. "
                        "Nenhuma etapa adicional. Apenas a resposta final."
                    ),
                    input=second_input,
                    max_output_tokens=3000,
                    tools=[],
                    tool_choice="none",
                    metadata={"source": "evolution_agent"},
                    reasoning={"effort": "minimal"},
                )

                log_tool_calls(response2, "[Segunda rodada]")
                if _get(response2, "status") != "completed":
                    inc_details = _get(response2, "incomplete_details")
                    preview = extract_output_text(response2) or ""
                    if len(preview) > 300:
                        preview = preview[:300] + "..."
                    print(
                        f"[Segunda rodada] incomplete status={_get(response2, 'status')} "
                        f"details={inc_details} output_preview={preview}"
                    )
                final_response = response2

            else:
                final_response = response1

            # -----------------------------------------------------------
            # Extração do texto final
            # -----------------------------------------------------------
            if _get(final_response, "status") != "completed":
                # Se já temos texto extraído de PDF, devolve-o mesmo se o modelo falhar
                if pdf_texts:
                    reply_text = pdf_texts[0]
                elif ocr_texts:
                    reply_text = ocr_texts[0]
                else:
                    reply_text = "Desculpe, não consegui gerar uma resposta agora."
            else:
                reply_text = (
                    extract_output_text(final_response)
                    or "Desculpe, sem resposta no momento."
                )

            # -----------------------------------------------------------
            # Memória curta
            # -----------------------------------------------------------
            new_history = history + [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply_text},
            ]

            if len(new_history) > SESSION_MAX_STORED_TURNS:
                old_part = new_history[:-SESSION_RECENT_TURNS_FOR_PROMPT]
                recent_part = new_history[-SESSION_RECENT_TURNS_FOR_PROMPT:]

                old_text = self._format_turns(old_part)
                existing = self.session_summaries.get(sender, "")
                combined = self._summarize_history(existing, old_text)
                self.session_summaries[sender] = combined[:SESSION_SUMMARY_MAX_CHARS]
                self.conversations[sender] = recent_part
                self._redis_set_json(self._redis_key(sender, "conversation"), recent_part)
                self._redis_set_json(self._redis_key(sender, "summary"), self.session_summaries[sender])
            else:
                self.conversations[sender] = new_history
                self._redis_set_json(self._redis_key(sender, "conversation"), new_history)
                self._redis_set_json(self._redis_key(sender, "summary"), self.session_summaries.get(sender, ""))

            return reply_text

        except Exception:
            traceback.print_exc()
            return "Desculpe, tive um problema para gerar a resposta agora."

    # ---------------------------------------------------------------
    # WhatsApp event handler: engine assume TUDO
    # ---------------------------------------------------------------
    def handle_inbound_whatsapp(
        self,
        wa_event: Dict[str, Any],
        instance_name: str,
        send_text: Callable[[str, str, str], Any],
        send_media: Callable[[str, str, str, str], Any],
        media_downloader: Callable[[str, Dict[str, Any]], str],
    ) -> str:
        """
        Entrada única de eventos do Evolution API.
        - Detecta tipo de mensagem (texto, imagem, documento)
        - Baixa base64 quando necessário
        - Atualiza memória de mídia
        - Chama respond(...)
        - Envia a resposta via send_text
        - Retorna o texto da resposta (para log)
        """
        try:
            event = wa_event.get("event")
            if event != "messages.upsert":
                print("[handle_inbound_whatsapp] evento ignorado:", event)
                return ""

            data = wa_event.get("data") or {}
            msg = data.get("message") or {}
            key = data.get("key") or {}
            sender = key.get("remoteJid")

            if not sender:
                print("[handle_inbound_whatsapp] sender ausente")
                return ""

            # Reconstruir WebMessageInfo para o endpoint de mídia
            web_message_info = {
                "key": key,
                "message": msg,
                "pushName": data.get("pushName"),
                "messageTimestamp": data.get("messageTimestamp"),
                "instanceId": wa_event.get("instanceId") or wa_event.get("instance"),
                "source": wa_event.get("source", "web"),
            }

            image_b64: Optional[str] = None
            pdf_b64: Optional[str] = None
            user_message: str = ""

            # -----------------------------
            # IMAGEM
            # -----------------------------
            if "imageMessage" in msg:
                try:
                    image_b64 = msg.get("imageMessage", {}).get("base64") or media_downloader(
                        instance_name, web_message_info
                    )
                    caption = msg.get("imageMessage", {}).get("caption") or ""
                    base_text = "O usuário enviou uma imagem."
                    if caption:
                        base_text += f" Legenda: {caption}"
                    user_message = base_text
                except Exception as exc:
                    print("[handle_inbound_whatsapp] falha ao baixar imagem:", exc)
                    send_text(
                        sender,
                        "Não consegui baixar a imagem. Tente reenviar.",
                        instance_name,
                    )
                    return ""

            # -----------------------------
            # DOCUMENTO (PDF / outros)
            # -----------------------------
            elif "documentMessage" in msg:
                try:
                    mimetype = msg["documentMessage"].get("mimetype", "")
                    filename = msg["documentMessage"].get("fileName", "arquivo")

                    file_b64 = media_downloader(instance_name, web_message_info)
                    # só tratamos como PDF aqui; outros tipos podem ser adicionados depois
                    if "pdf" in mimetype.lower() or filename.lower().endswith(".pdf"):
                        pdf_b64 = file_b64
                        user_message = (
                            f"O usuário enviou um PDF ({filename}). "
                            "O arquivo está disponível internamente para extração."
                        )
                    else:
                        # por enquanto tratamos outros docs como texto genérico
                        user_message = (
                            f"O usuário enviou um documento ({filename}, {mimetype}). "
                            "No momento a extração automática está limitada a PDFs."
                        )
                except Exception as exc:
                    print("[handle_inbound_whatsapp] falha ao baixar documento:", exc)
                    send_text(
                        sender,
                        "Não consegui baixar o arquivo. Tente reenviar.",
                        instance_name,
                    )
                    return ""

            # -----------------------------
            # TEXTO NORMAL
            # -----------------------------
            else:
                user_message = (
                    msg.get("conversation")
                    or msg.get("text", {}).get("body")
                    or ""
                )

            # Atualiza memória de mídia
            self.ingest_media(sender, image_b64=image_b64, pdf_b64=pdf_b64)

            # Gera resposta principal
            reply_text = self.respond(
                sender=sender,
                user_message=user_message,
                instance_name=instance_name,
                image_b64=image_b64,
                pdf_b64=pdf_b64,
            )

            # Envia resposta via callback
            if reply_text:
                send_text(sender, reply_text, instance_name)

            return reply_text

        except Exception:
            traceback.print_exc()
            return ""
