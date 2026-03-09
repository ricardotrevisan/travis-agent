import os
import base64
from typing import Any, Dict

from PIL import Image
from io import BytesIO

from .base import Tool
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_BETA_HEADER = os.getenv("OPENAI_BETA_HEADER", "responses-2024-12-17")
OPENAI_OCR_MODEL = os.getenv("OPENAI_OCR_MODEL", "gpt-4.1")

client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL,
    default_headers={"x-openai-beta": OPENAI_BETA_HEADER},
)


def _encode_as_data_url(path: str) -> str:
    """Converte a imagem local para JPEG + Base64 + data URL (sempre funciona)."""
    img = Image.open(path).convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def _extract_mime_and_payload(raw: str) -> tuple[str, str]:
    """
    Aceita data URL ou apenas o Base64 cru.
    Retorna (mime, payload_base64).
    """
    if not raw:
        return "image/jpeg", ""

    cleaned = "".join(raw.strip().split())
    if cleaned.startswith("data:") and ";base64," in cleaned:
        try:
            header, payload = cleaned.split(",", 1)
            mime = header.split("data:")[1].split(";")[0] or "image/jpeg"
            return mime, "".join(payload.strip().split())
        except Exception:
            return "image/jpeg", cleaned
    return "image/jpeg", cleaned


class OCRImageTool(Tool):
    name = "ocr_image"
    description = "Extrair texto de uma imagem usando OpenAI Vision."
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Caminho local da imagem salva pelo agente."
            },
            "image_b64": {
                "type": "string",
                "description": "Imagem em Base64 (sem cabeçalho data URL)."
            }
        },
        "required": [],
    }

    def run(self, args: Dict[str, Any]) -> Any:
        image_path = args.get("image_path")
        image_b64 = args.get("image_b64")

        if image_b64:
            image_b64 = image_b64.strip()
        
        if not image_path and not image_b64:
            return {"error": "Informe image_path ou image_b64."}

        if not image_b64 and image_path and not os.path.exists(image_path):
            return {"error": f"Arquivo não encontrado: {image_path}"}

        b64_len = len(image_b64) if image_b64 else 0
        print(f"[OCR] Convertendo imagem (b64_len={b64_len}) para data URL...")

        try:
            if image_b64:
                mime, payload = _extract_mime_and_payload(image_b64)
                # valida base64
                try:
                    base64.b64decode(payload, validate=True)
                except Exception as exc:
                    return {"error": f"Base64 inválido para OCR: {exc}"}
                data_url = f"data:{mime};base64,{payload}"
            else:
                data_url = _encode_as_data_url(image_path)

            response = client.responses.create(
                model=OPENAI_OCR_MODEL,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Extraia todo o texto visível da imagem. Apenas transcreva."
                            },
                            {
                                "type": "input_image",
                                "image_url": data_url
                            }
                        ]
                    }
                ],
                max_output_tokens=2048,
            )

            text = (getattr(response, "output_text", "") or "").strip()
            if not text:
                return {"error": "OCR retornou vazio."}

            return {"ocr_text": text}

        except Exception as exc:
            print(f"[OCR] ERRO: {exc}")
            return {"error": f"OCR falhou: {exc}"}        
