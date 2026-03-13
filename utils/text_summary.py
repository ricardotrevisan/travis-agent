import re
from typing import List, Dict

# -------------------------
# Sentence splitting
# -------------------------
def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


# -------------------------
# Word frequency
# -------------------------
def build_word_freq(text: str) -> Dict[str, int]:
    stopwords = {
        "a", "o", "e", "é", "de", "da", "do", "em",
        "para", "um", "uma", "as", "os", "por", "com",
        "que", "se", "no", "na", "nos", "nas", "ao",
        "aos", "à", "às"
    }
    freq: Dict[str, int] = {}

    for word in re.findall(r"\b\w+\b", text.lower()):
        if len(word) <= 2 or word in stopwords:
            continue
        freq[word] = freq.get(word, 0) + 1

    return freq


# -------------------------
# Generic text summarizer
# -------------------------
def summarize_text_locally(
    text: str,
    max_sentences: int = 6,
    max_chars: int = 1200
) -> str:

    sentences = split_sentences(text)
    if not sentences:
        return ""

    freq = build_word_freq(text)

    scored = []
    for sent in sentences:
        score = sum(freq.get(w, 0) for w in re.findall(r"\b\w+\b", sent.lower()))
        scored.append((score, sent))

    # Remove duplicações lógicas
    scored_sorted = sorted(scored, key=lambda x: x[0], reverse=True)

    selected = [s for _, s in scored_sorted[:max_sentences * 2]]

    summary_parts = []
    for sent in sentences:
        if sent in selected:
            summary_parts.append(sent)

        if len(" ".join(summary_parts)) >= max_chars:
            break

        if len(summary_parts) >= max_sentences:
            break

    summary = " ".join(summary_parts)
    return summary[:max_chars]
