from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:
    aiohttp = None

_BILIBILI_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
_BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
_BVID_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)


class BilibiliParseError(RuntimeError):
    """Raised when a bilibili video link cannot be parsed."""


@dataclass(frozen=True, slots=True)
class BilibiliVideoContext:
    url: str
    original_url: str
    bvid: str
    title: str
    description: str
    owner_name: str = ""
    duration: int = 0


@dataclass(frozen=True, slots=True)
class BilibiliPreparedPrompt:
    prompt: str
    context: BilibiliVideoContext


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_bilibili_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return (
        host == "b23.tv"
        or host.endswith(".b23.tv")
        or host == "bilibili.com"
        or host.endswith(".bilibili.com")
    )


def extract_bvid_from_url(url: str) -> Optional[str]:
    if not isinstance(url, str):
        return None
    target = url.strip()
    if not target:
        return None

    matched = _BVID_PATTERN.search(target)
    if matched:
        return matched.group(1).upper()

    try:
        parsed = urlparse(target)
    except Exception:
        return None

    query = parse_qs(parsed.query)
    for key in ("bvid", "BVID"):
        values = query.get(key) or []
        for value in values:
            matched = _BVID_PATTERN.search(str(value))
            if matched:
                return matched.group(1).upper()
    return None


async def resolve_b23_url(url: str, timeout_sec: int) -> str:
    headers = {"User-Agent": _BILIBILI_USER_AGENT}

    async def _aiohttp_fetch() -> Optional[str]:
        if aiohttp is None:
            return None
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, timeout=timeout_sec, allow_redirects=False
                ) as resp:
                    location = str(resp.headers.get("Location") or "").strip()
                    if location.startswith("http"):
                        return location
        except Exception:
            pass
        return None

    async def _urllib_fetch() -> Optional[str]:
        def _do() -> Optional[str]:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            req = urllib.request.Request(url, headers=headers)
            try:
                with opener.open(req, timeout=timeout_sec) as resp:
                    location = str(resp.headers.get("Location") or "").strip()
                    if location.startswith("http"):
                        return location
            except Exception as exc:
                headers_obj = getattr(exc, "headers", None)
                if headers_obj is not None:
                    location = str(headers_obj.get("Location") or "").strip()
                    if location.startswith("http"):
                        return location
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    resolved = await _aiohttp_fetch()
    if resolved:
        return resolved.split("?", 1)[0]
    resolved = await _urllib_fetch()
    if resolved:
        return resolved.split("?", 1)[0]
    return url


async def _fetch_bilibili_video_json(bvid: str, timeout_sec: int) -> Dict[str, Any]:
    api_url = _BILIBILI_API.format(bvid=bvid)
    headers = {
        "User-Agent": _BILIBILI_USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://www.bilibili.com/",
    }

    async def _aiohttp_fetch() -> Optional[Dict[str, Any]]:
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

    async def _urllib_fetch() -> Optional[Dict[str, Any]]:
        import urllib.request

        def _do() -> Optional[Dict[str, Any]]:
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
        raise BilibiliParseError("Bilibili 视频信息获取失败，请稍后重试。")
    return result


def _build_bilibili_video_context(
    original_url: str, resolved_url: str, data: Dict[str, Any]
) -> BilibiliVideoContext:
    if int(data.get("code") or 0) != 0:
        message = str(data.get("message") or data.get("msg") or "").strip()
        raise BilibiliParseError(f"Bilibili 视频信息获取失败：{message or '未知错误'}")

    video = data.get("data") or {}
    if not isinstance(video, dict) or not video:
        raise BilibiliParseError("Bilibili 视频数据为空。")

    bvid = str(video.get("bvid") or "").strip().upper()
    title = str(video.get("title") or "").strip()
    description = str(video.get("desc") or "").strip()
    owner = video.get("owner") or {}
    owner_name = str(owner.get("name") or "").strip() if isinstance(owner, dict) else ""
    duration = int(video.get("duration") or 0)

    if not bvid or not title:
        raise BilibiliParseError("Bilibili 视频数据不完整。")

    return BilibiliVideoContext(
        url=resolved_url,
        original_url=original_url,
        bvid=bvid,
        title=title,
        description=description,
        owner_name=owner_name,
        duration=duration,
    )


def build_bilibili_prompt(ctx: BilibiliVideoContext) -> str:
    parts = ["请基于以下 Bilibili 视频信息，解释视频的主要内容："]
    parts.append(f"链接：{ctx.original_url}")
    if ctx.url != ctx.original_url:
        parts.append(f"解析后链接：{ctx.url}")
    parts.append(f"BVID：{ctx.bvid}")
    parts.append(f"标题：{ctx.title}")
    if ctx.owner_name:
        parts.append(f"UP主：{ctx.owner_name}")
    if ctx.duration > 0:
        parts.append(f"时长：{ctx.duration} 秒")
    parts.append(f"简介：{ctx.description or '(无)'}")
    return "\n\n".join(parts).strip()


async def prepare_bilibili_prompt(
    url: str,
    timeout_sec: int,
) -> Optional[BilibiliPreparedPrompt]:
    target_url = url.strip()
    resolved_url = target_url
    if "b23.tv" in target_url.lower():
        resolved_url = await resolve_b23_url(target_url, timeout_sec)

    bvid = extract_bvid_from_url(resolved_url)
    if not bvid:
        return None

    data = await _fetch_bilibili_video_json(bvid, timeout_sec)
    ctx = _build_bilibili_video_context(target_url, resolved_url, data)
    return BilibiliPreparedPrompt(prompt=build_bilibili_prompt(ctx), context=ctx)
