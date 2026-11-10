# Changelog

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
