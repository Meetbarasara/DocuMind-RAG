import hashlib
from typing import List, Optional
from collections import Counter


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


# ── Chat History Formatting ───────────────────────────────────────────────


def format_chat_history(chat_history: list, window: int = 6, max_tokens: int = 2000) -> str:
    """Format the last *window* messages into a string for LLM prompt injection.

    Args:
        chat_history: List of {"role": ..., "content": ...} dicts.
        window: Maximum number of messages to retain (default 6).
        max_tokens: Hard token ceiling — oldest messages are dropped until under budget.

    Returns:
        Formatted string safe to inject into an LLM prompt.
    """
    if not chat_history:
        return ""

    recent = chat_history[-window:]

    def _format_messages(messages: list) -> str:
        """Convert a list of message dicts into a single formatted string."""
        lines = []
        for msg in messages:
            role = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{role}: {msg['content'][:500]}")
        return "\n".join(lines)

    # Try tiktoken-based truncation; fall back to character-based
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")

        while recent:
            text = _format_messages(recent)
            if len(enc.encode(text)) <= max_tokens:
                return text
            recent = recent[1:]  # drop oldest message

        return ""
    except ImportError:
        return _format_messages(recent)