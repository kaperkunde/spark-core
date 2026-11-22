"""Text sizing for the mesh side. Limits come from the HostLink contract."""

from __future__ import annotations

import hashlib

ROOM_POST_MAX = 151  # bytes, including the "[name] " tag
PUBLIC_MAX = 160  # bytes on air, including the "<node name>: " prefix


def clean(text: str) -> str:
    """Collapse whitespace/newlines: mesh text is a single line."""
    return " ".join(text.split())


def truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut text to at most max_bytes of UTF-8 on a character boundary."""
    if max_bytes <= 0:
        return "", bool(text)
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[:max_bytes]
    # drop a partial trailing character
    return cut.decode("utf-8", errors="ignore"), True


def public_budget(node_name: str) -> int:
    """Bytes available for the text itself on Public (node prepends "<name>: ")."""
    return PUBLIC_MAX - len(f"{node_name}: ".encode("utf-8"))


def tag(name: str, text: str, use_prefix: bool) -> str:
    return f"[{name}] {text}" if use_prefix else text


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def split_author(text: str, default: str = "mesh") -> tuple[str, str]:
    """Public messages look like "<sender>: <message>"."""
    head, sep, tail = text.partition(": ")
    if sep and head and len(head) <= 64:
        return head, tail
    return default, text
