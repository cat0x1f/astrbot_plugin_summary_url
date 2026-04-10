from __future__ import annotations

from typing import Any, Optional

# === 默认提示词常量（集中管理，可供用户修改） ===

DEFAULT_SYSTEM_PROMPT = (
    "你是中文信息提炼助手，只基于提供内容总结，不得编造或补充未出现的信息。\n"
    "输出必须使用 Markdown，并遵循：\n"
    "1. 第一行：3~8 个关键词，用「 | 」分隔；\n"
    "2. 第二部分：1~2 句总述；\n"
    "3. 后续分段：补充说明与要点。\n"
    "语言要求：客观、简洁、偏年轻表达，不写套话。\n"
    "格式要求：统一使用「直角引号」。\n"
    "禁止输出推理过程、原文链接、版权信息、无依据内容、互动信息、网站本身信息。"
)

DEFAULT_URL_USER_PROMPT = (
    "你将看到网页关键信息，请判断是否为可阅读正文页。\n"
    "若为登录/注册/验证/权限受限等页面，直接输出：[[ACCESS_WALL]]。\n"
    "否则，仅基于提供内容生成摘要（2-8句，中文），不得编造或补充未出现的信息。\n"
    "要求：客观、简洁、偏年轻表达，避免套话；使用「直角引号」。\n"
    "网址: {url}\n"
    "标题: {title}\n"
    "描述: {desc}\n"
    "正文片段:\n{snippet}"
)

DEFAULT_URL_USER_PROMPT_ALLOW_ACCESS_WALL = (
    "你将看到一个网页的关键信息，请总结当前页面实际展示出来的内容。"
    "输出简版摘要（2-8句，中文）。避免口水话，保留事实与结论。不得编造或补充未出现的信息\n"
    "要求：客观、简洁、偏年轻表达，避免套话；使用「直角引号」。\n"
    "网址: {url}\n"
    "标题: {title}\n"
    "描述: {desc}\n"
    "正文片段: \n{snippet}"
)


def build_url_user_prompt_template(*, intercept_access_wall: bool) -> str:
    if intercept_access_wall:
        return DEFAULT_URL_USER_PROMPT
    return DEFAULT_URL_USER_PROMPT_ALLOW_ACCESS_WALL


def build_system_prompt() -> str:
    """返回系统提示词（供 LLM 调用使用）。"""
    return DEFAULT_SYSTEM_PROMPT


async def build_system_prompt_for_event(
    context: Any,
    umo: Any,
    *,
    keep_original_persona: bool,
) -> str:
    """根据会话人格（可选）构造系统提示词。

    - keep_original_persona=False：直接返回默认系统提示词；
    - keep_original_persona=True：若 context.persona_manager 存在，则尝试读取当前会话人格 prompt，
      并替换默认系统提示词的首行（保留其余结构化输出约束）。
    """
    sp = build_system_prompt()
    if not keep_original_persona:
        return sp

    persona_mgr = getattr(context, "persona_manager", None)
    if persona_mgr is None:
        return sp

    persona_prompt: Optional[str] = None
    try:
        personality = await persona_mgr.get_default_persona_v3(umo)
        if isinstance(personality, dict):
            persona_prompt = personality.get("prompt")
        else:
            persona_prompt = getattr(personality, "prompt", None)
    except Exception:
        persona_prompt = None

    if not isinstance(persona_prompt, str) or not persona_prompt.strip():
        return sp

    base_lines = sp.splitlines()
    rest_lines = base_lines[1:] if len(base_lines) > 1 else []
    merged_lines: List[str] = [persona_prompt.strip()]
    merged_lines.extend(rest_lines)
    return "\n".join(merged_lines)
