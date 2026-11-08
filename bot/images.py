"""Screenshot validation. Discord's upload filter checks only the extension, so check the bytes."""

from __future__ import annotations

import hashlib

# Discord's default per-file upload limit, documented on 3 September 2026.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def detect_image(data: bytes) -> str | None:
    """Return the file extension if these bytes are an image we accept."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
