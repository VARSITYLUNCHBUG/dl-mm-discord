"""The GIFs on a finished draft, from presets/draft_gifs.json. Edit that file to change them."""

from __future__ import annotations

import json
import logging
from functools import cache

from django.conf import settings

logger = logging.getLogger(__name__)


@cache
def draft_gifs() -> tuple[str, ...]:
    """Every GIF URL in the file. A missing or broken file means no GIF, never an error."""
    path = settings.BASE_DIR / "presets" / "draft_gifs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError):
        logger.exception("Could not read the draft GIF list", extra={"path": str(path)})
        return ()
    raw = data.get("gifs", []) if isinstance(data, dict) else data
    return tuple(url for url in raw if isinstance(url, str) and url.startswith("https://"))
