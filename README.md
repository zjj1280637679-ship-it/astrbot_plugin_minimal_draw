<div align="center">

<img src="logo.png" width="160" alt="astrbot_plugin_minimal_draw logo" />

# 🎨 astrbot_plugin_minimal_draw

**极简视觉视野 · 全渠道任意来源图片转发 · 管理员双档位分权 OpenAI 生图/多图融合/GIF 插件**

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-27a7d8?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/Version-v1.2.0-ff69b4?style=for-the-badge)](https://github.com/zjj1280637679-ship-it/astrbot_plugin_minimal_draw)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![OpenAI Cache](https://img.shields.io/badge/Prompt_Cache-95%25%2B_Hit-10a37f?style=for-the-badge)](https://platform.openai.com/docs/guides/prompt-caching)
[![Stars](https://img.shields.io/github/stars/zjj1280637679-ship-it/astrbot_plugin_minimal_draw?style=for-the-badge&color=e9b949)](https://github.com/zjj1280637679-ship-it/astrbot_plugin_minimal_draw/stargazers)

_✨ 支持 **OneBot v11**（NapCat / LLOneBot / Lagrange）、**QQ 官方机器人**、**Telegram**、**飞书**、**Discord**、**微信** 等主流平台 ✨_

</div>

---

## 🌟 你能用它做什么？

| 你想做的事 | 本插件如何帮你实现 |
| :--- | :--- |
| **群里天天有人发图，但不想白烧视觉 Token** | **平时 0 视觉 Token 消耗**：旁路静默缓存群内所有图片（含直发图、原图文件、合并转发内嵌图、卡片封面），仅当对话需要看图时由主模型按需调用 `view_recent_images(count=N)` 一次性观察缩略图。 |
| **把合并转发里的图拆出来 / 多图打包转发** | 调用 `forward_images` 零成本提取 **QQ 合并转发聊天记录** 内的全部图片、跨会话私聊图、群友/群头像或防盗链外链图；支持单图直发或打包成合并转发卡片（`Comp.Nodes`），还能跨群/私聊定向投递。 |
| **拿群友头像、聊天记录图、外链图做多图融合** | `draw_image` 支持通过 `,` 或 `+` 任意混搭最多 **6 张不同来源参考图**（如 `"self_avatar, at_avatar"`、`"IMG_1, IMG_3"`、`"forward"`），内置原生多图数组上传与智能网格拼接双重兜底。 |
| **直接生成动态 GIF 表情包 / 静态图转动图** | 支持 `output_format="gif"` 原生生成多帧透明/循环动态 GIF，或将群友头像、聊天图片一键转化为动态表情包。 |
| **管理员用旗舰精绘模型，普通群友用便宜极速模型** | 日常默认使用便宜极速档（如 `gpt-image-2.6-flare` + `low`）；管理员在上下文中表达精绘意图时自动解锁旗舰档（如 `gpt-image-2.6-sunburst` + `high`），并采用尾部临时注入保护 **95%+ OpenAI 缓存命中率**。 |

---

## 📦 安装方法

### 方式一：AstrBot 管理面板一键安装（推荐）
1. 打开 AstrBot WebUI 管理后台 -> **插件管理** -> **右上角「安装插件」**；
2. 粘贴本仓库地址：
   ```text
   https://github.com/zjj1280637679-ship-it/astrbot_plugin_minimal_draw
   ```
3. 点击安装即可热加载生效（默认自动复用您在 AstrBot「服务提供商」中已配置的 OpenAI API Key，零配置开箱即用）。

### 方式二：手动克隆安装
```bash
cd AstrBot/data/plugins
git clone https://github.com/zjj1280637679-ship-it/astrbot_plugin_minimal_draw.git
```

---

## ✨ 核心特性详解

### 1. 👁️ 零闲置 Token 全渠道旁路嗅探 + 主动多图视野 (`view_recent_images`)
- **全形态静默嗅探入池**：
  - 普通聊天图片 (`Comp.Image`) 与动画表情
  - 用户以“文件”形式发送的原图附件 (`Comp.File`: `.png` / `.jpg` / `.gif` / `.webp`)
  - **QQ 合并转发聊天记录 (`Comp.Forward` / `Comp.Nodes`) 内嵌的所有图片**（自动递归拆包入池）
  - 分享卡片与音乐封面图 (`Comp.Share` / `Comp.Music`)
- **按需批量视觉观察**：当需要对比多张图片、查看上文配图或挑选改图素材时，主模型调用 `view_recent_images(count=5)` 获取最近 `N` 张图片的缩略图与唯一编号 `#IMG_n`；若当前群聊无图，还会**自动跨会话回退调取该用户在私聊或其他群刚发过的图片**。

### 2. 📤 任意来源图片提取与转发 (`forward_images`)
- **合并转发拆图**：将冗长的合并转发聊天记录里的所有图片一键提取并单独发出来（`image_source="forward"`）。
- **跨会话调图转发**：在私聊发图给机器人后，去群里说“把刚才我私聊发你的图发到群里”（`image_source="my_latest"`），或从指定群调图（`group:群号`）。
- **全系高清头像提取**：支持发送者头像（`self_avatar`）、被 `@` 群友头像（`at_avatar`，支持同时 `@` 多人）、任意指定 QQ 头像（`avatar:QQ号`）、机器人头像（`bot_avatar`）及群头像（`group_avatar` / `group_avatar:群号`）。
- **外链防盗链抓取**：支持任意 `http(s)://` 链接（内置 Pixiv / 微博 / B站等常见防盗链 `Referer` 适配）、Markdown 图片语法、`data:image` 与 `base64://`。
- **双模式发送 (`forward_mode`) + 跨会话投递 (`target`)**：
  - `direct`：直接作为原图消息发出（1~3 张默认直发）；
  - `merged`：自动打包为 **QQ 合并转发聊天记录卡片 (`Comp.Nodes`)** 发送（超过 3 张自动切换，干净不刷屏）；
  - `target`：支持 `"private"`（私聊发给提问者本人），管理员还可使用 `"group:群号"` 或 `"private:QQ号"` 跨群/跨人投递。

### 3. 🖌️ 任意来源图生图 / 多图融合 / 原生 GIF 动图 (`draw_image`)
- **全能多图融合 (`images.edit`)**：
  - `image_source` 支持用逗号（`,`）或加号（`+`）任意混搭最多 **6 张不同来源的参考图**；
  - 优先通过 OpenAI 原生多图数组 `image=[file1, file2, ...]` 上传；若遇到不支持多文件数组的兼容接口，自动触发 `_stitch_images_grid_png` 智能网格画布拼接兜底，确保 100% 融合成功。
- **原生 GIF 动图生成 (`output_format="gif"`)**：
  - 支持纯文生 GIF 动图，也支持将单张参考图或多张融合图转化为循环动态 GIF 表情包（可在插件设置中自定义帧数与单帧时长）。
- **上下文驱动的管理员双档位分权 + OpenAI 前缀缓存保护**：
  - 日常默认极速档：`gpt-image-2.6-flare` + `low`（180s 超时）；
  - 管理员专属旗舰档：`gpt-image-2.6-sunburst` + `high`（360s 超时），通过 `req.extra_user_content_parts` + `mark_as_temp()` 尾部临时注入权限提醒，严禁污染顶部 `system_prompt`，实测保持 **95%+ 的 OpenAI Prompt Caching 命中率**。

---

## 🛠️ LLM 工具与万能图片源 (`image_source`) 速查表

### 1. 三大 LLM 工具 (`@filter.llm_tool`) 与辅助指令

| 工具 / 指令 | 参数 | 核心用途 |
| :--- | :--- | :--- |
| `view_recent_images` | `count: int = 5` | 多图视觉观察：调取最近 `N` 张图片画面、编号 `#IMG_n`、发送者与配文 |
| `forward_images` | `image_source: str = "latest"`<br>`forward_mode: str = "auto"`<br>`caption: str = ""`<br>`target: str = ""` | 原图提取与转发：将任意来源的单图/多图直接发送或打包为合并转发卡片，支持跨会话投递 |
| `draw_image` | `prompt: str = ""`<br>`image_source: str = ""`<br>`output_format: str = "png"`<br>`size: str = ""`<br>`high_precision: bool = False` | 文生图 / 图生图 / 多图融合 / GIF 动图生成（支持管理员上下文双档位分权） |
| `/img_pool` | 无 | 查看当前会话图片缓存池状态、图片编号、角色与来源摘要 |
| `/img_clear` | 无 | 清空当前会话的图片缓存池 |

### 2. `image_source` 万能来源语法（`draw_image` 与 `forward_images` 通用，支持 `,` 或 `+` 组合）

| 来源类别 | 填写示例 | 解析说明 |
| :--- | :--- | :--- |
| **自动感知当前/引用** | `""` 或 `"auto"` | 自动提取本条消息（含直发图、原图文件、合并转发、文本 URL）及引用消息中的全部图片 |
| **合并转发拆图** | `"forward"` | 递归提取当前消息或引用消息中 QQ 合并转发聊天记录内的所有图片 |
| **引用消息图片** | `"quoted"` | 提取被回复/引用消息中的所有图片 |
| **本会话编号/区间** | `"IMG_1"` / `"IMG_1,IMG_3"` / `"1-4"` | 按图片池编号单选、多选或连续区间批量选取 |
| **相对位置选取** | `"latest"` / `"-2"` / `"last:5"` | 最近第 1 张、倒数第 2 张、或最近连续 5 张图片 |
| **按发送角色筛选** | `"bot_latest"` / `"user_latest"` | 当前会话中机器人最近生成的图 / 群友最近发的图 |
| **跨会话调取图片** | `"my_latest"` / `"global_latest"` / `"group:群号"` / `"private:QQ号"` | 调取当前用户在私聊/其他群刚发的图，或指定群/私聊的最新图 |
| **QQ 个人与群头像** | `"self_avatar"` / `"at_avatar"` / `"bot_avatar"` / `"avatar:QQ号"` / `"group_avatar"` | 提取自己、被 `@` 群友（支持同时 `@` 多人）、机器人、指定 QQ 号或当前群的 640px 高清头像 |
| **外部链接与本地文件** | `"https://..."` / `"data:image/..."` / `"C:/..."` | 任意网络图片 URL（含防盗链适配）、Markdown 图片、Base64 或本地路径 |

---

## ⚙️ 插件配置项说明 (`_conf_schema.json`)

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `model` | `gpt-image-2.6-flare` | 日常默认生图模型（便宜极速档，也可填 `gpt-image-1-mini` 等） |
| `default_quality` | `low` | 日常默认画质档位（`low` / `medium` / `high` / `auto`） |
| `high_precision_model` | `gpt-image-2.6-sunburst` | 管理员专属高精度模型（旗舰精绘档） |
| `high_precision_quality` | `high` | 管理员触发高精度时的画质档位 |
| `admin_qq_list` | `""` | 除 AstrBot 全局 `admins_id` 外，额外允许触发高精度模型的 ID 列表（逗号分隔） |
| `non_admin_high_policy` | `downgrade` | 普通群友要求高精度时的策略：`downgrade`（静默降级为日常模型）或 `reject`（拒绝并提示） |
| `non_admin_cooldown_seconds` | `10` | 普通群友生图冷却时间（秒），管理员不受限制 |
| `default_size` | `1024x1024` | 默认静态图片尺寸（`1024x1024` / `1024x1536` / `1536x1024` / `auto`） |
| `gif_frame_count` | `9` | GIF 动图默认帧数（建议 `4 ~ 16` 帧） |
| `gif_frame_duration_ms` | `180` | GIF 动图单帧停留毫秒数 |
| `default_view_count` | `5` | `view_recent_images` 默认查看图片数量 |
| `max_cache_per_session` | `20` | 每个群聊/私聊环形队列缓存近期图片上限 |
| `max_disk_cache_files` | `100` | 磁盘缓存文件总数上限（超出自动 GC 清理最旧未引用图片） |
| `api_key` | `""` *(密文掩码)* | 自定义 OpenAI API Key（留空自动复用 AstrBot 已配置的提供商 Key） |
| `api_base` | `""` | 自定义 API Base URL（留空自动复用 AstrBot 已配置的 API Base） |

---

## 💡 常见自然语言对话示例

- **合并转发拆图 / 改图**：引用一条合并转发聊天记录说：`把这个转发记录里的图全部单独提取发出来` 或 `把转发记录里的两张图融合改成赛博朋克风`
- **双人/多人头像融合**：`@群友A @群友B 把他俩的头像融合成一张街头霸王对战的 GIF 动图` 或 `把我的头像和 @群友A 的头像合成动漫同框照`
- **跨会话私聊供图、群内发图/改图**：先私聊发一张图给机器人，然后在群里说：`把刚才我私聊发你的那张图发到群里` 或 `用我刚私聊发你的图改成水彩画风`
- **外链抓取与融合**：`把这个链接 https://example.com/a.png 的图抓下来发到群里` 或 `把刚才第 1 张图的人物放到 https://example.com/bg.png 这个背景里`
- **打包合并转发**：`把群里最近 6 张图用合并转发打包发出来` 或 `把那几张图私聊发给我`

---

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 开源。欢迎提交 Issue 与 Pull Request！
