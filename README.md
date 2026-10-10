# astrbot_plugin_summary_url

发送链接后自动返回网页图文解释的 AstrBot 插件。

支持普通网页、微信公众号文章、知乎、小红书、Coolapk、Twitter(X)、Reddit，以及哔哩哔哩视频（含字幕）。

> 修改自 [astrbot_zssm_explain](https://github.com/exynos967/astrbot_zssm_explain)，并整合了 [astrbot_plugin_xhs_reader](https://github.com/Akusative/astrbot_plugin_xhs_reader) 的小红书解析与 [astrbot_plugin_biliread](https://github.com/SodaCodeSave/astrbot_plugin_biliread) 的 B 站字幕总结能力。

---

## 功能特性

- **自动识别链接**：无需指令，消息中出现受支持的链接即会自动抓取并交给大模型生成简洁解释。
- **普通网页**：抓取 HTML，提取标题、描述与正文片段生成摘要；命中登录页 / 验证码页时可选择拦截。
- **微信公众号文章**：专用抓取流程，转为便于阅读的格式后交给模型。
- **知乎**：文章 / 回答 / 问题 / 想法 专用解析，提取作者、正文、图片与统计数据（需配置 `zhihu_cookie`）。
- **小红书**：解析笔记标题、作者、正文、话题、互动数据、首屏评论，并下载笔记图片交给图文模型一起理解。
- **哔哩哔哩视频**：优先获取视频字幕（需配置 B 站 Cookie）并据此总结视频内容；无字幕时退回按标题与简介解释。
- **Coolapk / Twitter / Reddit**：内置对应站点的专用解析。
- **Cookie 检测工具**：内置 LLM 工具 `check_cookie`，说一句「爪爪你测一下cookie」即可让机器人检测 B 站 / 知乎 Cookie 是否有效。
- **多模型支持**：文本解释与图文解释可分别指定 Provider，调用失败时自动回退到当前会话 Provider。
- **群聊权限控制、域名黑名单、重复链接去重、静默失败**等可选配置。

---

## 触发方式

插件监听全部消息，只要消息文本中包含受支持的链接，就会自动处理并回复该链接的解释，例如：

```
https://www.zhihu.com/question/12345678
看看这个 https://www.xiaohongshu.com/explore/xxxxxxxx
https://www.bilibili.com/video/BV1xxxxxxxxx
https://xhslink.com/xxxxxxx
```

无需 `/命令` 前缀，也不需要通过「回复」的方式触发。

---

## 安装

1. 在 AstrBot 管理后台的 **插件市场** 中搜索 `summary_url` 安装；或手动将本仓库放入 `data/plugins/` 目录。
2. 依赖安装（如插件未自动安装依赖，请手动执行）：

   ```bash
   pip install -r requirements.txt
   ```

3. 重启 AstrBot 即可生效。

> 网络抓取优先使用 `curl_cffi`（已在 `requirements.txt` 中声明），并会回退到 `aiohttp` / `urllib`。

---

## 配置说明

进入 AstrBot 后台 → 插件 → `summary_url` → 配置。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `group_list_mode` | string | `none` | 群聊权限模式：`whitelist` 仅允许列表内群 / `blacklist` 拒绝列表内群 / `none` 不限制 |
| `group_list` | list | `[]` | 群号列表，仅在 whitelist / blacklist 模式下生效 |
| `url_domain_blacklist` | list | `[]` | 域名黑名单，命中（含子域名）的链接不再解析 |
| `url_timeout_sec` | int | `20` | 链接抓取超时时间（秒） |
| `url_max_chars` | int | `6000` | 传给模型的网页正文截取上限 |
| `silent_fail` | bool | `false` | 静默失败：抓取 / 解析 / 模型异常时不返回任何提示 |
| `intercept_access_wall` | bool | `true` | 识别到登录页 / 验证码页时不返回总结；关闭则总结当前可见内容 |
| `dedupe_processed_urls` | bool | `true` | 跳过过去已成功处理过的同一链接 |
| `dedupe_processed_urls_limit` | int | `500` | 已处理链接记录上限，超出后清理最旧记录 |
| `llm_timeout_sec` | int | `90` | 大模型调用超时时间（秒） |
| `keep_original_persona` | bool | `true` | 尽量沿用当前会话的人设和语气 |
| `text_provider_id` | string | 空 | 文本解释优先使用的 Provider；留空使用当前会话 Provider |
| `image_provider_id` | string | 空 | 图文解释优先使用的 Provider；留空自动选择支持图片输入的 Provider |
| `zhihu_cookie` | string | 空 | 知乎 Cookie，用于知乎链接解析 |
| `bilibili_sessdata` | string | 空 | B 站 `SESSDATA`，用于获取视频字幕 |
| `bilibili_jct` | string | 空 | B 站 `bili_jct`，与 `SESSDATA` 搭配使用 |
| `bilibili_max_subtitle_length` | int | `4000` | 传给模型的 B 站字幕截取上限 |
| `xiaohongshu_enabled` | bool | `true` | 是否启用小红书解析 |
| `xiaohongshu_max_desc_chars` | int | `2000` | 小红书正文截取上限 |
| `xiaohongshu_max_comments` | int | `8` | 小红书最多读取的评论数，设为 `0` 不读取评论 |
| `xiaohongshu_max_images` | int | `9` | 小红书最多读取的图片数，设为 `0` 禁用图片下载（最多 18） |
| `xiaohongshu_proxy` | string | 空 | 小红书抓取代理，如 `http://127.0.0.1:7890`；留空使用默认网络 |

---

## Cookie 获取教程

### 知乎 Cookie（`zhihu_cookie`）

1. 在浏览器登录 [知乎](https://www.zhihu.com)。
2. 按 `F12` 打开开发者工具，切换到 **网络** 标签页，刷新页面。
3. 点击任意请求，在 **请求标头** 中找到 `Cookie` 字段。
4. 复制整段 Cookie 值，填入插件配置的 `zhihu_cookie`。

> 未配置 `zhihu_cookie` 时，知乎链接将无法解析。

### B 站 Cookie（`bilibili_sessdata` / `bilibili_jct`）

B 站的字幕接口需要登录态，请提供账号 Cookie 中的 `SESSDATA` 和 `bili_jct` 两个字段。

**使用浏览器插件（适合新手）**

1. 安装 [Cookie-Editor](https://microsoftedge.microsoft.com/addons/detail/cookieeditor/neaplmfkghagebokkhpjpoebhdledlfi) 等 Cookie 管理插件。
2. 登录 B 站后打开插件，分别复制 `SESSDATA` 和 `bili_jct` 的值。
3. 填入插件配置的 `bilibili_sessdata` 与 `bilibili_jct`。

**使用开发者工具（适合进阶）**

1. 在浏览器登录 [Bilibili](https://www.bilibili.com)。
2. 按 `F12` 打开开发者工具，切换到 **网络** 或 **应用** 标签页。
3. 找到任意请求的 `Cookie`，在其中定位 `SESSDATA=xxxxxx` 与 `bili_jct=xxxxxx`。
4. 把 `=` 后面的值分别填入配置项。

> 未配置 B 站 Cookie 时，插件仍可解释视频，但只能基于标题与简介，无法总结字幕内容。

---

## Cookie 检测

插件注册了一个 LLM 工具 `check_cookie`，可直接检测已配置的 Cookie 是否有效：

- 直接对机器人说 **「爪爪你测一下cookie」**（或「cookie 还能用吗」「cookie 过期了吗」等类似说法）即可，模型会自行调用该工具。
- 工具会检测 **哔哩哔哩**（`bilibili_sessdata` / `bilibili_jct`）与 **知乎**（`zhihu_cookie`）的登录状态，并回复示例：

  ```
  Cookie 检测结果
  · 哔哩哔哩：✅ 有效（昵称：xxx）
  · 知乎：❌ 无效或已过期
  · 小红书：无需 Cookie
  ```

- 未配置的项显示「未配置」；网络异常显示「检测失败（网络异常）」。
- 需要当前会话使用的模型支持**函数调用（function calling）**；若模型不支持，AstrBot 会自动移除工具，此时不会触发检测。

---

## 常见问题 / 已知限制

- **不是所有 B 站视频都有字幕**：若 UP 主未上传且 B 站未生成 AI 字幕，则无法获取字幕，此时仅按标题与简介解释，属于正常现象。
- **Cookie 过期**：B 站 `SESSDATA` 有效期通常为几个月，出现鉴权失败时重新登录并更新配置即可。
- **风控限制**：请求过于频繁可能触发知乎 / 小红书 / B 站风控，请勿短时间内高频测试。
- **小红书图片**：图片会走图文模型（`image_provider_id` 或自动选择的支持图片 Provider）。若当前环境没有视觉模型，建议将 `xiaohongshu_max_images` 设为 `0`，仅读取文字。
- **Token 消耗**：将较长字幕或正文交给大模型会消耗较多 Token，长视频建议使用成本较低的小模型。
- **复杂页面**：强 JS 渲染、强登录依赖的站点仍可能抓取失败。

---

## 特别感谢

- 原项目作者 [exynos967](https://github.com/exynos967/astrbot_zssm_explain)。
- 小红书解析参考 [Akusative/astrbot_plugin_xhs_reader](https://github.com/Akusative/astrbot_plugin_xhs_reader)。
- B 站字幕总结参考 [SodaCodeSave/astrbot_plugin_biliread](https://github.com/SodaCodeSave/astrbot_plugin_biliread)。
