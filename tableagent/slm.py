"""Gemma SLM: summarize, classify_by_values, adapt_sql, translate."""
from __future__ import annotations
from pathlib import Path

_llm = None
_translate_cache: dict[tuple[str, str], str] = {}


def load_model(model_path: str | None = None):
    """Load Gemma via llama-cpp-python. Optional — app works without it."""
    global _llm
    if model_path and Path(model_path).exists():
        try:
            from llama_cpp import Llama
            _llm = Llama(model_path=model_path, n_ctx=1024, n_threads=4, verbose=False)
        except Exception as e:
            print(f"SLM load failed: {e}")
            _llm = None


def unload_model():
    global _llm
    _llm = None


def _generate(prompt: str, max_tokens: int = 256) -> str:
    if _llm is None:
        return ""
    try:
        out = _llm(prompt, max_tokens=max_tokens, stop=["\n\n", "</s>"])
        return out["choices"][0]["text"].strip()
    except Exception:
        return ""


def summarize(text: str) -> str:
    prompt = f"Summarize this data analysis result in one sentence:\n{text}\nSummary:"
    return _generate(prompt, 128)


def classify_by_values(values: list[str], categories: list[str]) -> str:
    vals = ", ".join(values[:10])
    cats = ", ".join(categories)
    prompt = f"Given these values: [{vals}]\nClassify into one of: [{cats}]\nCategory:"
    return _generate(prompt, 32)


def adapt_sql(original_sql: str, template_question: str, user_question: str) -> str:
    """Disabled: 270M model too small for reliable SQL generation."""
    return original_sql


def ask_yes_no(prompt: str) -> bool:
    """Ask SLM a yes/no question. Returns True for yes."""
    if _llm is None:
        return False
    answer = _generate(prompt + "\nAnswer yes or no:", 8).strip().lower()
    return any(w in answer for w in ("yes", "sim", "sí"))


def beam_refine(user_question: str, candidates: list, table_name: str,
                columns: list[str], text_col: str | None = None,
                amount_col: str | None = None) -> dict | None:
    """Use SLM yes/no to refine the top classifier match (not to pick candidates).
    
    candidates: list of (Question, filled_sql, score) — already ranked by classifier
    Returns: {sql, question_id, refinements: list[str]} or None
    """
    if _llm is None or not candidates:
        return None

    # Trust classifier ranking — use top candidate
    q, sql = candidates[0][0], candidates[0][1]
    refinements = []
    upper = sql.upper()

    # Refinement 1: Exclude summary rows?
    if text_col and ask_yes_no(
        f"The data has rows like 'Total Materiais Principais' (summary rows).\n"
        f"User asked: \"{user_question}\"\n"
        f"Should summary/total rows be excluded?"):
        clause = f'"{text_col}" NOT LIKE \'%Total%\' AND "{text_col}" NOT LIKE \'%Subtotal%\''
        if ' WHERE ' in upper:
            sql = sql.replace(' WHERE ', f' WHERE {clause} AND ', 1)
        elif ' GROUP BY ' in upper:
            sql = sql.replace(' GROUP BY ', f' WHERE {clause} GROUP BY ', 1)
        elif ' ORDER BY ' in upper:
            sql = sql.replace(' ORDER BY ', f' WHERE {clause} ORDER BY ', 1)
        else:
            sql += f' WHERE {clause}'
        refinements.append("exclude_totals")
        upper = sql.upper()

    # Refinement 2: Single result or ranking?
    if 'LIMIT' not in upper:
        if ask_yes_no(f"User: \"{user_question}\"\nDoes the user want exactly ONE result (not a list)?"):
            sql += ' LIMIT 1'
            refinements.append("limit_1")

    return {"sql": sql, "question_id": q.question_id, "question_text": q.text, "refinements": refinements}


def translate(text: str, lang: str = "pt-BR") -> str:
    """Translate text to target language. Cached, SLM-based."""
    if not text or lang == "en":
        return text
    key = (text, lang)
    if key in _translate_cache:
        return _translate_cache[key]
    if _llm is None:
        return text  # fallback: return original
    prompt = f"Translate to {lang}. Return only the translation:\n{text}\nTranslation:"
    result = _generate(prompt, 128)
    if result:
        _translate_cache[key] = result
        return result
    return text


def extract_entity(question: str) -> str:
    """Extract the entity/filter the user is asking about. ~25 token prompt."""
    if _llm is None:
        return ""
    prompt = f"Q: {question}\nEntity mentioned (or NONE):"
    return _generate(prompt, 40)


def narrate(user_question: str, columns: list[str], data: list[list], max_rows: int = 10) -> str:
    """Generate a brief PT-BR narrative about query results."""
    if _llm is None or not data:
        return ""
    # Build compact data representation
    header = "|".join(columns)
    rows = "\n".join("|".join(str(v) if v is not None else "" for v in r) for r in data[:max_rows])
    prompt = (
        f"{header}\n{rows}\n\n"
        f"User: {user_question}\n"
        f"Answer briefly in Portuguese based on the data above:\n"
    )
    result = _generate(prompt, 150)
    return result if result and len(result) > 10 else ""


def get_translate_cache() -> dict:
    """Serializable format for persistence."""
    return {f"{t}||{l}": v for (t, l), v in _translate_cache.items()}


def set_translate_cache(cache: dict):
    """Restore from serialized format."""
    global _translate_cache
    _translate_cache = {}
    for k, v in cache.items():
        parts = k.split("||", 1)
        if len(parts) == 2:
            _translate_cache[(parts[0], parts[1])] = v
