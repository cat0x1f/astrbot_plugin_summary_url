<div align="center">

![:name](https://count.getloli.com/@:astrbot_zssm_explain?name=%3Aastrbot_zssm_explain&theme=miku&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

## zssm_explain 插件说明

一个为 AstrBot 提供链接自动解释能力的插件。发送链接后，插件会抓取网页内容并返回中文摘要。

---

</div>

## 功能概览

- 链接自动解释  
  - 消息中出现 URL 时自动触发。  
  - 当前一条消息默认只处理第一个链接。

- 网页摘要  
  - 自动抓取网页 HTML，提取标题、描述与正文片段，输出中文简版摘要。  
  - 支持普通网页、微信公众号文章、知乎文章 / 回答 / 问题 / 想法链接。

- 访问受限识别  
  - 若页面本质上是登录页、验证码页或访问受限页，不返回总结内容。  

---

## 触发方式

- 直接发送链接即可自动触发。  
- 不再支持关键词触发、命令触发、回复消息解释、视频解释、群文件解释或 PDF 解析。

---

## 提示词与输出格式

- 系统提示词与用户提示词模板集中在 `prompt_utils.py`：  
  - `DEFAULT_SYSTEM_PROMPT`：约束 LLM 输出结构，如「关键词行 + 总结 + **详细阐述**」。  
  - `DEFAULT_URL_USER_PROMPT`：用于网页摘要，并要求模型识别访问墙页面。  
- 如需自定义输出格式（例如改为项目管理风格、问答风格），建议修改 `prompt_utils.py` 中的常量。

---

## 已知限制与 TODO

- 目前对部分复杂网站（强 JS 渲染、严格登录态依赖）仍可能抓取失败。  
- 一条消息中如果有多个链接，当前默认只处理第一个。

---

## 特别感谢

- [Reina](https://github.com/Ri-Nai) 本插件参考了他的json消息处理代码并完善了json卡片消息的处理
- [氕氙](https://github.com/piexian) 感谢稀有气体同学的PR
- [プリン](https://github.com/zouyonghe) 感谢热心群u的PR
- [回归天空](https://github.com/SXP-Simon) 感谢回归天空同学的PR
- [xunxiing](https://github.com/xunxiing) 感谢到此为止吧同学的PR
- [晴空](https://github.com/XXXxx7258) 感谢晴空的PR
