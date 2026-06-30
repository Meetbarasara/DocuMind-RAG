import hashlib
from pathlib import PurePosixPath
from typing import Any, Dict, List

from src.logger import get_logger

logger = get_logger(__name__)


# ── Filename Safety ───────────────────────────────────────────────────────


def sanitize_filename(filename: str) -> str:
    """Reduce a client-supplied filename to a safe basename (SEC-2).

    Client filenames (multipart upload names, path params) are used to build
    local temp paths and Supabase storage keys. Without this, a value like
    "../../etc/passwd" or "..\\..\\evil.txt" escapes the intended directory.
    Normalizing backslashes before stripping handles Windows-style traversal
    too, since PurePosixPath only treats "/" as a separator.

    Raises:
        ValueError: if nothing safe remains (empty, ".", or "..").
    """
    normalized = (filename or "").replace("\\", "/")
    name = PurePosixPath(normalized).name
    if not name or name in (".", ".."):
        raise ValueError(f"Invalid filename: {filename!r}")
    return name


# ══════════════════════════════════════════════════════════════════════════════
#  Feature F: Conversation Memory Summarization
# ══════════════════════════════════════════════════════════════════════════════

# Module-level LRU-style cache for conversation summaries.
# Keyed by SHA-256 of the older messages — the LLM is called at most once
# per unique set of older turns.
_summary_cache: Dict[str, str] = {}


