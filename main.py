from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.star.star_handler import EventType

from .llm_client import LLMClient
from .bilibili_utils import BilibiliParseError, is_bilibili_url, prepare_bilibili_prompt
from .prompt_utils import build_system_prompt_for_event, build_url_user_prompt_template
from .coolapk_utils import is_coolapk_url, prepare_coolapk_prompt
from .reddit_utils import RedditParseError, is_reddit_url, prepare_reddit_prompt
from .twitter_utils import TwitterParseError, is_twitter_url, prepare_twitter_prompt
from .url_utils import build_url_failure_message, extract_urls_from_text, prepare_url_prompt
from .zhihu_utils import ZhihuParseError, match_zhihu_url, prepare_zhihu_prompt

URL_FETCH_TIMEOUT_KEY = "url_timeout_sec"
URL_MAX_CHARS_KEY = "url_max_chars"
SILENT_FAIL_KEY = "silent_fail"
GROUP_LIST_MODE_KEY = "group_list_mode"
GROUP_LIST_KEY = "group_list"
KEEP_ORIGINAL_PERSONA_KEY = "keep_original_persona"
ZHIHU_COOKIE_KEY = "zhihu_cookie"
URL_DOMAIN_BLACKLIST_KEY = "url_domain_blacklist"
INTERCEPT_ACCESS_WALL_KEY = "intercept_access_wall"
DEDUP_ENABLED_KEY = "dedupe_processed_urls"
DEDUP_LIMIT_KEY = "dedupe_processed_urls_limit"

DEFAULT_URL_FETCH_TIMEOUT = 20
DEFAULT_URL_MAX_CHARS = 6000
DEFAULT_KEEP_ORIGINAL_PERSONA = True
DEFAULT_DEDUP_ENABLED = True
DEFAULT_DEDUP_LIMIT = 500
ACCESS_WALL_SENTINEL = "[[ACCESS_WALL]]"
PROCESSED_URLS_KV_KEY = "processed_urls"


