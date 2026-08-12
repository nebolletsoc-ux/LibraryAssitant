from __future__ import annotations

from typing import Any

from library.oakland import search as oakland_search


def search_availability(title: str, author: str, timeout: int = 12) -> list[dict[str, Any]]:
    return oakland_search(title, author, timeout=timeout)
