import hashlib
from collections import Counter
from typing import Any, Dict, List, Optional

from src.logger import get_logger

logger = get_logger(__name__)

# ── Element Introspection Helpers ──────────────────────────────────────────


def _get_element_type(el) -> str:
    """Return the element's category/type as a string."""
    for attr in ("category", "type"):
        value = getattr(el, attr, None)
        if value:
            return str(value)
    return type(el).__name__


def _get_page_number(el) -> Optional[int]:
    """Extract page_number from an element's metadata, if present."""
    meta = getattr(el, "metadata", None)
    return getattr(meta, "page_number", None) if meta else None


def _element_has_image_payload(el) -> bool:
    """Check if the element carries an image (base64 or file path)."""
    meta = getattr(el, "metadata", None)
    if not meta:
        return False
    return bool(getattr(meta, "image_base64", None) or getattr(meta, "image_path", None))


def _table_html(el) -> Optional[str]:
    """Return the HTML representation of a table element, if available."""
    meta = getattr(el, "metadata", None)
    return getattr(meta, "text_as_html", None) if meta else None


def _get_metadata_fields(el, fields: list) -> dict:
    """Safely extract multiple metadata attributes into a dict.

    Args:
        el: An unstructured element with an optional `.metadata` object.
        fields: List of attribute names to extract.

    Returns:
        Dict mapping each field name to its value (or None if absent).
    """
    meta = getattr(el, "metadata", None)
    if not meta:
        return {f: None for f in fields}
    return {f: getattr(meta, f, None) for f in fields}


# ── ID Generation ─────────────────────────────────────────────────────────


def _stable_id(file_path: str, chunk_type: str, index: int, text: str) -> str:
    """Generate a deterministic SHA-1 chunk ID from its key attributes."""
    raw = f"{file_path}::{chunk_type}::{index}::{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


# ── Logging / Analysis ────────────────────────────────────────────────────


def _log_elements_analysis(elements: List) -> None:
    """Print a frequency breakdown of element types."""
    counts = Counter(_get_element_type(el) for el in elements)
    print(f"Element breakdown: {dict(counts)}")


# ── Content Description Builders ──────────────────────────────────────────


def _create_table_description(el) -> str:
    """Build a short text summary of a table element."""
    try:
        table_text = el.text or ""
        preview_lines = table_text.split("\n")[:5]
        summary = " ".join(line.strip() for line in preview_lines if line.strip())
        return f"Table containing: {summary[:200]}..."
    except Exception:
        return "Table data extracted from document."


def _create_image_description(el, page_num=None) -> str:
    """Build a text description for an image element."""
    try:
        meta = getattr(el, "metadata", None)
        alt_text = getattr(meta, "alt_text", None) if meta else None
        caption = getattr(meta, "caption", None) if meta else None

        if alt_text or caption:
            return alt_text or caption

        page_info = f" on page {page_num}" if page_num else ""
        return f"Image content{page_info}. Contains visual information related to document content."
    except Exception:
        return "Image content extracted from document."


# ── Feature F: Conversation Memory Summarization ─────────────────────────

# Module-level cache for conversation summaries.
# Keyed by hash of the older messages to avoid redundant LLM calls.
# Replace the whole _summary_cache approach with this:
_summary_cache: Dict[str, str] = {}

def _hash_messages(messages: list) -> str:
    # Key by count + last message ID, not full content — 
    # same older messages + new message should still hit the cache
    if not messages:
        return ""
    raw = f"count:{len(messages)}::last:{messages[-1].get('content','')[:50]}"
    return hashlib.sha256(raw.encode()).hexdigest()

def _summarize_older_messages(messages: list, llm: Any) -> str:
    """Summarize older conversation messages using an LLM.

    Results are cached so the LLM is only called once per unique
    set of older messages (~1 call every ``window`` new messages).

    Args:
        messages: The older messages to summarize.
        llm: A LangChain LLM instance (e.g. ``ChatOpenAI``).

    Returns:
        A concise summary string of the conversation so far.
    """
    if not messages:
        return ""

    cache_key = _hash_messages(messages)

    # Return cached summary if available (0ms latency on cache hit)
    if cache_key in _summary_cache:
        logger.debug("Memory summary cache HIT")
        return _summary_cache[cache_key]

    # Format older messages for the summarization prompt
    formatted = "\n".join(
        f"{'User' if m.get('role') in ('user', 'human') else 'Assistant'}: "
        f"{m.get('content', '')[:500]}"
        for m in messages
    )

    try:
        from langchain_core.prompts import ChatPromptTemplate

        summary_prompt = ChatPromptTemplate.from_messages([
            ("system",
             "Summarize the following conversation in 2-3 concise sentences. "
             "Preserve key topics, questions asked, and important facts mentioned. "
             "Do NOT include greetings or filler."),
            ("user", "{conversation}"),
        ])

        chain = summary_prompt | llm
        result = chain.invoke({"conversation": formatted})

        # Handle both string and AIMessage return types
        summary = result.content if hasattr(result, "content") else str(result)
        summary = summary.strip()

        # Cache the result
        _summary_cache[cache_key] = summary
        logger.info(
            "Summarized %d older messages into %d chars",
            len(messages), len(summary),
        )
        return summary

    except Exception as e:
        logger.warning("Memory summarization failed, dropping older messages: %s", e)
        return ""


# ── Chat History Formatting ───────────────────────────────────────────────


def format_chat_history(
    chat_history: list,
    window: int = 6,
    max_tokens: int = 2000,
    llm: Any = None,
    use_summarization: bool = False,
) -> str:
    """Format the last *window* messages into a string for LLM prompt injection.

    When *use_summarization* is True and there are more messages than *window*,
    older messages are summarized via LLM (Feature F) and prepended, rather
    than being silently dropped.

    Args:
        chat_history: List of {"role": ..., "content": ...} dicts.
        window: Maximum number of recent messages to retain (default 6).
        max_tokens: Hard token ceiling — oldest messages are dropped until under budget.
        llm: Optional LangChain LLM for summarizing older messages (Feature F).
        use_summarization: Enable conversation memory summarization.

    Returns:
        Formatted string safe to inject into an LLM prompt.
    """
    if not chat_history:
        return ""

    # ── Feature F: Summarize older messages instead of dropping them ──
    summary_prefix = ""
    if use_summarization and llm and len(chat_history) > window:
        older_messages = chat_history[:-window]
        summary = _summarize_older_messages(older_messages, llm)
        if summary:
            summary_prefix = f"[Summary of earlier conversation]: {summary}\n\n"

    recent = chat_history[-window:]

    def _format_messages(messages: list) -> str:
        """Convert a list of message dicts into a single formatted string."""
        lines = []
        for msg in messages:
            role = "User" if msg["role"] in ("user", "human") else "Assistant"
            lines.append(f"{role}: {msg['content'][:500]}")
        return "\n".join(lines)

    # Try tiktoken-based truncation; fall back to character-based
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")

        while recent:
            text = summary_prefix + _format_messages(recent)
            if len(enc.encode(text)) <= max_tokens:
                return text
            recent = recent[1:]  # drop oldest message

        return summary_prefix.strip() if summary_prefix else ""
    except ImportError:
        return summary_prefix + _format_messages(recent)