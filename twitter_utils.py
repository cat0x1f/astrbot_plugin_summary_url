from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:
    aiohttp = None

_TWEET_PATTERN = re.compile(
    r"^https?://(?:(?:www\.|mobile\.)?(?:twitter|x)\.com)/([^/?#]+)/status/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)

_FXTWITTER_API = "https://api.fxtwitter.com/{username}/status/{tweet_id}"


class TwitterParseError(RuntimeError):
    """Raised when a Twitter link cannot be parsed."""


@dataclass(frozen=True, slots=True)
class TwitterMatch:
    username: str
    tweet_id: str
    url: str


@dataclass(eq=True, slots=True)
class TwitterContext:
    tweet_id: str
    url: str
    text: str
    author_name: str
    author_screen_name: str
    created_at: str
    likes: int
    retweets: int
    replies: int
    views: int
    photos: List[str] = field(default_factory=list)
    quote_text: str = ""
    quote_author: str = ""


def is_twitter_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    return _TWEET_PATTERN.match(url.strip()) is not None


def match_twitter_url(url: str) -> Optional[TwitterMatch]:
    if not isinstance(url, str):
        return None
    matched = _TWEET_PATTERN.match(url.strip())
    if not matched:
        return None
    return TwitterMatch(username=matched.group(1), tweet_id=matched.group(2), url=url.strip())


async def _fetch_fxtwitter_json(
    username: str,
    tweet_id: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    api_url = _FXTWITTER_API.format(username=username, tweet_id=tweet_id)
    headers = {
        "User-Agent": "AstrBot-zssm/1.0 (+https://github.com/xiaoxi68/astrbot_zssm_explain)",
        "Accept": "application/json",
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
        import json
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
        raise TwitterParseError("Twitter 推文获取失败，请确认链接有效或稍后重试。")
    return result


def _build_twitter_context(match: TwitterMatch, data: Dict[str, Any]) -> TwitterContext:
    if data.get("code") != 200:
        message = data.get("message", "")
        raise TwitterParseError(f"Twitter 推文获取失败：{message or '未知错误'}")

    tweet = data.get("tweet") or {}
    if not tweet:
        raise TwitterParseError("Twitter 推文数据为空。")

    author = tweet.get("author") or {}
    media = tweet.get("media") or {}

    photos: List[str] = []
    for photo in media.get("photos") or []:
        url = photo.get("url") or photo.get("cdn_url") or ""
        if isinstance(url, str) and url.startswith("http"):
            photos.append(url)

    quote_text = ""
    quote_author = ""
    quote = tweet.get("quote")
    if isinstance(quote, dict):
        quote_text = str(quote.get("text") or "").strip()
        quote_author_info = quote.get("author") or {}
        if isinstance(quote_author_info, dict):
            q_name = str(quote_author_info.get("name") or "").strip()
            q_screen = str(quote_author_info.get("screen_name") or "").strip()
            if q_name or q_screen:
                quote_author = f"@{q_screen}" if q_screen else q_name

    return TwitterContext(
        tweet_id=str(tweet.get("id") or match.tweet_id),
        url=match.url,
        text=str(tweet.get("text") or "").strip(),
        author_name=str(author.get("name") or "").strip(),
        author_screen_name=str(author.get("screen_name") or match.username).strip(),
        created_at=str(tweet.get("created_at") or "").strip(),
        likes=int(tweet.get("likes") or 0),
        retweets=int(tweet.get("retweets") or 0),
        replies=int(tweet.get("replies") or 0),
        views=int(tweet.get("views") or 0),
        photos=photos,
        quote_text=quote_text,
        quote_author=quote_author,
    )


def build_twitter_prompt(ctx: TwitterContext) -> str:
    parts = ["请解释以下 Twitter 推文的内容：", f"链接：{ctx.url}"]
    if ctx.author_name or ctx.author_screen_name:
        author_text = ctx.author_name
        if ctx.author_screen_name:
            author_text += (
                f" (@{ctx.author_screen_name})" if ctx.author_name else f"@{ctx.author_screen_name}"
            )
        parts.append(f"作者：{author_text}")
    if ctx.created_at:
        parts.append(f"发布时间：{ctx.created_at}")

    stats: List[str] = []
    if ctx.likes:
        stats.append(f"点赞 {ctx.likes}")
    if ctx.retweets:
        stats.append(f"转推 {ctx.retweets}")
    if ctx.replies:
        stats.append(f"回复 {ctx.replies}")
    if ctx.views:
        stats.append(f"浏览 {ctx.views}")
    if stats:
        parts.append(f"互动数据：{' | '.join(stats)}")

    if ctx.text:
        parts.append(f"推文内容：\n{ctx.text}")
    if ctx.quote_text:
        label = f"引用推文（{ctx.quote_author}）：" if ctx.quote_author else "引用推文："
        parts.append(f"{label}\n{ctx.quote_text}")
    if ctx.photos:
        parts.append(f"附图数量：{len(ctx.photos)}")
    return "\n\n".join(part for part in parts if part).strip()


@dataclass(eq=True, slots=True)
class TwitterPreparedPrompt:
    prompt: str
    images: List[str]
    context: TwitterContext
    cleanup_paths: List[str] = field(default_factory=list)


def _guess_image_suffix(url: str, content_type: str = "") -> str:
    try:
        path = urlparse(url).path or ""
        _, ext = os.path.splitext(path)
    except Exception:
        ext = ""
    ext = ext.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
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
    return ".img"


async def _download_twitter_images(photo_urls: List[str], timeout_sec: int) -> tuple[List[str], List[str]]:
    if not photo_urls:
        return [], []

    headers = {
        "User-Agent": "AstrBot-zssm/1.0 (+https://github.com/xiaoxi68/astrbot_zssm_explain)",
        "Accept": "image/*,*/*;q=0.8",
    }
    temp_dir = tempfile.mkdtemp(prefix="astrbot_twitter_")
    local_paths: List[str] = []

    async def _aiohttp_download(url: str, index: int) -> Optional[str]:
        if aiohttp is None:
            return None
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=timeout_sec, allow_redirects=True) as resp:
                    if not (200 <= int(resp.status) < 400):
                        return None
                    data = await resp.read()
                    suffix = _guess_image_suffix(url, resp.headers.get("Content-Type", ""))
                    path = os.path.join(temp_dir, f"twitter_{index}{suffix}")
                    with open(path, "wb") as fh:
                        fh.write(data)
                    return path
        except Exception:
            return None

    async def _urllib_download(url: str, index: int) -> Optional[str]:
        import urllib.request

        def _do() -> Optional[str]:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = resp.read()
                    suffix = _guess_image_suffix(
                        url,
                        getattr(resp, "headers", {}).get("Content-Type", ""),
                    )
                    path = os.path.join(temp_dir, f"twitter_{index}{suffix}")
                    with open(path, "wb") as fh:
                        fh.write(data)
                    return path
            except Exception:
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    try:
        for index, url in enumerate(photo_urls):
            local_path = await _aiohttp_download(url, index)
            if local_path is None:
                local_path = await _urllib_download(url, index)
            if local_path is not None:
                local_paths.append(local_path)
    except Exception:
        pass

    if local_paths:
        return local_paths, [temp_dir]

    try:
        os.rmdir(temp_dir)
    except Exception:
        pass
    return [], []


async def prepare_twitter_prompt(
    url: str,
    timeout_sec: int = 20,
) -> TwitterPreparedPrompt:
    matched = match_twitter_url(url)
    if matched is None:
        raise TwitterParseError("未识别到受支持的 Twitter 推文链接。")

    data = await _fetch_fxtwitter_json(matched.username, matched.tweet_id, timeout_sec)
    ctx = _build_twitter_context(matched, data)
    local_images, cleanup_paths = await _download_twitter_images(ctx.photos, timeout_sec)
    return TwitterPreparedPrompt(
        prompt=build_twitter_prompt(ctx),
        images=local_images or ctx.photos,
        context=ctx,
        cleanup_paths=cleanup_paths,
    )
