from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from .url_utils import (
    build_url_user_prompt,
    fetch_html,
)

_COOLAPK_HOSTS = {
    "coolapk.com",
    "www.coolapk.com",
    "m.coolapk.com",
}
_COOLAPK_MIRROR_HOST = "www.coolapk1s.com"


def is_coolapk_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return False
    return host in _COOLAPK_HOSTS


def to_coolapk_mirror_url(url: str) -> str:
    target = str(url or "").strip()
    if not is_coolapk_url(target):
        return target
    parsed = urlparse(target)
    if not parsed.scheme or not parsed.netloc:
        return target
    return urlunparse(
        (
            "https",
            _COOLAPK_MIRROR_HOST,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


async def prepare_coolapk_prompt(
    url: str,
    timeout_sec: int,
    last_fetch_info: Dict[str, Any],
    *,
    max_chars: int,
    user_prompt_template: str,
) -> Optional[Tuple[str, Optional[str], List[str]]]:
    mirror_url = to_coolapk_mirror_url(url)
    html = await fetch_html(mirror_url, timeout_sec, last_fetch_info)
    if not html:
        return None

    if isinstance(last_fetch_info, dict):
        last_fetch_info["source_url"] = url
        last_fetch_info["mirror_url"] = mirror_url

    user_prompt, _title = build_url_user_prompt(
        url, html, max_chars, user_prompt_template
    )
    return (user_prompt, None, [])
