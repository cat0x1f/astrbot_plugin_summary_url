from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .url_utils import fetch_html

_REDDIT_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "np.reddit.com",
    "redd.it",
}

_META_TAG_PATTERN = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_PATTERN = re.compile(
    r"([A-Za-z_:][A-Za-z0-9_:\-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s/>]+))",
    re.IGNORECASE | re.DOTALL,
)


class RedditParseError(RuntimeError):
    """Raised when a Reddit link cannot be parsed."""


@dataclass(eq=True, slots=True)
class RedditContext:
    url: str
    title: str
    description: str
    site_name: str
    image: str
    page_type: str
    images: List[str] = field(default_factory=list)


@dataclass(eq=True, slots=True)
class RedditPreparedPrompt:
    prompt: str
    images: List[str]
    context: RedditContext


def is_reddit_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return False
    return host in _REDDIT_HOSTS


def _extract_meta_property(html: str, target_name: str) -> str:
    target = target_name.strip().lower()
    for tag in _META_TAG_PATTERN.findall(html or ""):
        attrs: Dict[str, str] = {}
        for key, dq, sq, bare in _ATTR_PATTERN.findall(tag):
            attrs[key.lower()] = dq or sq or bare or ""
        name = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        if name != target:
            continue
        content = (attrs.get("content") or "").strip()
        if content:
            return content
    return ""


def _build_reddit_context(url: str, html: str) -> RedditContext:
    title = _extract_meta_property(html, "og:title")
    description = _extract_meta_property(html, "og:description")
    site_name = _extract_meta_property(html, "og:site_name")
    image = _extract_meta_property(html, "og:image")
    page_type = _extract_meta_property(html, "og:type")

    images: List[str] = []
    if image.startswith("http"):
        images.append(image)

    if not any([title, description, site_name, image, page_type]):
        raise RedditParseError("Reddit 页面未找到可用的 Open Graph 信息。")

    return RedditContext(
        url=url,
        title=title,
        description=description,
        site_name=site_name,
        image=image,
        page_type=page_type,
        images=images,
    )


def build_reddit_prompt(ctx: RedditContext) -> str:
    parts = ["请基于以下 Reddit 页面 Open Graph 信息解释页面内容：", f"链接：{ctx.url}"]
    if ctx.site_name:
        parts.append(f"站点：{ctx.site_name}")
    if ctx.page_type:
        parts.append(f"页面类型：{ctx.page_type}")
    if ctx.title:
        parts.append(f"标题：{ctx.title}")
    if ctx.description:
        parts.append(f"描述：\n{ctx.description}")
    if ctx.image:
        parts.append("Open Graph 图片：已提供给模型。")
    return "\n\n".join(part for part in parts if part).strip()


async def prepare_reddit_prompt(
    url: str,
    timeout_sec: int,
    last_fetch_info: Dict[str, Any],
) -> RedditPreparedPrompt:
    if not is_reddit_url(url):
        raise RedditParseError("未识别到受支持的 Reddit 链接。")

    html = await fetch_html(url, timeout_sec, last_fetch_info)
    if not html:
        raise RedditParseError("Reddit 页面获取失败，请确认链接可访问或稍后重试。")

    ctx = _build_reddit_context(url, html)
    return RedditPreparedPrompt(
        prompt=build_reddit_prompt(ctx),
        images=ctx.images,
        context=ctx,
    )
