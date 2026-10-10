from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from astrbot.api import logger

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional runtime dependency
    curl_requests = None

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional runtime dependency
    aiohttp = None

# 小红书公开笔记页面的只读抓取与解析。
# 仅接受官方正式域名及当前常用官方短链域名。
_XHS_HOSTS = ("xiaohongshu.com", "xhslink.com", "xhslink.cn")

_XHS_URL_PATTERN = re.compile(
    r"https?://(?:[\w-]+\.)*(?:xiaohongshu\.com|xhslink\.(?:com|cn))/"
    r"[^\s\"'<>【】（）「」，。！？；]+",
    re.IGNORECASE,
)
_STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.DOTALL
)

_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)
_PAGE_REFERER = "https://www.xiaohongshu.com/"


class XiaohongshuParseError(RuntimeError):
    """Raised when a xiaohongshu note link cannot be parsed."""


@dataclass(slots=True)
class XiaohongshuNote:
    title: str
    author: str
    date: str
    note_type: str
    description: str
    tags: List[str] = field(default_factory=list)
    likes: Any = 0
    collects: Any = 0
    comment_count: Any = 0
    shares: Any = 0
    image_count: int = 0
    image_urls: List[str] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class XiaohongshuPreparedPrompt:
    prompt: str
    images: List[str]
    context: XiaohongshuNote
    cleanup_paths: List[str] = field(default_factory=list)


def _hostname(url: str) -> str:
    try:
        return (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return ""


def is_xiaohongshu_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    host = _hostname(url)
    if not host:
        return False
    return any(host == item or host.endswith("." + item) for item in _XHS_HOSTS)


def find_xiaohongshu_links(text: str) -> List[str]:
    """从分享文本中提取小红书链接，去重并保持原顺序。"""
    seen: set[str] = set()
    links: List[str] = []
    for match in _XHS_URL_PATTERN.finditer(text or ""):
        url = match.group(0)
        key = url.casefold()
        if key not in seen:
            seen.add(key)
            links.append(url)
    return links


def _looks_like_note_url(url: str) -> bool:
    host = _hostname(url)
    if not host:
        return False
    if host.endswith("xhslink.com") or host.endswith("xhslink.cn"):
        return True
    return host.endswith("xiaohongshu.com")


def _request_headers() -> Dict[str, str]:
    return {
        "User-Agent": _MOBILE_USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": _PAGE_REFERER,
    }


async def _curl_bytes(
    url: str, timeout_sec: int, proxy: str = "", impersonate: str = "safari_ios"
) -> Optional[bytes]:
    if curl_requests is None:
        return None

    headers = _request_headers()

    def _do() -> Optional[bytes]:
        try:
            response = curl_requests.get(
                url,
                headers=headers,
                impersonate=impersonate,
                proxies={"https": proxy, "http": proxy} if proxy else None,
                timeout=timeout_sec,
                allow_redirects=True,
            )
            if 200 <= int(response.status_code) < 400:
                return bytes(response.content)
        except Exception:
            return None
        return None

    try:
        return await asyncio.to_thread(_do)
    except Exception:
        return None


async def _aiohttp_bytes(url: str, timeout_sec: int, proxy: str = "") -> Optional[bytes]:
    if aiohttp is None:
        return None
    try:
        async with aiohttp.ClientSession(headers=_request_headers()) as session:
            async with session.get(
                url, timeout=timeout_sec, allow_redirects=True, proxy=proxy or None
            ) as resp:
                if 200 <= int(resp.status) < 400:
                    return await resp.read()
    except Exception:
        return None
    return None


async def _urllib_bytes(url: str, timeout_sec: int) -> Optional[bytes]:
    import urllib.request

    def _do() -> Optional[bytes]:
        try:
            req = urllib.request.Request(url, headers=_request_headers())
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return resp.read()
        except Exception:
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do)


async def _fetch_bytes(url: str, timeout_sec: int, proxy: str = "") -> bytes:
    data = await _curl_bytes(url, timeout_sec, proxy=proxy)
    if data is None:
        data = await _aiohttp_bytes(url, timeout_sec, proxy=proxy)
    if data is None:
        data = await _urllib_bytes(url, timeout_sec)
    if data is None:
        raise XiaohongshuParseError("小红书页面抓取失败，请确认链接可访问或稍后重试。")
    return data


def _format_time(value: Any) -> str:
    try:
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _nickname(user: Any) -> str:
    if not isinstance(user, dict):
        return "匿名"
    return str(user.get("nickname") or user.get("nickName") or "匿名")


