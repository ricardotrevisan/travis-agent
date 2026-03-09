import base64
import io
import os
from typing import Any, Dict, Optional

import requests
from pypdf import PdfReader

from .base import Tool


PDF_MAX_CHARS = int(os.getenv("PDF_MAX_CHARS", "2000"))

_pdf_reader_initialized = False


def truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


# -------------------------------
#  PDF Reader utilitário
# -------------------------------
class PDFReaderTool:
    """Extrai texto de PDFs a partir de URL, bytes ou arquivo local."""

    @staticmethod
    def fetch_from_url(url: str) -> Optional[bytes]:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None

    @staticmethod
    def extract_text(pdf_bytes: bytes) -> str:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            return "\n".join(pages)
        except Exception:
            return ""


# reader global seguindo seu padrão
_pdf_reader: Optional[PDFReaderTool] = None


def get_pdf_reader() -> PDFReaderTool:
    global _pdf_reader, _pdf_reader_initialized
    if _pdf_reader_initialized and _pdf_reader:
        return _pdf_reader

    _pdf_reader = PDFReaderTool()
    _pdf_reader_initialized = True
    return _pdf_reader


# -------------------------------
#  Serviço de extração
# -------------------------------
def _read_from_b64(source_b64: str) -> Optional[bytes]:
    try:
        cleaned = source_b64.strip()
        if cleaned.startswith("data:"):
            # data URLs no formato data:application/pdf;base64,<conteúdo>
            cleaned = cleaned.split("base64,", 1)[-1]
        return base64.b64decode(cleaned)
    except Exception as exc:
        print(f"[pdf_extract] falha ao decodificar base64: {exc}")
        return None


def run_pdf_extract(source: str, source_b64: Optional[str] = None) -> Dict[str, Any]:
    reader = get_pdf_reader()

    # Base64 já disponível (ex.: PDF enviado no WhatsApp)
    if source_b64:
        pdf_bytes = _read_from_b64(source_b64)
        if not pdf_bytes:
            return {"source": source or "internal", "error": "PDF base64 inválido."}

        text = reader.extract_text(pdf_bytes)
        return {
            "source": source or "internal",
            "content": truncate(text, PDF_MAX_CHARS),
        }

    # Se for URL
    if source.startswith("http://") or source.startswith("https://"):
        pdf_bytes = reader.fetch_from_url(source)
        if not pdf_bytes:
            return {"source": source, "error": "Falha ao baixar ou detectar PDF."}

        text = reader.extract_text(pdf_bytes)
        return {
            "source": source,
            "content": truncate(text, PDF_MAX_CHARS),
        }

    # Se for arquivo local
    if os.path.exists(source):
        try:
            with open(source, "rb") as f:
                pdf_bytes = f.read()
        except Exception:
            return {"source": source, "error": "Falha ao ler arquivo local."}

        text = reader.extract_text(pdf_bytes)
        return {
            "source": source,
            "content": truncate(text, PDF_MAX_CHARS),
        }

    return {"source": source, "error": "Fonte inválida. Use URL ou caminho local."}


# -------------------------------
#  Tool class
# -------------------------------
class PDFExtractorTool(Tool):
    name = "pdf_extract"
    description = "Extrai texto de um PDF (via URL ou arquivo local) e retorna trecho limitado."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "URL do PDF ou caminho local.",
            },
            "source_b64": {
                "type": "string",
                "description": "Conteúdo do PDF em base64 (usado quando o arquivo já foi recebido).",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def run(self, args: Dict[str, Any]) -> Any:
        source = args.get("source") or ""
        source_b64 = args.get("source_b64") or None
        return run_pdf_extract(source, source_b64=source_b64)
