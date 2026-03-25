from __future__ import annotations

import re
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List
from urllib.parse import quote, urlparse

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:
    aiohttp = None

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
_REDDIT_OEMBED_API = "https://www.reddit.com/oembed?url={url}&format=json"


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
    author_name: str = ""
    provider_name: str = ""
    provider_url: str = ""
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


async def _fetch_reddit_oembed_json(url: str, timeout_sec: int) -> Dict[str, Any]:
    api_url = _REDDIT_OEMBED_API.format(url=quote(url, safe=""))
    headers = {
        "User-Agent": "AstrBot-zssm/1.0 (+https://github.com/xiaoxi68/astrbot_zssm_explain)",
        "Accept": "application/json",
    }

    async def _aiohttp_fetch() -> Dict[str, Any] | None:
        if aiohttp is None:
            return None
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    api_url, timeout=timeout_sec, allow_redirects=True
                ) as resp:
                    if 200 <= int(resp.status) < 400:
                        return await resp.json(content_type=None)
        except Exception:
            pass
        return None

    async def _urllib_fetch() -> Dict[str, Any] | None:
        import urllib.request

        def _do() -> Dict[str, Any] | None:
            try:
                req = urllib.request.Request(api_url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = resp.read()
                    return json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    result = await _aiohttp_fetch()
    if result is None:
        result = await _urllib_fetch()
    if result is None:
        raise RedditParseError("Reddit oEmbed 获取失败。")
    return result


def _build_reddit_oembed_context(url: str, data: Dict[str, Any]) -> RedditContext:
    title = str(data.get("title") or "").strip()
    author_name = str(data.get("author_name") or "").strip()
    provider_name = str(data.get("provider_name") or "").strip()
    provider_url = str(data.get("provider_url") or "").strip()
    page_type = str(data.get("type") or "").strip()
    image = str(data.get("thumbnail_url") or "").strip()

    description_parts: List[str] = []
    if author_name:
        description_parts.append(f"作者：{author_name}")
    html = str(data.get("html") or "").strip()
    if html:
        description_parts.append("页面可嵌入内容：有")

    if not any([title, author_name, provider_name, image, page_type, html]):
        raise RedditParseError("Reddit oEmbed 数据为空。")

    images: List[str] = []
    if image.startswith("http"):
        images.append(image)

    return RedditContext(
        url=url,
        title=title,
        description="\n".join(description_parts).strip(),
        site_name=provider_name or "Reddit",
        image=image,
        page_type=page_type,
        author_name=author_name,
        provider_name=provider_name,
        provider_url=provider_url,
        images=images,
    )


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
    parts = ["请基于以下 Reddit 页面信息解释页面内容：", f"链接：{ctx.url}"]
    if ctx.site_name:
        parts.append(f"站点：{ctx.site_name}")
    if ctx.author_name:
        parts.append(f"作者：{ctx.author_name}")
    if ctx.page_type:
        parts.append(f"页面类型：{ctx.page_type}")
    if ctx.title:
        parts.append(f"标题：{ctx.title}")
    if ctx.description:
        parts.append(f"描述：\n{ctx.description}")
    if ctx.image:
        parts.append("缩略图/预览图：已提供给模型。")
    return "\n\n".join(part for part in parts if part).strip()


async def prepare_reddit_prompt(
    url: str,
    timeout_sec: int,
    last_fetch_info: Dict[str, Any],
) -> RedditPreparedPrompt:
    if not is_reddit_url(url):
        raise RedditParseError("未识别到受支持的 Reddit 链接。")

    try:
        oembed_data = await _fetch_reddit_oembed_json(url, timeout_sec)
        ctx = _build_reddit_oembed_context(url, oembed_data)
    except RedditParseError:
        html = await fetch_html(url, timeout_sec, last_fetch_info)
        if not html:
            raise RedditParseError("Reddit 页面获取失败，请确认链接可访问或稍后重试。")
        ctx = _build_reddit_context(url, html)

    return RedditPreparedPrompt(
        prompt=build_reddit_prompt(ctx),
        images=ctx.images,
        context=ctx,
    )