class ZssmExplain(Star):
    @dataclass
    class _LLMPlan:
        user_prompt: str
        images: List[str] = field(default_factory=list)
        cleanup_paths: List[str] = field(default_factory=list)

    @dataclass
    class _ReplyPlan:
        message: str
        stop_event: bool = True
        cleanup_paths: List[str] = field(default_factory=list)

    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config: Dict[str, Any] = config or {}
        self._last_fetch_info: Dict[str, Any] = {}
        self._llm = LLMClient(
            context=self.context,
            get_conf_int=self._get_conf_int,
            get_config_provider=self._get_config_provider,
            logger=logger,
        )

    async def initialize(self):
        return

    def _reply_text_result(self, event: AstrMessageEvent, text: str):
        try:
            msg_id = getattr(event.message_obj, "message_id", None)
        except Exception:
            msg_id = None
        try:
            if msg_id:
                return event.chain_result(
                    [Comp.Reply(id=str(msg_id)), Comp.Plain(str(text or ""))]
                )
            return event.plain_result(str(text or ""))
        except Exception:
            return event.plain_result(str(text or ""))

    def _format_llm_error(self, exc: Exception, context: str = "") -> str:
        err_str = str(exc)
        prefix = f"{context}失败：" if context else "LLM 调用失败："
        if "Connection error" in err_str or "ConnectionError" in err_str:
            return f"{prefix}LLM 服务连接失败，请检查网络或代理设置。"
        if "timeout" in err_str.lower() or "Timeout" in err_str:
            return f"{prefix}LLM 服务响应超时，请稍后重试。"
        if "401" in err_str or "Unauthorized" in err_str or "invalid_api_key" in err_str:
            return f"{prefix}LLM API Key 无效或已过期。"
        if "429" in err_str or "rate_limit" in err_str.lower():
            return f"{prefix}LLM 请求频率超限，请稍后重试。"
        if "all providers failed" in err_str:
            return f"{prefix}所有 LLM 服务均不可用，请检查配置。"
        return f"{prefix}LLM 调用出错，请查看日志。"

    def _get_conf_str(self, key: str, default: str) -> str:
        try:
            value = self.config.get(key) if isinstance(self.config, dict) else None
            if isinstance(value, str):
                return value.strip()
        except Exception:
            pass
        return default

    def _get_conf_bool(self, key: str, default: bool) -> bool:
        try:
            value = self.config.get(key) if isinstance(self.config, dict) else None
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("1", "true", "yes", "on"):
                    return True
                if lowered in ("0", "false", "no", "off"):
                    return False
        except Exception:
            pass
        return default

    def _get_conf_int(
        self, key: str, default: int, min_v: int = 1, max_v: int = 120000
    ) -> int:
        try:
            value = self.config.get(key) if isinstance(self.config, dict) else None
            if isinstance(value, int):
                return max(min(value, max_v), min_v)
            if isinstance(value, str) and value.strip().isdigit():
                return max(min(int(value.strip()), max_v), min_v)
        except Exception:
            pass
        return default

    def _get_conf_list_str(self, key: str) -> List[str]:
        try:
            value = self.config.get(key) if isinstance(self.config, dict) else None
            if isinstance(value, list):
                result: List[str] = []
                for item in value:
                    if isinstance(item, (str, int)):
                        text = str(item).strip()
                        if text:
                            result.append(text)
                return result
            if isinstance(value, str) and value.strip():
                return [item.strip() for item in re.split(r"[\s,，、]+", value) if item.strip()]
        except Exception:
            pass
        return []

    def _get_config_provider(self, key: str) -> Optional[Any]:
        try:
            provider_id = self.config.get(key) if isinstance(self.config, dict) else None
            if isinstance(provider_id, str):
                provider_id = provider_id.strip()
            if provider_id:
                return self.context.get_provider_by_id(provider_id=provider_id)
        except Exception as exc:
            logger.warning("zssm_explain: provider id lookup failed for %s: %s", key, exc)
        return None

    def _get_domain_blacklist(self) -> Set[str]:
        result: Set[str] = set()
        for item in self._get_conf_list_str(URL_DOMAIN_BLACKLIST_KEY):
            domain = item.strip().lower().lstrip(".")
            if domain:
                result.add(domain)
        return result

    def _is_domain_blacklisted(self, url: str) -> bool:
        blacklist = self._get_domain_blacklist()
        if not blacklist:
            return False
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        if not host:
            return False
        if host in blacklist:
            return True
        return any(host.endswith("." + domain) for domain in blacklist)

    async def _build_system_prompt(self, event: AstrMessageEvent) -> str:
        return await build_system_prompt_for_event(
            self.context,
            event.unified_msg_origin,
            keep_original_persona=self._get_conf_bool(
                KEEP_ORIGINAL_PERSONA_KEY, DEFAULT_KEEP_ORIGINAL_PERSONA
            ),
        )

    @staticmethod
    def _extract_access_wall_message(content: Optional[str]) -> Optional[str]:
        if isinstance(content, str) and content.strip() == ACCESS_WALL_SENTINEL:
            return "目标页面存在登录或访问限制，当前不会返回该页面的总结内容。"
        return None

    def _format_explain_output(
        self, content: str, elapsed_sec: Optional[float] = None
    ) -> str:
        body = str(content or "").strip()
        if not body:
            return ""
        if not isinstance(elapsed_sec, (int, float)) or elapsed_sec <= 0:
            return body
        return f"{body}\n\ncost: {elapsed_sec:.3f}s"

    def _should_suppress_errors(self) -> bool:
        return self._get_conf_bool(SILENT_FAIL_KEY, False)

    def _should_intercept_access_wall(self) -> bool:
        return self._get_conf_bool(INTERCEPT_ACCESS_WALL_KEY, True)

    def _build_error_reply_plan(self, message: str) -> "_ReplyPlan":
        logger.warning("zssm_explain: explain plan failed: %s", message)
        if self._should_suppress_errors():
            return self._ReplyPlan(message="", stop_event=False)
        return self._ReplyPlan(message=message, stop_event=True)

    def _should_dedupe_processed_urls(self) -> bool:
        return self._get_conf_bool(DEDUP_ENABLED_KEY, DEFAULT_DEDUP_ENABLED)

    def _get_dedupe_limit(self) -> int:
        return self._get_conf_int(DEDUP_LIMIT_KEY, DEFAULT_DEDUP_LIMIT, 10, 50000)

    async def _load_processed_urls(self) -> List[Dict[str, Any]]:
        try:
            payload = await self.get_kv_data(PROCESSED_URLS_KV_KEY, [])
        except Exception:
            return []

        if not isinstance(payload, list):
            return []

        result: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            ts = item.get("ts")
            if not url:
                continue
            try:
                ts_value = float(ts)
            except Exception:
                ts_value = 0.0
            result.append({"url": url, "ts": ts_value})
        return result

    async def _save_processed_urls(self, items: List[Dict[str, Any]]) -> None:
        try:
            await self.put_kv_data(PROCESSED_URLS_KV_KEY, items)
        except Exception as exc:
            logger.warning("zssm_explain: failed to save processed urls: %s", exc)

    async def _is_processed_url(self, url: str) -> bool:
        if not self._should_dedupe_processed_urls():
            return False
        target = str(url or "").strip()
        if not target:
            return False
        for item in await self._load_processed_urls():
            if item.get("url") == target:
                return True
        return False

    async def _mark_processed_url(self, url: str) -> None:
        if not self._should_dedupe_processed_urls():
            return
        target = str(url or "").strip()
        if not target:
            return

        items = [item for item in await self._load_processed_urls() if item.get("url") != target]
        items.append({"url": target, "ts": time.time()})
        items.sort(key=lambda item: float(item.get("ts") or 0))
        limit = self._get_dedupe_limit()
        if len(items) > limit:
            items = items[-limit:]
        await self._save_processed_urls(items)

    def _already_handled(self, event: AstrMessageEvent) -> bool:
        try:
            extras = event.get_extra()
            if isinstance(extras, dict) and extras.get("zssm_handled"):
                return True
        except Exception:
            pass
        try:
            event.set_extra("zssm_handled", True)
        except Exception:
            pass
        return False

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        gid = None
        try:
            gid = event.get_group_id()
        except Exception:
            gid = None
        if not gid:
            try:
                gid = getattr(getattr(event, "message_obj", None), "group_id", None)
            except Exception:
                gid = None
        if not gid:
            return True

        mode = self._get_conf_str(GROUP_LIST_MODE_KEY, "none").lower()
        allowed_groups = self._get_conf_list_str(GROUP_LIST_KEY)
        gid = str(gid).strip()
        if mode == "whitelist":
            return gid in allowed_groups if allowed_groups else False
        if mode == "blacklist":
            return gid not in allowed_groups if allowed_groups else True
        return True

    async def _build_explain_plan(
        self,
        inline: str,
    ) -> "_LLMPlan | _ReplyPlan":
        text = inline.strip()
        if not text:
            return self._ReplyPlan(message="请发送要解释的链接。")

        urls = extract_urls_from_text(text)
        if not urls:
            return self._ReplyPlan(message="请发送要解释的链接。")

        target_url = urls[0]
        if self._is_domain_blacklisted(target_url):
            logger.info("zssm_explain: url blocked by domain blacklist: %s", target_url[:100])
            return self._build_error_reply_plan("该链接的域名已被屏蔽，无法解析。")

        timeout_sec = self._get_conf_int(
            URL_FETCH_TIMEOUT_KEY, DEFAULT_URL_FETCH_TIMEOUT, 2, 60
        )
        if is_twitter_url(target_url):
            try:
                twitter_ctx = await prepare_twitter_prompt(
                    target_url,
                    timeout_sec=timeout_sec,
                )
            except TwitterParseError as exc:
                return self._build_error_reply_plan(str(exc))
            return self._LLMPlan(
                user_prompt=twitter_ctx.prompt,
                images=twitter_ctx.images,
                cleanup_paths=list(getattr(twitter_ctx, "cleanup_paths", []) or []),
            )

        if is_reddit_url(target_url):
            try:
                reddit_ctx = await prepare_reddit_prompt(
                    target_url,
                    timeout_sec=timeout_sec,
                    last_fetch_info=self._last_fetch_info,
                )
            except RedditParseError as exc:
                return self._build_error_reply_plan(str(exc))
            return self._LLMPlan(
                user_prompt=reddit_ctx.prompt,
                images=reddit_ctx.images,
                cleanup_paths=[],
            )

        if match_zhihu_url(target_url):
            try:
                zhihu_ctx = await prepare_zhihu_prompt(
                    target_url,
                    cookie=self._get_conf_str(ZHIHU_COOKIE_KEY, ""),
                    timeout_sec=timeout_sec,
                )
            except ZhihuParseError as exc:
                return self._build_error_reply_plan(str(exc))
            cleanup_paths = [
                item for item in zhihu_ctx.images if isinstance(item, str) and os.path.isabs(item)
            ]
            return self._LLMPlan(
                user_prompt=zhihu_ctx.prompt,
                images=zhihu_ctx.images,
                cleanup_paths=cleanup_paths,
            )

        if is_bilibili_url(target_url):
            try:
                bilibili_ctx = await prepare_bilibili_prompt(
                    target_url,
                    timeout_sec=timeout_sec,
                )
            except BilibiliParseError as exc:
                logger.warning(
                    "zssm_explain: bilibili specialized parsing failed, fallback to generic url parser: %s",
                    str(exc),
                )
                bilibili_ctx = None
            if bilibili_ctx is not None:
                return self._LLMPlan(
                    user_prompt=bilibili_ctx.prompt,
                    images=[],
                    cleanup_paths=[],
                )

        max_chars = self._get_conf_int(
            URL_MAX_CHARS_KEY, DEFAULT_URL_MAX_CHARS, min_v=1000, max_v=50000
        )
        user_prompt_template = build_url_user_prompt_template(
            intercept_access_wall=self._should_intercept_access_wall()
        )
        if is_coolapk_url(target_url):
            url_ctx = await prepare_coolapk_prompt(
                target_url,
                timeout_sec,
                self._last_fetch_info,
                max_chars=max_chars,
                user_prompt_template=user_prompt_template,
            )
        else:
            url_ctx = await prepare_url_prompt(
                target_url,
                timeout_sec,
                self._last_fetch_info,
                max_chars=max_chars,
                user_prompt_template=user_prompt_template,
            )
        if not url_ctx:
            return self._build_error_reply_plan(
                build_url_failure_message(self._last_fetch_info)
            )

        user_prompt, _text, images = url_ctx
        cleanup_paths = [
            item for item in images if isinstance(item, str) and os.path.isabs(item)
        ]
        return self._LLMPlan(
            user_prompt=user_prompt,
            images=images,
            cleanup_paths=cleanup_paths,
        )

    async def _execute_explain_plan(
        self, event: AstrMessageEvent, plan: "_LLMPlan | _ReplyPlan"
    ):
        if isinstance(plan, self._ReplyPlan):
            if plan.message.strip():
                yield self._reply_text_result(event, plan.message)
            if plan.stop_event:
                try:
                    event.stop_event()
                except Exception:
                    pass
            return

        try:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        except Exception as exc:
            logger.error("zssm_explain: get provider failed: %s", exc)
            provider = None
        if not provider:
            logger.warning("zssm_explain: no provider available for current request")
            if not self._should_suppress_errors():
                yield self._reply_text_result(
                    event, "未检测到可用的大语言模型提供商，请先在 AstrBot 配置中启用。"
                )
            else:
                logger.warning("zssm_explain: reply suppressed because silent_fail is enabled")
            return

        system_prompt = await self._build_system_prompt(event)
        image_urls = self._llm.filter_supported_images(plan.images)

        try:
            start_ts = time.perf_counter()
            call_provider = self._llm.select_primary_provider(
                session_provider=provider, image_urls=image_urls
            )
            llm_resp = await self._llm.call_with_fallback(
                primary=call_provider,
                session_provider=provider,
                user_prompt=plan.user_prompt,
                system_prompt=system_prompt,
                image_urls=image_urls,
            )
            try:
                await call_event_hook(event, EventType.OnLLMResponseEvent, llm_resp)
            except Exception:
                pass

            reply_text = getattr(llm_resp, "completion_text", None)
            if not isinstance(reply_text, str) or not reply_text.strip():
                reply_text = self._llm.pick_llm_text(llm_resp)
            access_wall_message = (
                self._extract_access_wall_message(reply_text)
                if self._should_intercept_access_wall()
                else None
            )
            if access_wall_message:
                logger.warning(
                    "zssm_explain: model detected access wall, suppress=%s",
                    self._should_suppress_errors(),
                )
                if self._should_suppress_errors():
                    logger.warning(
                        "zssm_explain: access wall reply suppressed because silent_fail is enabled"
                    )
                    logger.warning(
                        "zssm_explain: suppressed access wall raw model output:\n%s",
                        str(reply_text or "").strip(),
                    )
                    return
                reply_text = access_wall_message
            elapsed = time.perf_counter() - start_ts
            yield self._reply_text_result(
                event, self._format_explain_output(reply_text, elapsed_sec=elapsed)
            )
            try:
                event.stop_event()
            except Exception:
                pass
            setattr(plan, "_completed", True)
        except asyncio.TimeoutError:
            logger.warning("zssm_explain: llm call timed out")
            if not self._should_suppress_errors():
                yield self._reply_text_result(
                    event, "解释超时，请稍后重试或换一个模型提供商。"
                )
            else:
                logger.warning("zssm_explain: timeout reply suppressed because silent_fail is enabled")
        except Exception as exc:
            logger.error("zssm_explain: LLM call failed: %s", exc)
            if not self._should_suppress_errors():
                yield self._reply_text_result(event, self._format_llm_error(exc, "解释"))
            else:
                logger.warning("zssm_explain: LLM failure reply suppressed because silent_fail is enabled")

    async def zssm(self, event: AstrMessageEvent):
        cleanup_paths: List[str] = []
        target_url = ""
        try:
            if not self._is_group_allowed(event):
                return
            try:
                inline = event.get_message_str()
            except Exception:
                inline = getattr(event, "message_str", "") or ""
            if not isinstance(inline, str):
                inline = ""

            urls = extract_urls_from_text(inline)
            target_url = urls[0] if urls else ""
            if target_url and await self._is_processed_url(target_url):
                logger.info(
                    "zssm_explain: skip duplicated processed url: %s",
                    target_url[:200],
                )
                return

            event.should_call_llm(True)
            if self._already_handled(event):
                return

            plan = await self._build_explain_plan(inline)
            cleanup_paths = list(getattr(plan, "cleanup_paths", []) or [])
            async for item in self._execute_explain_plan(event, plan):
                yield item
            if target_url and getattr(plan, "_completed", False):
                await self._mark_processed_url(target_url)
        except Exception as exc:
            logger.error("zssm_explain: handler crashed: %s", exc)
            if not self._should_suppress_errors():
                yield self._reply_text_result(
                    event, "解释失败：插件内部异常，请稍后再试或联系管理员。"
                )
                try:
                    event.stop_event()
                except Exception:
                    pass
            else:
                logger.warning("zssm_explain: handler failure reply suppressed because silent_fail is enabled")
        finally:
            for path in cleanup_paths:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass

    async def terminate(self):
        return

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_zssm(self, event: AstrMessageEvent):
        try:
            if not self._is_group_allowed(event):
                return
        except Exception:
            return

        try:
            text = event.get_message_str()
        except Exception:
            text = getattr(event, "message_str", "") or ""
        if not isinstance(text, str) or not text.strip():
            return
        if not extract_urls_from_text(text):
            return
        async for item in self.zssm(event):
            yield item
