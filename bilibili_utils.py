from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:
    aiohttp = None

_BILIBILI_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
_BILIBILI_PLAYER_API = "https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
_BILIBILI_WBI_PLAYER_API = "https://api.bilibili.com/x/player/wbi/v2?bvid={bvid}&cid={cid}"
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
    cid: int = 0
    subtitle: str = ""
    subtitle_language: str = ""


@dataclass(frozen=True, slots=True)
class BilibiliPreparedPrompt:
    prompt: str
    context: BilibiliVideoContext


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _truncate_for_log(value: Any, limit: int = 400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _normalize_bvid_prefix(bvid: str) -> str:
    text = str(bvid or "").strip()
    if len(text) >= 2 and text[:2].lower() == "bv":
        return "BV" + text[2:]
    return text


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
        return _normalize_bvid_prefix(matched.group(1))

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
                return _normalize_bvid_prefix(matched.group(1))
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


async def _fetch_json(
    api_url: str, headers: Dict[str, str], timeout_sec: int
) -> Optional[Dict[str, Any]]:
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
    return result


async def _fetch_bilibili_video_json(bvid: str, timeout_sec: int) -> Dict[str, Any]:
    api_url = _BILIBILI_API.format(bvid=bvid)
    headers = {
        "User-Agent": _BILIBILI_USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://www.bilibili.com/",
    }
    result = await _fetch_json(api_url, headers, timeout_sec)
    if result is None:
        raise BilibiliParseError("Bilibili 视频信息获取失败，请稍后重试。")
    return result


def _pick_cid(video: Dict[str, Any], url: str) -> int:
    pages = video.get("pages") or []
    page_index = 1
    try:
        query = parse_qs(urlparse(url).query)
        raw_page = (query.get("p") or ["1"])[0]
        page_index = max(1, int(raw_page))
    except Exception:
        page_index = 1
    if 1 <= page_index <= len(pages):
        page = pages[page_index - 1]
        if isinstance(page, dict) and page.get("cid"):
            try:
                return int(page["cid"])
            except Exception:
                pass
    try:
        return int(video.get("cid") or 0)
    except Exception:
        return 0


async def _fetch_bilibili_subtitle(
    bvid: str,
    cid: int,
    cookie: str,
    timeout_sec: int,
    max_subtitle_length: int,
) -> tuple[str, str]:
    """返回 (字幕文本, 字幕语言)；无字幕时返回 ("", "")。"""
    if cid <= 0:
        return "", ""

    headers = {
        "User-Agent": _BILIBILI_USER_AGENT,
        "Accept": "application/json",
        "Referer": f"https://www.bilibili.com/video/{bvid}",
    }
    if isinstance(cookie, str) and cookie.strip():
        headers["Cookie"] = cookie.strip()

    for api_template in (_BILIBILI_PLAYER_API, _BILIBILI_WBI_PLAYER_API):
        api_url = api_template.format(bvid=bvid, cid=cid)
        data = await _fetch_json(api_url, headers, timeout_sec)
        if not isinstance(data, dict):
            continue
        if int(data.get("code") or 0) != 0:
            message = str(data.get("message") or data.get("msg") or "").strip()
            logger.info(
                "zssm_explain: bilibili player api code=%s message=%s url=%s",
                data.get("code"),
                _truncate_for_log(message, 120),
                api_url,
            )
            continue

        subtitles = (((data.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
        if not isinstance(subtitles, list) or not subtitles:
            continue

        target = None
        for sub in subtitles:
            if isinstance(sub, dict) and str(sub.get("lan") or "").lower().startswith("zh"):
                target = sub
                break
        if target is None:
            target = next((s for s in subtitles if isinstance(s, dict)), None)
        if not target:
            continue

        subtitle_url = str(target.get("subtitle_url") or "").strip()
        if not subtitle_url:
            continue
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        elif not subtitle_url.startswith("http"):
            subtitle_url = "https://" + subtitle_url.lstrip("/")

        language = str(target.get("lan") or "").strip()
        logger.info(
            "zssm_explain: bilibili subtitle found bvid=%s lan=%s url=%s",
            bvid,
            language,
            subtitle_url.split("?")[0],
        )
        subtitle_json = await _fetch_json(
            subtitle_url, {"User-Agent": _BILIBILI_USER_AGENT}, timeout_sec
        )
        if not isinstance(subtitle_json, dict):
            continue
        body = subtitle_json.get("body") or []
        if not isinstance(body, list):
            continue
        text = "\n".join(
            str(item.get("content") or "").strip()
            for item in body
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        )
        if not text:
            continue
        if max_subtitle_length > 0 and len(text) > max_subtitle_length:
            logger.info(
                "zssm_explain: bilibili subtitle truncated bvid=%s length=%s limit=%s",
                bvid,
                len(text),
                max_subtitle_length,
            )
            text = text[:max_subtitle_length] + "\n……（字幕过长已截断）"
        return text, language
    return "", ""


def _build_bilibili_video_context(
    original_url: str,
    resolved_url: str,
    data: Dict[str, Any],
    *,
    subtitle: str = "",
    subtitle_language: str = "",
) -> BilibiliVideoContext:
    if int(data.get("code") or 0) != 0:
        message = str(data.get("message") or data.get("msg") or "").strip()
        raise BilibiliParseError(f"Bilibili 视频信息获取失败：{message or '未知错误'}")

    video = data.get("data") or {}
    if not isinstance(video, dict) or not video:
        raise BilibiliParseError("Bilibili 视频数据为空。")

    bvid = _normalize_bvid_prefix(str(video.get("bvid") or "").strip())
    title = str(video.get("title") or "").strip()
    description = str(video.get("desc") or "").strip()
    owner = video.get("owner") or {}
    owner_name = str(owner.get("name") or "").strip() if isinstance(owner, dict) else ""
    duration = int(video.get("duration") or 0)
    cid = _pick_cid(video, resolved_url)

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
        cid=cid,
        subtitle=subtitle,
        subtitle_language=subtitle_language,
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
    if ctx.subtitle:
        language = f"（{ctx.subtitle_language}）" if ctx.subtitle_language else ""
        parts.append(f"视频字幕{language}：\n{ctx.subtitle}")
    else:
        parts.append("字幕：未获取到可用字幕，请仅基于标题与简介进行解释，不要编造视频内容。")
    return "\n\n".join(parts).strip()


async def prepare_bilibili_prompt(
    url: str,
    timeout_sec: int,
    *,
    cookie: str = "",
    max_subtitle_length: int = 4000,
) -> Optional[BilibiliPreparedPrompt]:
    target_url = url.strip()
    resolved_url = target_url
    if "b23.tv" in target_url.lower():
        resolved_url = await resolve_b23_url(target_url, timeout_sec)

    bvid = extract_bvid_from_url(resolved_url)
    if not bvid:
        return None

    last_error: Optional[BilibiliParseError] = None
    attempts = 2
    data: Optional[Dict[str, Any]] = None
    for attempt in range(1, attempts + 1):
        data = await _fetch_bilibili_video_json(bvid, timeout_sec)
        code = int(data.get("code") or 0) if isinstance(data, dict) else -1
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("msg") or "").strip()
        logger.info(
            "zssm_explain: bilibili api response attempt=%s bvid=%s code=%s message=%s url=%s payload=%s",
            attempt,
            bvid,
            code,
            _truncate_for_log(message, 120),
            resolved_url,
            _truncate_for_log(json.dumps(data, ensure_ascii=False), 600),
        )
        try:
            if code == 0:
                video = data.get("data") or {}
                cid = _pick_cid(video, resolved_url) if isinstance(video, dict) else 0
                subtitle, subtitle_language = await _fetch_bilibili_subtitle(
                    bvid,
                    cid,
                    cookie,
                    timeout_sec,
                    max_subtitle_length,
                )
            else:
                subtitle, subtitle_language = "", ""
            ctx = _build_bilibili_video_context(
                target_url,
                resolved_url,
                data,
                subtitle=subtitle,
                subtitle_language=subtitle_language,
            )
            return BilibiliPreparedPrompt(prompt=build_bilibili_prompt(ctx), context=ctx)
        except BilibiliParseError as exc:
            last_error = exc
            if attempt < attempts:
                logger.warning(
                    "zssm_explain: bilibili parse failed, retrying attempt=%s bvid=%s error=%s",
                    attempt,
                    bvid,
                    str(exc),
                )
                await asyncio.sleep(0.6)
                continue
            raise
    if last_error is not None:
        raise last_error
    raise BilibiliParseError("Bilibili 视频信息获取失败，请稍后重试。")