def _pick_note(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    note_data = state.get("noteData") or {}
    note = (note_data.get("data") or {}).get("noteData")
    if isinstance(note, dict):
        return note

    detail_map = (state.get("note") or {}).get("noteDetailMap") or {}
    if isinstance(detail_map, dict):
        for value in detail_map.values():
            if isinstance(value, dict) and isinstance(value.get("note"), dict):
                return value["note"]
    return None


def parse_note(html: str) -> Optional[Dict[str, Any]]:
    """从页面 HTML 中解析笔记；页面结构不匹配时返回 ``None``。"""
    match = _STATE_PATTERN.search(html or "")
    if not match:
        return None
    try:
        state = json.loads(re.sub(r"\bundefined\b", "null", match.group(1)))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict):
        return None

    note = _pick_note(state)
    if not note:
        return None

    note_data = state.get("noteData") or {}
    comment_data = ((note_data.get("data") or {}).get("commentData") or {})
    comments = (
        comment_data.get("comments")
        or (comment_data.get("data") or {}).get("comments")
        or []
    )
    interaction = note.get("interactInfo") or {}

    image_urls: List[str] = []
    for image in note.get("imageList") or []:
        if not isinstance(image, dict):
            continue
        detail = next(
            (
                item
                for item in image.get("infoList") or []
                if isinstance(item, dict) and item.get("imageScene") == "H5_DTL"
            ),
            None,
        )
        image_url = (detail or {}).get("url") or image.get("url") or ""
        if image_url:
            image_urls.append(str(image_url))

    parsed_comments: List[Dict[str, Any]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        replies = []
        for reply in comment.get("subComments") or []:
            if isinstance(reply, dict):
                replies.append(
                    {
                        "author": _nickname(reply.get("user")),
                        "content": str(reply.get("content") or "").replace("\n", " "),
                    }
                )
        parsed_comments.append(
            {
                "author": _nickname(comment.get("user")),
                "content": str(comment.get("content") or "").replace("\n", " "),
                "replies": replies,
            }
        )

    tags: List[str] = []
    for tag in note.get("tagList") or []:
        if isinstance(tag, dict) and tag.get("name"):
            tags.append(str(tag["name"]))

    return {
        "title": str(note.get("title") or "(无标题)"),
        "author": _nickname(note.get("user")),
        "date": _format_time(note.get("time")),
        "type": str(note.get("type") or ""),
        "description": str(note.get("desc") or ""),
        "tags": tags,
        "likes": interaction.get("likedCount") or 0,
        "collects": interaction.get("collectedCount") or 0,
        "comment_count": interaction.get("commentCount")
        or comment_data.get("commentCount")
        or 0,
        "shares": interaction.get("shareCount") or 0,
        "image_count": len(note.get("imageList") or []),
        "image_urls": image_urls,
        "comments": parsed_comments,
    }


def _clean_external_text(value: Any) -> str:
    """移除会干扰提示词边界的控制字符。"""
    text = str(value or "").replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "……（内容过长已截断）"


def build_xiaohongshu_prompt(
    info: Dict[str, Any], max_description: int = 2000, max_comments: int = 8
) -> str:
    """把结构化笔记整理为供模型阅读的提示词。"""
    note_type = "视频帖（仅文字与封面）" if info.get("type") == "video" else "图文帖"
    description = _clean_external_text(info.get("description"))
    description = _truncate(description, max_description)

    parts = [
        "请基于以下小红书笔记信息，解释这篇笔记的主要内容：",
        f"链接：{_clean_external_text(info.get('url'))}",
        f"标题：{_clean_external_text(info.get('title'))}",
        f"作者：{_clean_external_text(info.get('author'))}",
        f"发布日期：{_clean_external_text(info.get('date')) or '未知'}",
        f"类型：{note_type}；图片：{info.get('image_count', 0)} 张",
        (
            f"互动：点赞 {info.get('likes', 0)}；收藏 {info.get('collects', 0)}；"
            f"评论 {info.get('comment_count', 0)}；分享 {info.get('shares', 0)}"
        ),
    ]
    if info.get("tags"):
        parts.append("话题：" + " ".join(f"#{_clean_external_text(t)}" for t in info["tags"]))
    parts.append(f"正文：\n{description or '（无正文）'}")

    comments = (info.get("comments") or [])[:max_comments]
    if comments:
        comment_lines = [f"首屏评论（展示 {len(comments)} 条）："]
        for comment in comments:
            comment_lines.append(
                f"- {_clean_external_text(comment.get('author'))}："
                f"{_clean_external_text(comment.get('content'))}"
            )
            for reply in comment.get("replies") or []:
                comment_lines.append(
                    f"  回复（{_clean_external_text(reply.get('author'))}）："
                    f"{_clean_external_text(reply.get('content'))}"
                )
        parts.append("\n".join(comment_lines))

    if info.get("image_urls"):
        parts.append("笔记图片已随本条消息一并提供，请结合图片内容进行解释。")
    return "\n\n".join(part for part in parts if part).strip()


def _guess_image_suffix(url: str, content_type: str = "") -> str:
    try:
        path = urlparse(url).path or ""
        ext = os.path.splitext(path)[1].lower()
    except Exception:
        ext = ""
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return ext

    lowered = str(content_type or "").lower()
    if "jpeg" in lowered or "jpg" in lowered:
        return ".jpg"
    if "png" in lowered:
        return ".png"
    if "webp" in lowered:
        return ".webp"
    if "gif" in lowered:
        return ".gif"
    return ".jpg"


async def _download_images(
    urls: List[str], max_images: int, timeout_sec: int, proxy: str = ""
) -> Tuple[List[str], List[str]]:
    """下载笔记图片到临时目录；单张失败时跳过。失败时回退到远程链接。"""
    targets = [u for u in (urls or []) if isinstance(u, str) and u.startswith("http")]
    if not targets or max_images <= 0:
        return [], []

    temp_dir = tempfile.mkdtemp(prefix="astrbot_xhs_")
    local_paths: List[str] = []
    for index, url in enumerate(targets[:max_images]):
        data = await _curl_bytes(url, timeout_sec, proxy=proxy)
        if data is None:
            data = await _aiohttp_bytes(url, timeout_sec, proxy=proxy)
        if data is None or len(data) < 1000:
            continue
        suffix = _guess_image_suffix(url)
        path = os.path.join(temp_dir, f"xhs_{index}{suffix}")
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except Exception:
            continue
        local_paths.append(path)

    if local_paths:
        return local_paths, [temp_dir]

    try:
        os.rmdir(temp_dir)
    except Exception:
        pass
    return [], []


async def prepare_xiaohongshu_prompt(
    url: str,
    timeout_sec: int = 20,
    *,
    max_desc_chars: int = 2000,
    max_comments: int = 8,
    max_images: int = 9,
    proxy: str = "",
) -> XiaohongshuPreparedPrompt:
    target_url = str(url or "").strip()
    if not target_url:
        raise XiaohongshuParseError("未识别到受支持的小红书链接。")
    if not _looks_like_note_url(target_url):
        raise XiaohongshuParseError("未识别到受支持的小红书链接。")

    html_bytes = await _fetch_bytes(target_url, max(int(timeout_sec), 2), proxy=proxy)
    html = html_bytes.decode("utf-8", errors="replace")
    info = parse_note(html)
    if not info:
        raise XiaohongshuParseError(
            "小红书笔记解析失败：链接可能失效、被风控拦截或页面结构已改版。"
        )

    info["url"] = target_url
    logger.info(
        "zssm_explain: xiaohongshu note parsed url=%s title=%s images=%s comments=%s",
        target_url[:200],
        str(info.get("title") or "")[:60],
        info.get("image_count", 0),
        len(info.get("comments") or []),
    )

    prompt = build_xiaohongshu_prompt(
        info,
        max_description=max(0, int(max_desc_chars)),
        max_comments=max(0, int(max_comments)),
    )

    local_images, cleanup_paths = await _download_images(
        info.get("image_urls") or [],
        max_images=max(0, int(max_images)),
        timeout_sec=max(int(timeout_sec), 2),
        proxy=proxy,
    )
    images = local_images or [
        u for u in (info.get("image_urls") or []) if isinstance(u, str) and u.startswith("http")
    ]

    note = XiaohongshuNote(
        title=info["title"],
        author=info["author"],
        date=info["date"],
        note_type=info["type"],
        description=info["description"],
        tags=info["tags"],
        likes=info["likes"],
        collects=info["collects"],
        comment_count=info["comment_count"],
        shares=info["shares"],
        image_count=info["image_count"],
        image_urls=info["image_urls"],
        comments=info["comments"],
    )
    return XiaohongshuPreparedPrompt(
        prompt=prompt,
        images=images,
        context=note,
        cleanup_paths=cleanup_paths,
    )
