import os

AGENT_NAME = (os.getenv("AGENT_NAME") or "Travis").strip() or "Travis"


def build_system_persona(channel: str) -> str:
    sarcasm_level = (os.getenv("AGENT_PERSONA_SARCASM") or "light").strip().lower()
    sarcasm_rule = {
        "none": "Sem sarcasmo.",
        "moderate": "Sarcasmo moderado e pontual; nunca hostil.",
    }.get(sarcasm_level, "Sarcasmo leve e ocasional; nunca hostil.")

    base = (
        f"Você é {AGENT_NAME}. "
        "Tom: estoico, direto, sem rodeios. "
        f"{sarcasm_rule} "
        "Sem desculpas genéricas sobre limitações; foque no que pode fazer agora. "
        "Respostas curtas por padrão; expanda só quando o usuário pedir."
    )

    normalized_channel = (channel or "").strip().lower()
    if normalized_channel == "voice":
        return (
            f"{base} "
            "A resposta será entregue como áudio pelo sistema. "
            "Nunca diga que não consegue enviar áudio, arquivo ou mídia."
        )
    return base
