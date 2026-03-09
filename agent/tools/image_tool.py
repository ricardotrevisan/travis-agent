from typing import Any, Dict, Optional

from .base import Tool
from utils.image_generator import ImageGenerator

_image_generator: Optional[ImageGenerator] = None


def get_image_generator() -> Optional[ImageGenerator]:
    global _image_generator
    if _image_generator:
        return _image_generator
    try:
        _image_generator = ImageGenerator()
        return _image_generator
    except Exception as exc:
        print("[image] erro ao inicializar ImageGenerator:", exc)
        return None


class GenerateImageTool(Tool):
    name = "generate_image"
    description = "Gerar imagens com o modelo de imagens."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Descrição da imagem a ser gerada.",
            },
            "size": {
                "type": "string",
                "description": "Dimensão quadrada desejada (ex: 1024x1024, 512x512).",
                "default": "1024x1024",
            },
            "n": {
                "type": "integer",
                "description": "Quantidade de imagens (1-4).",
                "minimum": 1,
                "maximum": 4,
                "default": 1,
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }

    def run(self, args: Dict[str, Any]) -> Any:
        prompt = args.get("prompt") or ""
        size = args.get("size") or "1024x1024"
        n = int(args.get("n") or 1)
        n = max(1, min(n, 4))

        generator = get_image_generator()
        if not generator:
            return {
                "error": "Gerador de imagem indisponível (verifique OPENAI_API_KEY).",
                "prompt": prompt,
            }

        try:
            files = generator.generate_image(prompt=prompt, size=size, n=n)
            return {
                "prompt": prompt,
                "size": size,
                "count": len(files),
                "files": files,
            }
        except Exception as exc:
            print("[image] erro ao gerar imagem:", exc)
            return {
                "error": f"Falha ao gerar imagem: {exc}",
                "prompt": prompt,
            }
