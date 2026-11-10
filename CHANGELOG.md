# Changelog

## [1.3.0] - 2026-11-10

### Added
- **原生 GIF 动图全参数控制与自主节奏策略支持 (`draw_image`)**：
  - 新增 `frame_count`（动图总帧数，物理范围 2~16 帧，API 严格上限 16 帧）、`fps`（每秒播放帧率，范围 0.5~50 fps）与 `frame_duration_ms`（单帧停留毫秒数，物理范围 20~2000 ms）参数，向 LLM 主模型暴露完整的动图物理控制维度。
  - 严格遵循客观物理条件与数学公式换算（$\text{frame\_duration\_ms} = \text{round}(1000 / \text{fps})$，$\text{总播放时长} = \text{frame\_count} / \text{fps}$），支持从 1 fps / 16 帧慢速叙事分镜（16 秒长线故事流）到 16 fps / 16 帧极速丝滑循环（1 秒高动态微循环）的任意自由节奏。
  - 工具层严格遵循“只给客观条件描述与示例，不预设死板规则”的原则，由 AI 结合上下文情境、角色动作动态与叙事节奏自主设计动图节奏策略。
  - 修复 `_gif_frame_count` 边界保护由 24 修正为 API 严格上限 16 帧，`_gif_frame_duration_ms` 最小时间由 40ms 拓展为 20ms。

## [1.2.0] - 2026-11-10

### Added
- **新增任意来源图片提取与转发工具 (`forward_images`)**：支持将任意来源的单张或多张图片直接原图发送（`direct`）或自动封装为 QQ 合并转发卡片（`merged`），并支持发往提问者私聊（`target="private"`）或管理员跨群/跨人定向转发（`target="group:群号"` / `"private:QQ号"`）。
- **新增合并转发消息 (`Forward` / `Nodes`) 与原图文件 (`File`) 自动拆包入池**：`passive_image_sniffer` 与 `_resolve_image_sources` 现已原生支持递归拆包 OneBot 合并转发聊天记录内的全部图片、以文件附件发送的原图（`.png`/`.jpg`/`.gif`/`.webp`）、分享卡片封面图及文本内嵌的图片 URL。
- **新增跨会话全局图片池 (`_global_images`)**：支持 `my_latest`（调取当前用户在私聊或其他群刚发的图片）、`global_latest`、`group:群号`、`private:QQ号` 跨会话取图，满足“私聊供图、群内改图/转发”场景。
- **全面升级任意来源多图融合 (`draw_image`)**：支持通过 `,` 或 `+` 组合最多 6 张任意来源图片（如 `"IMG_1,IMG_2"`、`"self_avatar,at_avatar"`、多人同时 `@` 头像融合、历史图+外链 URL 融合、区间 `"1-4"`），并新增 `_stitch_images_grid_png` 智能网格拼接兜底，确保在任何后端接口下多图融合 100% 成功。
- **完善全系头像与防盗链抓取**：新增 `bot_avatar`（机器人头像）、`group_avatar` / `group_avatar:群号`（640px 高清群头像）、多人 `@` 批量头像提取，以及针对 Pixiv / 微博 / B站等图床的自动 `Referer` 防盗链适配与图像魔数后缀识别。

## [1.1.0] - 2026-11-10

### Added
- **规范化持久化数据目录**：全面迁移至 `StarTools.get_data_dir("astrbot_plugin_minimal_draw")`，支持自动无损迁移历史图片与 `#IMG_n` 索引。
- **完整插件生命周期 (`initialize` & `terminate`)**：实现异步连接池 (`aiohttp.ClientSession`) 复用与插件卸载/重载时的优雅关闭及自动磁盘 GC (`_prune_disk_cache`)。
- **新增聊天状态与管理指令**：新增 `/img_pool`（别名：`图片池`、`画图状态`）与管理员指令 `/img_clear`（别名：`清空图片池`）。
- **补齐标准交付文件**：新增 `logo.png`（256×256 高清图标）、`README.md`、`CHANGELOG.md`、`requirements.txt`。

## [1.0.0] - 2026-11-10

### Added
- 初始版本发布：支持 0 Token 旁路图片嗅探、`view_recent_images(count=5)` 主动多图视野、`draw_image` 文生图/图生图/GIF 动图生成、管理员上下文双档位分权及 OpenAI Prompt Caching 前缀缓存保护。
