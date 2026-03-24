from __future__ import annotations

from typing import List, Optional, Tuple, Dict, Any
import asyncio
import os
import re
from html import unescape
from urllib.parse import urlparse

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    aiohttp = None

from astrbot.api import logger

from .wechat_utils import is_wechat_article_url, fetch_wechat_article_markdown


def extract_urls_from_text(text: Optional[str]) -> List[str]:
    """从文本中提取 URL 列表，保持顺序去重。"""
    if not isinstance(text, str) or not text:
        return []
    url_pattern = re.compile(
        r"(https?://[\w\-._~:/?#\[\]@!$&'()*+,;=%]+)", re.IGNORECASE
    )
    urls = [m.group(1) for m in url_pattern.finditer(text)]
    seen = set()
    uniq: List[str] = []
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq


def strip_html(html: str) -> str:
    """基础 HTML 文本提取：去 script/style 与标签，归一空白。"""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    return ""


def extract_meta_desc(html: str) -> str:
    for name in [
        r'name="description"',
        r'property="og:description"',
        r'name="twitter:description"',
    ]:
        m = re.search(
            rf"<meta[^>]+{name}[^>]+content=\"(.*?)\"[^>]*>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            return unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    return ""


async def fetch_html(
    url: str, timeout_sec: int, last_fetch_info: Dict[str, Any]
) -> Optional[str]:
    """获取网页 HTML 文本并记录 Cloudflare 相关信息。"""

    def _mark(
        status: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        text_hint: Optional[str] = None,
        via: str = "",
        error: Optional[str] = None,
        final_url: Optional[str] = None,
        blocked: bool = False,
        block_reason: Optional[str] = None,
    ):
        headers = headers or {}
        server = str(headers.get("server", "")).lower()
        cf_header = (
            any(h.lower().startswith("cf-") for h in headers.keys())
            if headers
            else False
        )
        text_has_cf = False
        if isinstance(text_hint, str):
            tl = text_hint.lower()
            if (
                "cloudflare" in tl
                or "attention required" in tl
                or "enable javascript and cookies" in tl
            ):
                text_has_cf = True
        is_cf = ("cloudflare" in server) or cf_header or text_has_cf
        last_fetch_info.clear()
        last_fetch_info.update(
            {
                "url": url,
                "status": status,
                "cloudflare": is_cf,
                "via": via,
                "error": error,
                "final_url": final_url or url,
                "blocked": bool(blocked),
                "block_reason": block_reason or "",
            }
        )

    def _detect_access_block(final_url: Optional[str], text_hint: Optional[str]) -> Optional[str]:
        final_url = str(final_url or "").lower()
        text_hint = str(text_hint or "").lower()
        combined = f"{final_url}\n{text_hint}"

        patterns = (
            ("login", ("login", "log in", "sign in", "signin", "登录", "登陆")),
            ("captcha", ("captcha", "验证码", "verify you are human", "human verification")),
            (
                "access_denied",
                (
                    "access denied",
                    "forbidden",
                    "permission denied",
                    "please enable cookies",
                    "unauthorized",
                    "无权访问",
                    "访问受限",
                    "请先登录",
                    "需要登录",
                    "登录后查看",
                ),
            ),
        )
        for reason, needles in patterns:
            if any(needle in combined for needle in needles):
                return reason
        return None

    async def _aiohttp_fetch() -> Optional[str]:
        if aiohttp is None:
            return None
        try:
            async with aiohttp.ClientSession(
                headers={
                    "User-Agent": "AstrBot-zssm/1.0 (+https://github.com/xiaoxi68/astrbot_zssm_explain)"
                }
            ) as session:
                async with session.get(
                    url, timeout=timeout_sec, allow_redirects=True
                ) as resp:
                    status = int(resp.status)
                    hdrs = {k: v for k, v in resp.headers.items()}
                    final_url = str(resp.url)
                    if 200 <= status < 400:
                        text = await resp.text()
                        blocked_reason = _detect_access_block(final_url, text[:4096])
                        if blocked_reason:
                            _mark(
                                status=status,
                                headers=hdrs,
                                text_hint=text[:512],
                                via="aiohttp",
                                final_url=final_url,
                                blocked=True,
                                block_reason=blocked_reason,
                            )
                            return None
                        _mark(
                            status=status,
                            headers=hdrs,
                            text_hint=text[:512],
                            via="aiohttp",
                            final_url=final_url,
                        )
                        return text
                    _mark(
                        status=status,
                        headers=hdrs,
                        text_hint=None,
                        via="aiohttp",
                        final_url=final_url,
                    )
                    return None
        except Exception as e:  # pragma: no cover - 网络环境相关
            logger.warning(f"zssm_explain: aiohttp fetch failed: {e}")
            _mark(
                status=None, headers=None, text_hint=None, via="aiohttp", error=str(e)
            )
            return None

    async def _urllib_fetch() -> Optional[str]:
        import urllib.request
        import urllib.error

        def _do() -> Optional[str]:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "AstrBot-zssm/1.0 (+https://github.com/xiaoxi68/astrbot_zssm_explain)",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = resp.read()
                    enc = resp.headers.get_content_charset() or "utf-8"
                    final_url = resp.geturl() if hasattr(resp, "geturl") else url
                    try:
                        text = data.decode(enc, errors="replace")
                        blocked_reason = _detect_access_block(final_url, text[:4096])
                        if blocked_reason:
                            _mark(
                                status=getattr(resp, "status", 200),
                                headers=dict(resp.headers),
                                text_hint=text[:512],
                                via="urllib",
                                final_url=final_url,
                                blocked=True,
                                block_reason=blocked_reason,
                            )
                            return None
                        _mark(
                            status=getattr(resp, "status", 200),
                            headers=dict(resp.headers),
                            text_hint=text[:512],
                            via="urllib",
                            final_url=final_url,
                        )
                        return text
                    except Exception:
                        text = data.decode("utf-8", errors="replace")
                        blocked_reason = _detect_access_block(final_url, text[:4096])
                        if blocked_reason:
                            _mark(
                                status=getattr(resp, "status", 200),
                                headers=dict(resp.headers),
                                text_hint=text[:512],
                                via="urllib",
                                final_url=final_url,
                                blocked=True,
                                block_reason=blocked_reason,
                            )
                            return None
                        _mark(
                            status=getattr(resp, "status", 200),
                            headers=dict(resp.headers),
                            text_hint=text[:512],
                            via="urllib",
                            final_url=final_url,
                        )
                        return text
            except urllib.error.HTTPError as e:
                try:
                    body = e.read() or b""
                    hint = body.decode("utf-8", errors="ignore")[:512]
                except Exception:
                    hint = None
                hdrs = dict(getattr(e, "headers", {}) or {})
                _mark(
                    status=getattr(e, "code", None),
                    headers=hdrs,
                    text_hint=hint,
                    via="urllib",
                    error=str(e),
                    final_url=getattr(e, "url", url),
                )
                logger.warning(f"zssm_explain: urllib fetch failed: {e}")
                return None
            except Exception as e:  # pragma: no cover
                _mark(
                    status=None,
                    headers=None,
                    text_hint=None,
                    via="urllib",
                    error=str(e),
                    final_url=url,
                )
                logger.warning(f"zssm_explain: urllib fetch failed: {e}")
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    html = await _aiohttp_fetch()
    if html is not None:
        return html
    return await _urllib_fetch()


def build_url_user_prompt(
    url: str,
    html: str,
    max_chars: int,
    user_prompt_template: str,
) -> Tuple[str, str]:
    title = extract_title(html)
    desc = extract_meta_desc(html)
    plain = strip_html(html)
    snippet = plain[: max(0, int(max_chars))]
    user_prompt = user_prompt_template.format(
        url=url,
        title=title or "(无)",
        desc=desc or "(无)",
        snippet=snippet,
    )
    return user_prompt, title or ""


def build_url_brief_for_forward(html: str, max_chars: int) -> Tuple[str, str, str]:
    """为合并转发场景构造网址的精简信息摘要（标题/描述/正文片段）。"""
    title = extract_title(html)
    desc = extract_meta_desc(html)
    plain = strip_html(html)
    snippet = plain[: max(0, int(max_chars))]
    return title or "", desc or "", snippet


async def prepare_url_prompt(
    url: str,
    timeout_sec: int,
    last_fetch_info: Dict[str, Any],
    *,
    max_chars: int,
    user_prompt_template: str,
) -> Optional[Tuple[str, Optional[str], List[str]]]:
    """统一处理网页抓取：成功返回摘要提示词。

    返回值：
    - user_prompt: 给 LLM 的用户提示词
    - text: 当前实现不返回正文（保持与旧逻辑一致，返回 None）
    - images: 当前实现始终为空列表
    """
    # 1) 特判微信公众号文章：仅抓取当前文章并转 Markdown（不抓账号/专栏列表）
    if is_wechat_article_url(url):
        wx_ctx = await fetch_wechat_article_markdown(
            url,
            timeout_sec,
            last_fetch_info,
            max_chars=max_chars,
            user_prompt_template=user_prompt_template,
        )
        if wx_ctx:
            return wx_ctx

    # 2) 常规 HTML 场景
    html = await fetch_html(url, timeout_sec, last_fetch_info)
    if html:
        user_prompt, _title = build_url_user_prompt(
            url, html, max_chars, user_prompt_template
        )
        return (user_prompt, None, [])

    return None


def build_url_failure_message(last_fetch_info: Dict[str, Any]) -> str:
    info = last_fetch_info or {}
    if info.get("wechat"):
        if info.get("wechat_captcha"):
            return "微信公众号页面触发验证码，当前无法自动抓取，请稍后重试或更换网络后再试。"
        return "微信公众号文章抓取失败，请确认链接可访问并稍后重试。"
    if info.get("blocked"):
        reason = str(info.get("block_reason") or "").strip().lower()
        if reason == "login":
            return "目标页面需要登录，当前不会对登录页内容进行总结。"
        if reason == "captcha":
            return "目标页面触发了验证或验证码，当前不会对验证页内容进行总结。"
        return "目标页面访问受限，当前不会对受限页面内容进行总结。"
    if info.get("cloudflare"):
        return "目标站点启用 Cloudflare 防护，当前无法抓取该页面。"
    return "网页获取失败或不受支持，请稍后重试并确认链接可访问。"
