from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import logger

try:
    import aiohttp  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional runtime dependency
    aiohttp = None

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional runtime dependency
    curl_requests = None

try:
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult
    from astrbot.core.astr_agent_context import AstrAgentContext
except Exception:  # pragma: no cover - 旧版本 AstrBot 兼容
    ContextWrapper = Any  # type: ignore[assignment,misc]
    FunctionTool = object  # type: ignore[assignment,misc]
    ToolExecResult = str  # type: ignore[assignment,misc]
    AstrAgentContext = Any  # type: ignore[assignment,misc]


_BILIBILI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
_ZHIHU_ME_API = "https://www.zhihu.com/api/v4/me"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class CookieStatus:
    """单个站点 Cookie 的检测结果。"""

    configured: bool
    ok: bool
    detail: str


async def _http_get_json(
    url: str, headers: Dict[str, str], timeout_sec: int
) -> Optional[Tuple[int, Optional[Any]]]:
    """GET 并返回 (status, json)；传输层失败返回 None。"""

    async def _via_curl() -> Optional[Tuple[int, Optional[Any]]]:
        if curl_requests is None:
            return None

        def _do():
            return curl_requests.get(
                url,
                headers=headers,
                impersonate="chrome",
                timeout=timeout_sec,
                allow_redirects=True,
            )

        try:
            resp = await asyncio.to_thread(_do)
            status = int(resp.status_code)
            try:
                payload: Optional[Any] = resp.json()
            except Exception:
                payload = None
            return status, payload
        except Exception:
            return None

    async def _via_aiohttp() -> Optional[Tuple[int, Optional[Any]]]:
        if aiohttp is None:
            return None
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, timeout=timeout_sec, allow_redirects=True
                ) as resp:
                    status = int(resp.status)
                    try:
                        payload: Optional[Any] = await resp.json(content_type=None)
                    except Exception:
                        payload = None
                    return status, payload
        except Exception:
            return None

    def _via_urllib() -> Optional[Tuple[int, Optional[Any]]]:
        import urllib.error

        def _decode(data: bytes) -> Optional[Any]:
            try:
                return json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                return None

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return int(getattr(resp, "status", 200)), _decode(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read() or b""
            except Exception:
                body = b""
            return int(exc.code), _decode(body)
        except Exception:
            return None

    result = await _via_curl()
    if result is not None:
        return result
    result = await _via_aiohttp()
    if result is not None:
        return result
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _via_urllib)


async def check_bilibili_cookie(
    sessdata: str, bili_jct: str, timeout_sec: int = 20
) -> CookieStatus:
    sessdata = str(sessdata or "").strip()
    bili_jct = str(bili_jct or "").strip()
    if not sessdata and not bili_jct:
        return CookieStatus(configured=False, ok=False, detail="未配置")

    cookie_parts = []
    if sessdata:
        cookie_parts.append(f"SESSDATA={sessdata}")
    if bili_jct:
        cookie_parts.append(f"bili_jct={bili_jct}")
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bilibili.com/",
        "Cookie": "; ".join(cookie_parts),
    }

    response = await _http_get_json(_BILIBILI_NAV_API, headers, max(int(timeout_sec), 2))
    if response is None:
        return CookieStatus(True, False, "检测失败（网络异常）")

    _status, payload = response
    if not isinstance(payload, dict):
        return CookieStatus(True, False, "检测失败（返回数据异常）")

    code = int(payload.get("code") or 0)
    if code != 0:
        message = str(payload.get("message") or "").strip()
        detail = f"无效或已过期（{message}）" if message else "无效或已过期"
        return CookieStatus(True, False, detail)

    data = payload.get("data") or {}
    if isinstance(data, dict) and data.get("isLogin"):
        uname = str(data.get("uname") or "").strip() or "未知账号"
        return CookieStatus(True, True, f"有效（昵称：{uname}）")
    return CookieStatus(True, False, "无效或已过期")


async def check_zhihu_cookie(cookie: str, timeout_sec: int = 20) -> CookieStatus:
    cookie = str(cookie or "").strip()
    if not cookie:
        return CookieStatus(configured=False, ok=False, detail="未配置")

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.zhihu.com/",
        "Cookie": cookie,
    }

    response = await _http_get_json(_ZHIHU_ME_API, headers, max(int(timeout_sec), 2))
    if response is None:
        return CookieStatus(True, False, "检测失败（网络异常）")

    status, payload = response
    if status == 200 and isinstance(payload, dict):
        name = str(payload.get("name") or payload.get("url_token") or "").strip()
        detail = f"有效（用户名：{name}）" if name else "有效"
        return CookieStatus(True, True, detail)
    if status in (401, 403):
        return CookieStatus(True, False, "无效或已过期")
    return CookieStatus(True, False, f"检测失败（HTTP {status}）")


def _format_line(label: str, status: CookieStatus) -> str:
    if not status.configured:
        return f"· {label}：未配置"
    mark = "✅" if status.ok else "❌"
    return f"· {label}：{mark} {status.detail}"


def format_cookie_report(
    bilibili: CookieStatus, zhihu: CookieStatus
) -> str:
    lines = [
        "Cookie 检测结果",
        _format_line("哔哩哔哩", bilibili),
        _format_line("知乎", zhihu),
        "· 小红书：无需 Cookie",
    ]
    return "\n".join(lines)


@pydantic_dataclass(config=dict(arbitrary_types_allowed=True))
class CookieCheckTool(FunctionTool[AstrAgentContext]):  # type: ignore[misc]
    name: str = "check_cookie"
    description: str = (
        "检测本插件配置的 Cookie（哔哩哔哩、知乎）是否有效或已过期，返回各账号的登录状态。"
        "当用户询问「cookie 还能用吗」「cookie 过期了吗」「帮我测一下 cookie」「cookie 还有效吗」"
        "或希望确认 B 站 / 知乎凭据是否可用时调用。无参数。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )
    # 由插件注入：返回当前 cookie 配置与超时的可调用对象
    config_provider: Optional[Callable[[], Dict[str, Any]]] = None

    async def call(
        self, context: "ContextWrapper[AstrAgentContext]", **kwargs: Any
    ) -> ToolExecResult:
        try:
            cfg: Dict[str, Any] = {}
            if callable(self.config_provider):
                cfg = self.config_provider() or {}
        except Exception as exc:
            logger.warning("zssm_explain: check_cookie config read failed: %s", exc)
            cfg = {}

        timeout_sec = int(cfg.get("timeout_sec") or 20)
        bilibili_status, zhihu_status = await asyncio.gather(
            check_bilibili_cookie(
                cfg.get("sessdata", ""), cfg.get("bili_jct", ""), timeout_sec
            ),
            check_zhihu_cookie(cfg.get("zhihu_cookie", ""), timeout_sec),
        )
        report = format_cookie_report(bilibili_status, zhihu_status)
        logger.info("zssm_explain: check_cookie result:\n%s", report)
        return report