def _hash_messages(messages: list) -> str:
    """Stable hash of a list of message dicts (used as cache key).

    Logical Mistake #7 fix: this used to hash only "count + last 50 chars
    of the last message" -- two different conversations with the same
    number of messages and the same (or same-prefix) last message collided
    on the same cache key and got served each other's cached summary.
    Hash every message's role and full content instead, with a separator
    byte unlikely to appear in real text so e.g. role/content boundaries
    can't be shifted to produce a matching hash for different inputs.
    """
    if not messages:
        return ""
    raw = "\x1f".join(
        f"{m.get('role', '')}\x1f{m.get('content', '')}" for m in messages
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _summarize_older_messages(messages: list, llm: Any) -> str:
    """Summarize older conversation messages using an LLM (synchronous).

    Results are cached so the LLM is called at most once per unique set of
    older messages. Always call via ``_summarize_older_messages_async`` from
    async contexts to avoid blocking the event loop.

    Args:
        messages: The older messages to summarize.
        llm:      A LangChain LLM instance (e.g. ``ChatGroq``).

    Returns:
        A concise summary string, or ``""`` on failure.
    """
    if not messages:
        return ""

    cache_key = _hash_messages(messages)
    if cache_key in _summary_cache:
        logger.debug("Memory summary cache HIT")
        return _summary_cache[cache_key]

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
        summary = result.content if hasattr(result, "content") else str(result)
        summary = summary.strip()
        _summary_cache[cache_key] = summary
        logger.info(
            "Summarized %d older messages into %d chars", len(messages), len(summary)
        )
        return summary
    except Exception as e:
        logger.warning("Memory summarization failed, dropping older messages: %s", e)
        return ""


async def _summarize_older_messages_async(messages: list, llm: Any) -> str:
    """Async wrapper for memory summarization.

    Bug 2 fix: the synchronous LLM call was blocking the async event loop on
    every query with long history. Now runs via asyncio.to_thread so FastAPI's
    async workers remain free. The result is cached so the overhead is paid at
    most once per unique set of older messages.
    """
    import asyncio
    return await asyncio.to_thread(_summarize_older_messages, messages, llm)


# ── Chat History Formatting ───────────────────────────────────────────────


def _role_label(role: str) -> str:
    return "User" if role in ("user", "human") else "Assistant"


def _trim_lines_to_budget(lines: List[str], enc, max_tokens: int) -> List[str]:
    """Return the longest *suffix* of lines whose combined token count fits.

    BUG-14 fix: encodes each line exactly once instead of re-encoding the
    whole (shrinking) joined text on every iteration as lines were dropped
    from the front — the old loop redundantly re-encoded every surviving
    line again on every single iteration.
    """
    token_counts = [len(enc.encode(line)) for line in lines]
    total = sum(token_counts)
    start = 0
    while total > max_tokens and start < len(lines):
        total -= token_counts[start]
        start += 1
    return lines[start:]


def format_chat_history(
    chat_history: list,
    window: int = 6,
    max_tokens: int = 2000,
    llm: Any = None,
    use_summarization: bool = False,
) -> str:
    """Format the last *window* messages into a string for LLM prompt injection.

    Synchronous version — does NOT run memory summarization even when
    ``use_summarization=True`` (summarization is async; use
    ``format_chat_history_async`` from async endpoints for that).

    Args:
        chat_history:      List of ``{"role": ..., "content": ...}`` dicts.
        window:            Max number of recent messages to include (default 6).
        max_tokens:        Hard token ceiling; oldest messages dropped until under budget.
        llm:               Ignored (kept for API compatibility with async callers).
        use_summarization: Ignored (kept for API compatibility with async callers).

    Returns:
        Formatted string safe to inject into an LLM prompt.
    """
    if not chat_history:
        return ""

    recent = chat_history[-window:]

    def _fmt(msgs: list) -> str:
        return "\n".join(f"{_role_label(m['role'])}: {m['content'][:500]}" for m in msgs)

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")

        full_text = _fmt(recent)
        if len(enc.encode(full_text)) <= max_tokens:
            return full_text

        lines = [f"{_role_label(m['role'])}: {m['content'][:500]}" for m in recent]
        return "\n".join(_trim_lines_to_budget(lines, enc, max_tokens))
    except ImportError:
        return _fmt(recent)


async def format_chat_history_async(
    chat_history: list,
    window: int = 6,
    max_tokens: int = 2000,
    llm: Any = None,
    use_summarization: bool = False,
) -> str:
    """Async version of ``format_chat_history`` with memory summarization support.

    Bug 2 fix: when summarization is enabled the LLM call runs via
    ``asyncio.to_thread`` so it never blocks the event loop.

    Args:
        chat_history:      List of ``{"role": ..., "content": ...}`` dicts.
        window:            Max number of recent messages to include (default 6).
        max_tokens:        Hard token ceiling.
        llm:               LangChain LLM for summarizing older messages (Feature F).
        use_summarization: Enable conversation memory summarization (Feature F).

    Returns:
        Formatted string safe to inject into an LLM prompt.
    """
    if not chat_history:
        return ""

    # ── Feature F: Summarize older messages instead of dropping them ─────
    summary_prefix = ""
    if use_summarization and llm and len(chat_history) > window:
        older_messages = chat_history[:-window]
        summary = await _summarize_older_messages_async(older_messages, llm)
        if summary:
            summary_prefix = f"[Summary of earlier conversation]: {summary}\n\n"

    recent = chat_history[-window:]

    def _fmt(msgs: list) -> str:
        return "\n".join(f"{_role_label(m['role'])}: {m['content'][:500]}" for m in msgs)

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")

        full_text = summary_prefix + _fmt(recent)
        if len(enc.encode(full_text)) <= max_tokens:
            return full_text

        # Reserve room for the summary's own tokens, then fit as many
        # recent lines as possible in what's left — same priority as
        # before (protect the summary, drop messages first), just without
        # re-encoding the whole shrinking text on every iteration.
        summary_tokens = len(enc.encode(summary_prefix)) if summary_prefix else 0
        budget = max_tokens - summary_tokens
        lines = [f"{_role_label(m['role'])}: {m['content'][:500]}" for m in recent]
        kept = _trim_lines_to_budget(lines, enc, budget) if budget > 0 else []

        if kept:
            return summary_prefix + "\n".join(kept)
        return summary_prefix.strip() if summary_prefix else ""
    except ImportError:
        return summary_prefix + _fmt(recent)