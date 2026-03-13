import os
import base64
import openai
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

class ImageGenerator:
    def __init__(self, api_key: str = None, save_path: str = "images"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("API key não fornecida. Defina OPENAI_API_KEY ou passe no construtor.")
        openai.api_key = self.api_key
        self.image_model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")

        self.save_path = save_path
        os.makedirs(self.save_path, exist_ok=True)

    def generate_image(self, prompt: str, size: str = "1024x1024", n: int = 1, model: str | None = None) -> list[str]:
        """
        Gera imagens a partir do prompt e salva localmente.
        Parâmetros:
            prompt: Prompt de texto para gerar a imagem.
            size: Tamanho da imagem (ex: "1024x1024").
            n: Número de imagens a gerar.
            model: Modelo a ser utilizado (ex: "gpt-image-1").
        """
        chosen_model = model or self.image_model
        response = openai.images.generate(
            model=chosen_model,
            prompt=prompt,
            size=size,
            n=n
        )

        saved_files = []
        for idx, img in enumerate(response.data, 1):
            # Decodifica a imagem do Base64
            image_data = base64.b64decode(img.b64_json)
            file_path = os.path.join(self.save_path, f"image_{idx}_{datetime.now().strftime('%Y%m%d')}.png")
            with open(file_path, "wb") as f:
                f.write(image_data)
            saved_files.append(file_path)
        return saved_files
