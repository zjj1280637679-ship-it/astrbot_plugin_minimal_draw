# Changelog

## [1.5.0] - 2026-11-13

### Changed & Refactored
- **全量对齐 OpenAI 官方 GPT-Image-2.6 / Responses API 标准工具调用规范 (`draw_image`)**：
  - **客观标准结构重塑**：遵循“只给客观条件描述与最小例子，不剥夺消极自由”原则，彻底对齐官方 Function Calling 字段与类型定义。
  - **新增 `action` 动作类型控制**：新增 `action` 参数（枚举值：`"auto"`, `"generate"`, `"edit"`），支持大模型显式声明文生图（`generate`，忽略历史上下文图片干扰）与参考图编辑抠图（`edit`）。
  - **释放动图与静态图全量长宽比控制 (`size`)**：彻底移除对 GIF 动图强制写死 `auto` 的内部限制，现在静态图与动图均完整支持 `1024x1024`（1:1 方图）、`1536x1024`（3:2/16:9 宽屏横图）、`1024x1536`（2:3/9:16 竖屏长图）及 `auto`；并新增接口像素预算超标时自动回退为 `auto` 的降级兜底。
  - **底层 JSON Schema 智能增强 (`_enhance_tool_schemas`)**：在生命周期初始化中，向 AstrBot 工具管理器注入标准 JSON Schema（包含 `required: ["prompt"]`、严格 `enum` 枚举限定、`minimum`/`maximum` 数值约束与 `default` 默认值），杜绝大模型在调用工具时因缺乏 Schema 选项而盲目猜想或参数残缺。
  - **规范化各工具契约注释**：同步规范 `view_recent_images`、`forward_images` 与 `draw_image` 的 docstring 与参数格式。

## [1.4.0] - 2026-11-12

### Added
- **Alpha 透明通道与幽灵图/表情包生成支持 (`background="transparent"`)**：
  - 新增 `background` 参数（支持 `"auto"`、`"transparent"`、`"opaque"`），允许 AI 或用户显式指定生成原生 Alpha 透明底图片（幽灵图、透明贴纸、无背景素材）。
  - 内置智能格式与通道适配（PNG/WebP 原生透明通道保护），并在接口不支持直接指定 `background` 时自动降级并以提示词引导透明底生成。
  - 在配置项 `_conf_schema.json` 中新增 `default_background`。
- **对话显式指定模型 ID 透传与权限路由 (`model`)**：
  - 新增 `model` 参数，允许 LLM 在用户指定模型时直接透传（如 `gpt-image-2.6-sunburst` 等），无需重复询问或硬编码。
  - 智能安全拦截与提示：普通用户尝试使用高阶旗舰模型时给出温和的权限说明并自动降级到标准模型。
- **宽容生图超时策略与 NTQQ 异步发送防抖**：
  - 接口超时全面放宽（标准模型 300s，高级模型 480s，并在配置项中支持自定义），杜绝“后台已生成但前台提示超时”问题。
  - 捕获并容错 NTQQ 大图上传异步确认超时（retcode 1200），避免重复报错。
  - 优化生图完成回包提示词，避免 LLM 在单轮对话中重复触发多次生成。

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
