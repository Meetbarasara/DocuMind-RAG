import os
import sys
from typing import List, Dict, Any, Optional
import hashlib

def _stable_id(file_path: str, chunk_type: str, index: int, text: str) -> str:
    
    raw = f"{file_path}::{chunk_type}::{index}::{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()

def _get_element_type(el) -> str:
    
    if hasattr(el, "category") and el.category:
        return str(el.category)
    if hasattr(el, "type") and el.type:
        return str(el.type)
    
    return type(el).__name__
def _get_page_number(el) -> Optional[int]:
   
    if hasattr(el, "metadata") and el.metadata:
        return getattr(el.metadata, "page_number", None)
    return None

def _element_has_image_payload(el) -> bool:
   
    if not hasattr(el, "metadata") or not el.metadata:
        return False
    image_base64 = getattr(el.metadata, "image_base64", None)
    image_path = getattr(el.metadata, "image_path", None)
    return bool(image_base64 or image_path)

def _table_html(el) -> Optional[str]:
    
    if not hasattr(el, "metadata") or not el.metadata:
        return None
    return getattr(el.metadata, "text_as_html", None)

def _log_elements_analysis(elements: List) -> None:
    element_types = {}
    for el in elements:
        el_type = _get_element_type(el)
        element_types[el_type] = element_types.get(el_type, 0) + 1

    print(f"Element breakdown: {element_types}")

def _create_table_description(el) -> str:
        try:
            table_text = el.text or ""
            lines = table_text.split('\n')[:5]
            summary = " ".join([line.strip() for line in lines if line.strip()])
            return f"Table containing: {summary[:200]}..."
        except Exception:
            return "Table data extracted from document."

def _create_image_description(el, page_num=None) -> str:
    try:
            
        alt_text = getattr(el.metadata, 'alt_text', None) if hasattr(el, 'metadata') else None
        caption = getattr(el.metadata, 'caption', None) if hasattr(el, 'metadata') else None
            
        if alt_text or caption:
            return alt_text or caption
            
        page_info = f" on page {page_num}" if page_num else ""
        return f"Image content{page_info}. Contains visual information related to document content."
    except Exception:
        return "Image content extracted from document."        

        
def format_chat_history(chat_history: list, window: int = 6, max_tokens: int = 2000) -> str:
    """Format last N messages into a string for LLM prompt injection.

    Args:
        chat_history: List of {"role": ..., "content": ...} dicts.
        window: Maximum number of messages to retain (default 6 = 3 user + 3 assistant turns).
        max_tokens: Hard token ceiling — oldest messages are dropped until under budget.

    Returns:
        Formatted string safe to inject into an LLM prompt.
    """
    if not chat_history:
        return ""

    # Step 1: Keep only the most recent `window` messages
    recent = chat_history[-window:]

    # Step 2: Attempt tiktoken truncation to stay under max_tokens
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")  # works for GPT-4 / GPT-4o-mini

        while recent:
            lines = []
            for msg in recent:
                role = "User" if msg["role"] == "user" else "Assistant"
                # Truncate individual message content to 500 chars to avoid runaway context
                content = msg["content"][:500]
                lines.append(f"{role}: {content}")
            text = "\n".join(lines)
            token_count = len(enc.encode(text))
            if token_count <= max_tokens:
                return text
            # Drop the oldest message and retry
            recent = recent[1:]

        return ""  # all messages exceeded the budget
    except ImportError:
        # tiktoken not installed — fall back to character-based truncation
        lines = []
        for msg in recent:
            role = "User" if msg["role"] == "user" else "Assistant"
            content = msg["content"][:500]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)