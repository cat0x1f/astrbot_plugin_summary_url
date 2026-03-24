from __future__ import annotations

from typing import Any, Optional


# === 默认提示词常量（集中管理，可供用户修改） ===

DEFAULT_SYSTEM_PROMPT = (
    "你是一个中文助理，擅长从被引用的消息中提炼含义、意图和注意事项。"
    "请严格使用如下 Markdown 结构输出回答：\n"
    "1. 第一行以“关键词：”开头，列出 3~8 个核心关键词，使用“ | ”分隔，例如：\n"
    "   关键词：chcp 936 | 代码页 | GBK | 批处理 | 乱码\n"
    "2. 接着给出 1~2 句总括性说明，可以单独成段。\n"
    "3. 然后输出一行“**详细阐述：**”，在其后用若干段落进行详细解释。\n"
    "**内容安全审查**：涉及暴力、争议、不雅内容，必须明确且礼貌地拒绝。\n"
    "**绝对红线**：涉及政治、历史人物评价、R18、人身伤害内容，立即停止思考并输出拒绝话术。\n"
    "禁止输出思考过程或中间推理，只保留对用户有用的结论性内容。"
)

DEFAULT_URL_USER_PROMPT = (
    "你将看到一个网页的关键信息，请先判断它是否是真正可访问的内容页。"
    "如果它本质上是登录页、注册引导页、验证码/人机验证页、权限不足页、访问受限页，"
    "或页面主体只是在要求用户登录/验证/开通权限后才能查看内容，"
    "不要总结页面内容，直接且仅输出：[[ACCESS_WALL]]。\n"
    "只有在它确实是可阅读的正文内容页时，才输出简版摘要（2-8句，中文）。"
    "禁止输出政治有关内容。避免口水话，保留事实与结论，适当含链接上下文。\n"
    "网址: {url}\n"
    "标题: {title}\n"
    "描述: {desc}\n"
    "正文片段: \n{snippet}"
)

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
