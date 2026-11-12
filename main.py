from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import mcp.types
from openai import AsyncOpenAI
from PIL import Image as PILImage

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import StarTools
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.quoted_message.chain_parser import (
    OneBotPayloadParser,
    _extract_image_refs_from_component_chain,
)
from astrbot.core.utils.quoted_message.extractor import (
    _collect_text_and_images_from_forward_ids,
    extract_quoted_message_images,
)
from astrbot.core.utils.quoted_message.image_refs import looks_like_image_file_name
from astrbot.core.utils.quoted_message.image_resolver import ImageResolver
from astrbot.core.utils.quoted_message.onebot_client import OneBotClient
from astrbot.core.utils.quoted_message.settings import SETTINGS

PLUGIN_NAME = "astrbot_plugin_minimal_draw"

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s\)]+)\)", re.IGNORECASE)
_DATA_IMAGE_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)


@dataclass
class RecentImageItem:
    """表示会话中缓存的一张近期图片及其上下文信息"""

    image_id: str  # 如 IMG_1, IMG_2
    local_path: str  # 本地持久化缓存路径
    original_ref: str  # 原始 URL 或路径
    sender_id: str
    sender_name: str
    context_text: str  # 同条消息或前后相邻消息的配文
    created_at: float
    is_bot_generated: bool = False
    session_umo: str = ""  # 所属会话 unified_msg_origin，用于跨会话检索与转发


class MinimalVisionDrawPlugin(star.Star):
    """极简主动多图视野 (view_recent_images)、任意来源图片转发 (forward_images) 与分权双档位 OpenAI 生图/图生图/多图融合/GIF (draw_image) 插件"""

    def __init__(
        self,
        context: star.Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config if config is not None else {}
        self._log = getattr(self, "logger", None) or logger

        # 遵循 AstrBot 官方规范：使用 StarTools.get_data_dir 获取插件专属持久化目录
        try:
            self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self._data_dir = (
                Path(get_astrbot_temp_path()).parent / "plugin_data" / PLUGIN_NAME
            )
        self._cache_dir = self._data_dir / "cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._index_file = self._data_dir / "_index.json"

        # 复用 HTTP 客户端会话
        self._http_session: aiohttp.ClientSession | None = None

        # 每个会话 (unified_msg_origin) 的近期图片环形队列
        self._session_images: dict[str, deque[RecentImageItem]] = defaultdict(
            lambda: deque(maxlen=self._max_cache_size())
        )
        # 全局跨会话近期图片环形队列（支持私聊->群聊或跨群调取/转发近期图片）
        self._global_images: deque[RecentImageItem] = deque(maxlen=60)
        # 每个会话自增的图片序号计数
        self._session_counter: dict[str, int] = defaultdict(int)
        # 记录非管理员用户上次生图时间戳: sender_id -> timestamp
        self._user_last_draw_ts: dict[str, float] = {}

        # 兼容迁移旧版 temp/minimal_draw_cache 数据并加载近期图片索引
        self._migrate_legacy_temp_cache()
        self._load_cache_index()

    async def initialize(self) -> None:
        """插件激活/加载生命周期钩子"""
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=90, connect=15)
            self._http_session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (AstrBot MinimalDrawPlugin/1.2)"},
            )
        self._prune_disk_cache()
        self._log.info(
            f"[MinimalDraw] 插件已初始化，数据目录: {self._data_dir}，"
            f"已恢复 {len(self._session_images)} 个活跃会话的图片池（全局共 {len(self._global_images)} 张）。"
        )

    async def terminate(self) -> None:
        """插件卸载/禁用/重载生命周期钩子：保存索引并释放网络资源"""
        try:
            self._save_cache_index()
            self._prune_disk_cache()
        except Exception as e:
            self._log.debug(f"[MinimalDraw] 卸载保存索引异常: {e}")

        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        self._log.info("[MinimalDraw] 插件已优雅终止并释放所有资源。")

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=90, connect=15)
            self._http_session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (AstrBot MinimalDrawPlugin/1.2)"},
            )
        return self._http_session

    def _migrate_legacy_temp_cache(self) -> None:
        """将旧版位于 temp/minimal_draw_cache 的索引与缓存图片平滑迁移至规范的 plugin_data 目录"""
        if self._index_file.exists():
            return
        legacy_dir = Path(get_astrbot_temp_path()) / "minimal_draw_cache"
        legacy_index = legacy_dir / "_index.json"
        if not legacy_index.exists():
            return
        try:
            raw_data = json.loads(legacy_index.read_text(encoding="utf-8"))
            sessions = raw_data.get("sessions", {})
            for _umo, items in sessions.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    old_path_str = str(item.get("local_path", ""))
                    if old_path_str and os.path.exists(old_path_str):
                        old_path = Path(old_path_str)
                        new_path = self._cache_dir / old_path.name
                        if not new_path.exists():
                            shutil.copy2(old_path, new_path)
                        item["local_path"] = str(new_path)
            self._index_file.write_text(
                json.dumps(raw_data, ensure_ascii=False),
                encoding="utf-8",
            )
            self._log.info(
                f"[MinimalDraw] 已将旧版图片池索引平滑迁移至规范目录: {self._data_dir}"
            )
        except Exception as e:
            self._log.debug(f"[MinimalDraw] 迁移旧版缓存跳过: {e}")

    def _max_cache_size(self) -> int:
        try:
            return max(5, int(self.config.get("max_cache_per_session", 20)))
        except (TypeError, ValueError):
            return 20

    def _max_disk_files(self) -> int:
        try:
            return max(20, int(self.config.get("max_disk_cache_files", 100)))
        except (TypeError, ValueError):
            return 100

    def _default_view_count(self) -> int:
        try:
            return max(1, int(self.config.get("default_view_count", 5)))
        except (TypeError, ValueError):
            return 5

    def _gif_frame_count(self) -> int:
        try:
            return max(2, min(16, int(self.config.get("gif_frame_count", 9))))
        except (TypeError, ValueError):
            return 9

    def _gif_frame_duration_ms(self) -> int:
        try:
            return max(20, min(2000, int(self.config.get("gif_frame_duration_ms", 180))))
        except (TypeError, ValueError):
            return 180

    def _non_admin_cooldown(self) -> int:
        try:
            return max(0, int(self.config.get("non_admin_cooldown_seconds", 10)))
        except (TypeError, ValueError):
            return 10

    def _load_cache_index(self) -> None:
        """从本地索引文件恢复近期图片队列与全局跨会话队列"""
        if not self._index_file.exists():
            return
        try:
            data = json.loads(self._index_file.read_text(encoding="utf-8"))
            counters = data.get("counters", {})
            sessions = data.get("sessions", {})
            for umo, cnt in counters.items():
                self._session_counter[umo] = int(cnt)
            max_len = self._max_cache_size()
            all_loaded: list[RecentImageItem] = []
            for umo, items in sessions.items():
                q: deque[RecentImageItem] = deque(maxlen=max_len)
                for raw in items:
                    if isinstance(raw, dict) and os.path.exists(
                        str(raw.get("local_path", ""))
                    ):
                        raw.setdefault("session_umo", umo)
                        item_obj = RecentImageItem(**raw)
                        q.append(item_obj)
                        all_loaded.append(item_obj)
                if q:
                    self._session_images[umo] = q
            all_loaded.sort(key=lambda x: x.created_at)
            for item_obj in all_loaded[-60:]:
                self._global_images.append(item_obj)
        except Exception as e:
            self._log.debug(f"读取近期图片缓存索引失败: {e}")

    def _save_cache_index(self) -> None:
        """持久化近期图片队列索引"""
        try:
            payload = {
                "counters": dict(self._session_counter),
                "sessions": {
                    umo: [asdict(item) for item in q]
                    for umo, q in self._session_images.items()
                    if q
                },
            }
            self._index_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            self._log.debug(f"保存近期图片缓存索引失败: {e}")

    def _prune_disk_cache(self) -> None:
        """自动磁盘缓存垃圾回收：清理超出上限且不再被任何活跃会话引用的旧图片文件"""
        try:
            max_files = self._max_disk_files()
            all_files = [p for p in self._cache_dir.iterdir() if p.is_file()]
            if len(all_files) <= max_files:
                return

            active_paths = {
                str(Path(item.local_path).resolve())
                for q in self._session_images.values()
                for item in q
                if item.local_path and os.path.exists(item.local_path)
            }
            for item in self._global_images:
                if item.local_path and os.path.exists(item.local_path):
                    active_paths.add(str(Path(item.local_path).resolve()))

            all_files.sort(key=lambda p: p.stat().st_mtime)
            need_remove = len(all_files) - max_files
            removed = 0
            for fpath in all_files:
                if removed >= need_remove:
                    break
                if str(fpath.resolve()) not in active_paths:
                    fpath.unlink(missing_ok=True)
                    removed += 1
            if removed > 0:
                self._log.debug(
                    f"[MinimalDraw] 已自动清理 {removed} 个过期未引用的磁盘缓存图片文件。"
                )
        except Exception as e:
            self._log.debug(f"[MinimalDraw] 磁盘缓存清理异常: {e}")

    def _is_sender_admin(self, event: AstrMessageEvent) -> bool:
        """硬核权限校验：判断当前消息发送者是否为管理员"""
        if event.is_admin():
            return True

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return False

        raw_admin_qqs = str(self.config.get("admin_qq_list", "") or "")
        plugin_admins = {
            x.strip()
            for x in re.split(r"[,，;；\s]+", raw_admin_qqs)
            if x and x.strip()
        }
        if sender_id in plugin_admins:
            return True

        cfg = self.context.get_config(umo=event.unified_msg_origin) or {}
        global_admins = {str(x).strip() for x in (cfg.get("admins_id") or [])}
        return sender_id in global_admins

    def _resolve_model_and_quality(
        self,
        event: AstrMessageEvent,
        high_precision: bool,
        explicit_model: str = "",
    ) -> tuple[str, str, float, str | None]:
        """根据是否请求高精度、显式指定的模型ID及发送者管理员权限，决定最终使用的模型、画质档位与超时时间。"""
        cheap_model = (
            str(self.config.get("model", "gpt-image-2.6-flare") or "").strip()
            or "gpt-image-2.6-flare"
        )
        cheap_quality = (
            str(self.config.get("default_quality", "low") or "").strip().lower()
            or "low"
        )

        timeout_std = float(self.config.get("request_timeout_seconds", 300.0) or 300.0)
        timeout_pro = float(self.config.get("high_precision_timeout_seconds", 480.0) or 480.0)

        is_admin = self._is_sender_admin(event)

        # 1. 检查是否有对话显式指定的生图模型 ID
        target_model = str(explicit_model or "").strip()
        # 如果未在参数中传 model，但在用户上下文消息中检测到具体的模型名称特征
        if not target_model:
            msg_str = str(getattr(event, "message_str", "") or "").lower()
            if "sunburst" in msg_str:
                target_model = "gpt-image-2.6-sunburst"
            elif "flare" in msg_str:
                target_model = "gpt-image-2.6-flare"
            elif "dall-e-3" in msg_str or "dalle3" in msg_str:
                target_model = "dall-e-3"
            elif "dall-e-2" in msg_str or "dalle2" in msg_str:
                target_model = "dall-e-2"

        # 判断请求的模型是否属于高级旗舰模型
        is_high_model = (
            bool(high_precision)
            or any(k in target_model.lower() for k in ("sunburst", "pro", "dall-e-3", "plus", "ultra"))
        )

        # 2. 如果发送者是管理员：完全尊重管理员显式指定的模型，享有最高自由度
        if is_admin:
            if target_model:
                chosen_model = target_model
                chosen_quality = "high" if is_high_model else cheap_quality
                chosen_timeout = timeout_pro if is_high_model else timeout_std
                notice = f"⚡已验证管理员权限，根据对话指令直接指定模型: {chosen_model}"
                return chosen_model, chosen_quality, chosen_timeout, notice
            elif high_precision:
                pro_model = (
                    str(
                        self.config.get("high_precision_model", "gpt-image-2.6-sunburst")
                        or ""
                    ).strip()
                    or "gpt-image-2.6-sunburst"
                )
                pro_quality = (
                    str(self.config.get("high_precision_quality", "high") or "")
                    .strip()
                    .lower()
                    or "high"
                )
                return (
                    pro_model,
                    pro_quality,
                    timeout_pro,
                    f"⚡已验证管理员权限，启用旗舰高级模型 ({pro_model} + {pro_quality})",
                )
            else:
                return cheap_model, cheap_quality, timeout_std, None

        # 3. 如果发送者非管理员：
        if is_high_model:
            policy = (
                str(self.config.get("non_admin_high_policy", "downgrade") or "")
                .strip()
                .lower()
            )
            if policy == "reject":
                return "", "", timeout_std, "REJECT_NON_ADMIN"

            return (
                cheap_model,
                cheap_quality,
                timeout_std,
                "⚠️当前用户非管理员，高级模型请求已由系统权限锁自动降级为标准省流模型生成",
            )

        chosen_model = target_model if target_model else cheap_model
        return chosen_model, cheap_quality, timeout_std, None

    def _resolve_openai_credentials(self, umo: str | None = None) -> tuple[str, str]:
        """获取 OpenAI API Key 和 Base URL，优先读插件配置，留空则自动复用 AstrBot 主配置"""
        api_key = str(self.config.get("api_key", "") or "").strip()
        api_base = str(self.config.get("api_base", "") or "").strip()

        if api_key and api_base:
            return api_key, api_base

        cfg = self.context.get_config(umo=umo) or {}
        sources = cfg.get("provider_sources", []) or []
        for src in sources:
            if not isinstance(src, dict):
                continue
            src_type = str(src.get("type", "")).lower()
            src_prov = str(src.get("provider", "")).lower()
            if "openai" in src_type or "openai" in src_prov:
                keys = src.get("key", [])
                if not api_key and isinstance(keys, list) and keys:
                    api_key = str(keys[0]).strip()
                if not api_base and src.get("api_base"):
                    api_base = str(src.get("api_base")).strip()
                if api_key:
                    break

        if not api_base:
            api_base = "https://api.openai.com/v1"
        return api_key, api_base

    @staticmethod
    def _format_ago(ts: float) -> str:
        diff = max(0, int(time.time() - ts))
        if diff < 60:
            return f"{diff}秒前"
        if diff < 3600:
            return f"{diff // 60}分钟前"
        return f"{diff // 3600}小时前"

    @staticmethod
    def _guess_ext_from_bytes(data: bytes, fallback: str = ".png") -> str:
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return ".gif"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        return fallback

    async def _download_or_copy_to_cache(self, ref: str) -> str | None:
        """将任意图片引用（本地路径、file://、http(s)://、Markdown链接、data:image、base64://）落地为本地缓存文件"""
        if not ref:
            return None
        ref = ref.strip()

        # 支持 Markdown 图片语法 ![alt](url)
        md_m = _MD_IMAGE_RE.search(ref)
        if md_m:
            ref = md_m.group(1).strip()

        # 支持 data:image/...;base64,...
        if ref.lower().startswith("data:image/"):
            comma_idx = ref.find(",")
            if comma_idx > 0:
                ref = "base64://" + ref[comma_idx + 1 :].strip()

        if ref.startswith("file:///"):
            local_candidate = ref[8:] if os.name == "nt" else "/" + ref[8:]
            if os.path.exists(local_candidate):
                ref = local_candidate
        elif ref.startswith("file://"):
            local_candidate = ref[7:]
            if os.path.exists(local_candidate):
                ref = local_candidate

        if os.path.exists(ref) and os.path.isfile(ref):
            try:
                ext = os.path.splitext(ref)[1].lower() or ".png"
                if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                    ext = ".png"
                if Path(ref).parent.resolve() == self._cache_dir.resolve():
                    return ref
                dest = self._cache_dir / f"cached_{uuid.uuid4().hex[:10]}{ext}"
                shutil.copy2(ref, dest)
                return str(dest)
            except Exception as e:
                self._log.warning(f"复制本地图片到缓存失败 ({ref}): {e}")
                return ref

        if ref.startswith("base64://"):
            try:
                raw = base64.b64decode(ref[9:])
                ext = self._guess_ext_from_bytes(raw, ".png")
                dest = self._cache_dir / f"b64_{uuid.uuid4().hex[:10]}{ext}"
                dest.write_bytes(raw)
                return str(dest)
            except Exception as e:
                self._log.warning(f"解码 base64 图片失败: {e}")
                return None

        if ref.startswith(("http://", "https://")):
            try:
                session = await self._get_http_session()
                parsed_url = urlsplit(ref)
                req_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                }
                # 针对常见防盗链图床自动补充 Referer
                host_low = (parsed_url.netloc or "").lower()
                if "pximg.net" in host_low or "pixiv.net" in host_low:
                    req_headers["Referer"] = "https://www.pixiv.net/"
                elif "sinaimg.cn" in host_low or "weibo.com" in host_low:
                    req_headers["Referer"] = "https://weibo.com/"
                elif "hdslb.com" in host_low or "bilibili.com" in host_low:
                    req_headers["Referer"] = "https://www.bilibili.com/"

                async with session.get(
                    ref, headers=req_headers, allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        self._log.warning(
                            f"下载图片失败 HTTP {resp.status}: {ref[:80]}"
                        )
                        return None
                    data = await resp.read()
                    if not data:
                        return None
                    ext = self._guess_ext_from_bytes(data, ".png")
                    dest = self._cache_dir / f"dl_{uuid.uuid4().hex[:10]}{ext}"
                    dest.write_bytes(data)
                    return str(dest)
            except Exception as e:
                self._log.warning(f"下载远程图片异常 ({ref[:80]}): {e}")
                return None

        return None

    async def _extract_forward_images_from_event(
        self,
        event: AstrMessageEvent,
    ) -> list[str]:
        """提取当前消息中合并转发 (Comp.Forward / Comp.Nodes / Comp.Node / multimsg JSON) 内的全部图片并落地缓存"""
        messages = event.get_messages()
        if not messages:
            return []

        raw_refs: list[str] = []
        forward_ids: list[str] = []

        for comp in messages:
            if isinstance(comp, (Comp.Node, Comp.Nodes)):
                raw_refs.extend(_extract_image_refs_from_component_chain([comp]))
            elif isinstance(comp, Comp.Forward):
                fid = str(getattr(comp, "id", "") or "").strip()
                if fid:
                    forward_ids.append(fid)
            elif isinstance(comp, Comp.Json):
                data = getattr(comp, "data", None)
                if isinstance(data, dict) and data.get("app") == "com.tencent.multimsg":
                    detail = (data.get("meta") or {}).get("detail") or {}
                    resid = str(detail.get("resid") or "").strip()
                    if resid:
                        forward_ids.append(resid)

        if forward_ids:
            try:
                client = OneBotClient(event)
                parser = OneBotPayloadParser()
                _, fwd_img_refs = await _collect_text_and_images_from_forward_ids(
                    client,
                    parser,
                    forward_ids,
                    max_fetch=SETTINGS.max_forward_fetch,
                )
                if fwd_img_refs:
                    resolver = ImageResolver(event, client)
                    resolved = await resolver.resolve_for_llm(fwd_img_refs)
                    raw_refs.extend(resolved or fwd_img_refs)
            except Exception as e:
                self._log.debug(f"解析合并转发图片失败: {e}")

        cached_paths: list[str] = []
        for ref in raw_refs:
            cached = await self._download_or_copy_to_cache(ref)
            if cached and cached not in cached_paths:
                cached_paths.append(cached)
        return cached_paths

    def _register_image_in_session(
        self,
        umo: str,
        local_path: str,
        original_ref: str,
        sender_id: str,
        sender_name: str,
        context_text: str = "",
        is_bot_generated: bool = False,
    ) -> RecentImageItem:
        """将图片推入当前会话环形池及全局跨会话池，并分配 #IMG_n 编号"""
        q = self._session_images[umo]
        for existing in reversed(q):
            if existing.local_path == local_path:
                if context_text and not existing.context_text:
                    existing.context_text = context_text
                    self._save_cache_index()
                return existing

        self._session_counter[umo] += 1
        img_id = f"IMG_{self._session_counter[umo]}"
        item = RecentImageItem(
            image_id=img_id,
            local_path=local_path,
            original_ref=original_ref,
            sender_id=str(sender_id or "unknown"),
            sender_name=str(sender_name or sender_id or "群友"),
            context_text=context_text.strip(),
            created_at=time.time(),
            is_bot_generated=is_bot_generated,
            session_umo=umo,
        )
        q.append(item)
        self._global_images.append(item)
        self._save_cache_index()
        self._prune_disk_cache()
        return item

    @filter.event_message_type(filter.EventMessageType.ALL, priority=999)
    async def passive_image_sniffer(self, event: AstrMessageEvent) -> None:
        """全量静默旁路监听：0 Token 消耗记录近期图片（含普通图、原图文件、合并转发内图片、分享图）与附带配文"""
        try:
            messages = event.get_messages()
            if not messages:
                return

            text_parts: list[str] = []
            image_comps: list[Comp.Image] = []
            file_image_comps: list[Comp.File] = []
            share_urls: list[str] = []
            has_forward = False

            for comp in messages:
                if isinstance(comp, Comp.Plain) and comp.text.strip():
                    text_parts.append(comp.text.strip())
                elif isinstance(comp, Comp.Image):
                    image_comps.append(comp)
                elif isinstance(comp, Comp.File):
                    fname = str(getattr(comp, "name", "") or getattr(comp, "file_", "") or "")
                    if looks_like_image_file_name(fname):
                        file_image_comps.append(comp)
                elif isinstance(comp, (Comp.Share, Comp.Music)):
                    img_url = str(getattr(comp, "image", "") or "").strip()
                    if img_url.startswith(("http://", "https://")):
                        share_urls.append(img_url)
                elif isinstance(comp, (Comp.Forward, Comp.Node, Comp.Nodes, Comp.Json)):
                    has_forward = True

            # 若消息中没有任何图像相关内容，直接跳过，绝不干扰纯文本对话
            if not (image_comps or file_image_comps or share_urls or has_forward):
                return

            umo = event.unified_msg_origin
            sender_id = str(event.get_sender_id() or "")
            sender_name = str(event.get_sender_name() or sender_id or "用户")
            bound_text = " ".join(text_parts).strip()

            # 1. 普通图片组件
            for comp in image_comps:
                try:
                    raw_path = await comp.convert_to_file_path()
                    local_path = await self._download_or_copy_to_cache(raw_path)
                    if local_path:
                        orig_url = (
                            getattr(comp, "url", None)
                            or getattr(comp, "file", None)
                            or local_path
                        )
                        self._register_image_in_session(
                            umo=umo,
                            local_path=local_path,
                            original_ref=str(orig_url),
                            sender_id=sender_id,
                            sender_name=sender_name,
                            context_text=bound_text,
                            is_bot_generated=False,
                        )
                except Exception as e:
                    self._log.debug(f"旁路缓存图片组件失败: {e}")

            # 2. 原图文件附件 (Comp.File)
            for fcomp in file_image_comps:
                try:
                    fpath = await fcomp.get_file()
                    local_path = await self._download_or_copy_to_cache(fpath)
                    if local_path:
                        note = bound_text or f"[原图文件:{getattr(fcomp, 'name', '')}]"
                        self._register_image_in_session(
                            umo=umo,
                            local_path=local_path,
                            original_ref=fpath,
                            sender_id=sender_id,
                            sender_name=sender_name,
                            context_text=note,
                            is_bot_generated=False,
                        )
                except Exception as e:
                    self._log.debug(f"旁路缓存原图文件组件失败: {e}")

            # 3. 分享卡片封面图
            for s_url in share_urls:
                local_path = await self._download_or_copy_to_cache(s_url)
                if local_path:
                    self._register_image_in_session(
                        umo=umo,
                        local_path=local_path,
                        original_ref=s_url,
                        sender_id=sender_id,
                        sender_name=sender_name,
                        context_text=bound_text or "[分享卡片图片]",
                        is_bot_generated=False,
                    )

            # 4. 合并转发消息内部图片自动拆包入池
            if has_forward:
                fwd_paths = await self._extract_forward_images_from_event(event)
                for idx, local_path in enumerate(fwd_paths, 1):
                    note = (
                        f"[合并转发图{idx}] {bound_text}".strip()
                        if bound_text
                        else f"[合并转发内第{idx}张图]"
                    )
                    self._register_image_in_session(
                        umo=umo,
                        local_path=local_path,
                        original_ref=local_path,
                        sender_id=sender_id,
                        sender_name=sender_name,
                        context_text=note,
                        is_bot_generated=False,
                    )
        except Exception as e:
            self._log.debug(f"passive_image_sniffer error: {e}")

    @filter.command("img_pool", alias={"图片池", "画图状态"})
    async def cmd_img_pool(self, event: AstrMessageEvent):
        """查看当前会话近期缓存的图片编号列表及生图档位配置"""
        umo = event.unified_msg_origin
        q = self._session_images.get(umo)
        is_adm = self._is_sender_admin(event)
        cheap_model = self.config.get("model", "gpt-image-2.6-flare")
        cheap_q = self.config.get("default_quality", "low")
        pro_model = self.config.get("high_precision_model", "gpt-image-2.6-sunburst")
        pro_q = self.config.get("high_precision_quality", "high")

        lines = [
            "🎨 【极简视觉视野与生图状态】",
            f"• 当前身份: {'⚡管理员 (已解锁旗舰精绘档+跨会话转发)' if is_adm else '👤普通成员 (日常极速档)'}",
            f"• 默认极速档: {cheap_model} ({cheap_q})",
            f"• 旗舰精绘档: {pro_model} ({pro_q})",
        ]
        if not q:
            lines.append("• 当前会话图片池: 暂无缓存图片")
        else:
            valid_items = [it for it in q if os.path.exists(it.local_path)]
            lines.append(
                f"• 当前会话图片池: 共缓存 {len(valid_items)}/{self._max_cache_size()} 张近期图片"
            )
            for it in valid_items[-8:]:
                tag = "🤖Bot" if it.is_bot_generated else f"👤{it.sender_name}"
                note = f' "{it.context_text[:24]}"' if it.context_text else ""
                lines.append(
                    f"  - #{it.image_id} ({self._format_ago(it.created_at)}) {tag}{note}"
                )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("img_clear", alias={"清空图片池"})
    async def cmd_img_clear(self, event: AstrMessageEvent):
        """清空当前会话的近期图片缓存池（仅限管理员）"""
        umo = event.unified_msg_origin
        count = len(self._session_images.get(umo, []))
        self._session_images.pop(umo, None)
        self._save_cache_index()
        self._prune_disk_cache()
        yield event.plain_result(
            f"✅ 已清空当前会话的 {count} 条近期图片缓存记录。"
        )

    @staticmethod
    def _make_thumbnail_base64(local_path: str, max_edge: int = 768) -> str | None:
        """将本地图片压缩为轻量 JPEG base64 供视野工具快速多图查看"""
        try:
            with PILImage.open(local_path) as im:
                if im.mode in ("RGBA", "LA") or (
                    im.mode == "P" and "transparency" in im.info
                ):
                    bg = PILImage.new("RGB", im.size, (255, 255, 255))
                    rgba = im.convert("RGBA")
                    bg.paste(rgba, mask=rgba.split()[3])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=80)
                return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            logger.warning(f"生成视野缩略图失败 ({local_path}): {e}")
            return None

    @staticmethod
    def _convert_to_png_bytes(local_path: str, max_edge: int = 1536) -> bytes | None:
        """将任意本地图片转换为标准 PNG 字节流，确保 OpenAI images.edit 100% 兼容"""
        try:
            with PILImage.open(local_path) as im:
                # 若是 GIF 动图做参考图，提取第一帧并转 RGBA
                if getattr(im, "is_animated", False):
                    im.seek(0)
                im = im.convert("RGBA")
                im.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                return buf.getvalue()
        except Exception as e:
            logger.warning(f"转换参考图为 PNG 失败 ({local_path}): {e}")
            return None

    @staticmethod
    def _stitch_images_grid_png(
        local_paths: list[str],
        max_canvas_edge: int = 1536,
    ) -> bytes | None:
        """当后端接口不支持多文件数组上传时，自动将多张参考图智能拼接为单张高清网格 PNG 作为多图融合兜底"""
        loaded: list[PILImage.Image] = []
        for p in local_paths[:6]:
            try:
                with PILImage.open(p) as im:
                    if getattr(im, "is_animated", False):
                        im.seek(0)
                    loaded.append(im.convert("RGBA").copy())
            except Exception:
                continue
        if not loaded:
            return None
        if len(loaded) == 1:
            buf = io.BytesIO()
            loaded[0].save(buf, format="PNG")
            return buf.getvalue()

        cols = 2 if len(loaded) <= 4 else 3
        rows = math.ceil(len(loaded) / cols)
        cell_size = max_canvas_edge // max(cols, rows)
        canvas = PILImage.new(
            "RGBA",
            (cols * cell_size, rows * cell_size),
            (255, 255, 255, 255),
        )
        for idx, img in enumerate(loaded):
            r, c = divmod(idx, cols)
            img.thumbnail((cell_size - 12, cell_size - 12), PILImage.Resampling.LANCZOS)
            ox = c * cell_size + (cell_size - img.width) // 2
            oy = r * cell_size + (cell_size - img.height) // 2
            canvas.paste(img, (ox, oy), img)

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()

    @classmethod
    def _prepare_send_image_path(cls, original_path: Path, fmt: str) -> Path:
        """为 QQ 等平台消息发送准备轻量、高质量的图像文件。
        - 针对具有透明通道的图像（Alpha Channel / RGBA / 幽灵图 / 抠图贴纸），绝对不转为 JPEG（JPEG 会破坏透明度变黑/白底），
          而是保持 PNG 或生成优化压缩的透明 PNG，100% 保持原生透明通道与幽灵图显示效果。
        - 针对无透明通道的普通纯色静态大图(>800KB)，生成高质紧凑 JPEG 专发副本，大幅减少 Base64 传输与 NTQQ 上传等待。
        - 原始高清大图始终完整保存在本地缓存与图片池中。
        """
        try:
            if fmt == "gif" or not original_path.exists():
                return original_path

            # 若文件体积已较小 (<= 800KB)，无需二次优化，直接原样发送
            st_size = original_path.stat().st_size
            if st_size <= 800 * 1024:
                return original_path

            with PILImage.open(original_path) as im:
                # 检查是否存在真实透明通道 (Alpha Channel / 幽灵图 / 抠图贴纸)
                is_transparent = False
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    if im.mode == "P":
                        is_transparent = True
                    else:
                        alpha_channel = im.getchannel("A") if "A" in im.getbands() else None
                        if alpha_channel:
                            min_val, _ = alpha_channel.getextrema()
                            if min_val < 255:  # 存在非完全不透明的像素
                                is_transparent = True

                if is_transparent:
                    # 存在透明通道：绝不能转成 JPEG（JPEG 不支持透明度会变成黑底或白底）
                    # 生成针对透明大图的高效 PNG 优化副本
                    opt_png = original_path.with_name(f"{original_path.stem}_send.png")
                    if opt_png.exists() and opt_png.stat().st_size > 0:
                        return opt_png
                    # 保存为优化 PNG，保留完整透明通道
                    im.save(opt_png, format="PNG", optimize=True)
                    return opt_png

                # 纯实底静态大图：生成高质紧凑 JPEG
                opt_jpg = original_path.with_name(f"{original_path.stem}_send.jpg")
                if opt_jpg.exists() and opt_jpg.stat().st_size > 0:
                    return opt_jpg

                rgb_im = im.convert("RGB")
                rgb_im.save(opt_jpg, format="JPEG", quality=92, optimize=True)
                return opt_jpg
        except Exception as e:
            logger.debug(f"[MinimalDraw] 预处理发送图片异常，回退使用原图: {e}")
            return original_path

    @filter.llm_tool(name="view_recent_images")
    async def view_recent_images(
        self,
        event: AstrMessageEvent,
        count: int = 5,
    ) -> mcp.types.CallToolResult | str:
        """【工具是什么】：会话近期图片视野调取工具。
        【功能】：调取当前会话及关联会话近期缓存的历史图片数据与缩略图（含直发图片、原图文件、合并转发内图片）。
        【返回状态】：每张图片的真实画面缩略图、图片编号（#IMG_n）、发送者、相对时间、关联配文及本地文件路径。
        【最小例子】：
        - view_recent_images()：调取最近 5 张图片信息与缩略图
        - view_recent_images(count=10)：调取最近 10 张图片

        Args:
            count(number): 调取近期图片数量 N，默认 5（理论支持 1~20 范围自由调整）
        """
        umo = event.unified_msg_origin
        try:
            n = int(count) if count is not None else self._default_view_count()
        except (TypeError, ValueError):
            n = self._default_view_count()
        if n <= 0:
            n = self._default_view_count()
        n = min(n, self._max_cache_size())

        q = self._session_images.get(umo)
        recent_items = (
            [item for item in list(q)[-n:] if os.path.exists(item.local_path)]
            if q
            else []
        )

        # 若当前会话为空，检查该用户是否在其他会话（如私聊）刚刚发过图片
        cross_notice = ""
        if not recent_items:
            sender_id = str(event.get_sender_id() or "").strip()
            user_global = [
                it
                for it in self._global_images
                if it.sender_id == sender_id and os.path.exists(it.local_path)
            ]
            if user_global:
                recent_items = user_global[-n:]
                cross_notice = "（注：当前会话暂无图片，已自动调取该用户在其他会话中最近发送的图片）"
            else:
                return "当前会话暂无缓存的历史图片。"

        contents: list[mcp.types.TextContent | mcp.types.ImageContent] = [
            mcp.types.TextContent(
                type="text",
                text=f"当前会话最近 {len(recent_items)} 张图片记录（时间正序）{cross_notice}：",
            )
        ]

        for idx, item in enumerate(recent_items, 1):
            role_tag = (
                "🤖机器人生成"
                if item.is_bot_generated
                else f"👤{item.sender_name}({item.sender_id})"
            )
            ctx_note = f' | 配文/描述: "{item.context_text}"' if item.context_text else ""
            meta_line = (
                f"{idx}. 编号: [{item.image_id}] | 时间: {self._format_ago(item.created_at)} | "
                f"来源: {role_tag}{ctx_note} | 路径: {item.local_path}"
            )
            contents.append(mcp.types.TextContent(type="text", text=meta_line))
            b64_thumb = self._make_thumbnail_base64(item.local_path)
            if b64_thumb:
                contents.append(
                    mcp.types.ImageContent(
                        type="image",
                        data=b64_thumb,
                        mimeType="image/jpeg",
                    )
                )

        return mcp.types.CallToolResult(content=contents)

    async def _extract_current_and_quoted_images(
        self,
        event: AstrMessageEvent,
    ) -> list[str]:
        """自动提取当前消息（含直发图、原图文件、合并转发、文本内URL）以及引用/回复消息（含引用合并转发）中的全部图片"""
        resolved_paths: list[str] = []

        def _add(p: str | None) -> None:
            if p and os.path.exists(p) and p not in resolved_paths:
                resolved_paths.append(p)

        # 1. 当前消息中的直发图片与原图文件
        for comp in event.get_messages():
            if isinstance(comp, Comp.Image):
                try:
                    p = await comp.convert_to_file_path()
                    _add(await self._download_or_copy_to_cache(p))
                except Exception as e:
                    self._log.debug(f"解析当前消息图片失败: {e}")
            elif isinstance(comp, Comp.File):
                fname = str(getattr(comp, "name", "") or getattr(comp, "file_", "") or "")
                if looks_like_image_file_name(fname):
                    try:
                        fpath = await comp.get_file()
                        _add(await self._download_or_copy_to_cache(fpath))
                    except Exception as e:
                        self._log.debug(f"解析当前消息原图文件失败: {e}")
            elif isinstance(comp, (Comp.Share, Comp.Music)):
                img_url = str(getattr(comp, "image", "") or "").strip()
                if img_url.startswith(("http://", "https://")):
                    _add(await self._download_or_copy_to_cache(img_url))

        # 2. 当前消息中的合并转发 (Comp.Forward / Comp.Nodes)
        fwd_paths = await self._extract_forward_images_from_event(event)
        for fp in fwd_paths:
            _add(fp)

        # 3. 引用/回复消息中的图片（含引用的合并转发消息）
        if not resolved_paths:
            try:
                quoted_refs = await extract_quoted_message_images(event)
                for ref in quoted_refs:
                    _add(await self._download_or_copy_to_cache(ref))
            except Exception as e:
                self._log.debug(f"解析引用消息图片失败: {e}")

        # 4. 当前消息纯文本中夹带的图片 URL 或 data:image
        if not resolved_paths and event.message_str:
            for d_match in _DATA_IMAGE_RE.findall(event.message_str):
                _add(await self._download_or_copy_to_cache(d_match))
            for u_match in _URL_IN_TEXT_RE.findall(event.message_str):
                _add(await self._download_or_copy_to_cache(u_match))

        return resolved_paths

    async def _resolve_image_sources(
        self,
        event: AstrMessageEvent,
        image_source: str,
    ) -> list[str]:
        """全能图片源解析器：支持任意单图或多图组合（逗号/+号分隔），用于图生图、多图融合及图片转发。

        支持的来源语法：
        - "" / "auto" / "current": 自动提取当前消息(含合并转发/原图文件/URL)及引用消息中的所有图片
        - "forward" / "合并转发": 提取当前或引用合并转发消息中的所有图片
        - "quoted" / "reply" / "引用": 提取被引用消息中的所有图片
        - "IMG_1", "#IMG_2", "1-3", "IMG_1-IMG_4": 按会话图片池编号或编号区间提取
        - "latest" / "last" / "-1", "-2", "last:3": 最近第1张、倒数第N张、或最近N张
        - "bot_latest" / "user_latest": 当前会话中机器人最近生成的图 / 用户最近发的图
        - "my_latest" / "global_latest" / "group:群号" / "private:QQ号": 跨会话调取图片（如私聊发图到群里用）
        - "self_avatar" (自己头像), "bot_avatar" (机器人头像), "at_avatar" (消息内所有被@群友的头像), "avatar:QQ号" / "@QQ号", "group_avatar" / "group_avatar:群号" (群头像)
        - 任意 http(s):// 图片链接、Markdown图片、data:image、base64://、file:/// 或本地文件路径
        """
        umo = event.unified_msg_origin
        q = self._session_images.get(umo, deque())
        raw_source = str(image_source or "").strip()

        if raw_source.lower() in {"none", "t2i", "text_only", "false"}:
            return []

        resolved_paths: list[str] = []

        def _append_unique(path_str: str | None) -> None:
            if path_str and os.path.exists(path_str) and path_str not in resolved_paths:
                resolved_paths.append(path_str)

        if not raw_source or raw_source.lower() in {"auto", "current"}:
            extracted = await self._extract_current_and_quoted_images(event)
            for p in extracted:
                _append_unique(p)
            return resolved_paths

        # 支持逗号、中文逗号、分号、加号分隔多图来源（但不要切分 data:image/...;base64,...）
        if raw_source.lower().startswith("data:image/"):
            tokens = [raw_source]
        else:
            tokens = [
                t.strip()
                for t in re.split(r"[,，;；\+＋]+", raw_source)
                if t and t.strip()
            ]

        sender_id = str(event.get_sender_id() or "").strip()
        bot_id = str(event.get_self_id() or "").strip()

        for token in tokens:
            t_low = token.lower().lstrip("#").strip()

            # 1. 当前/自动/引用/合并转发提取
            if t_low in {"auto", "current", "当前", "本条"}:
                for p in await self._extract_current_and_quoted_images(event):
                    _append_unique(p)
                continue

            if t_low in {"forward", "fwd", "合并转发", "转发", "转发记录"}:
                fwd_list = await self._extract_forward_images_from_event(event)
                if not fwd_list:
                    try:
                        quoted_refs = await extract_quoted_message_images(event)
                        for ref in quoted_refs:
                            _append_unique(await self._download_or_copy_to_cache(ref))
                    except Exception:
                        pass
                else:
                    for p in fwd_list:
                        _append_unique(p)
                continue

            if t_low in {"quoted", "reply", "quote", "引用", "回复"}:
                try:
                    quoted_refs = await extract_quoted_message_images(event)
                    for ref in quoted_refs:
                        _append_unique(await self._download_or_copy_to_cache(ref))
                except Exception as e:
                    self._log.debug(f"提取引用消息图片失败: {e}")
                continue

            # 2. 最近 1 张 / 倒数第 N 张 / 最近 N 张 / 机器人最近 / 用户最近
            if t_low in {"latest", "last", "recent", "最近", "上一张", "-1"}:
                found = False
                for item in reversed(q):
                    if os.path.exists(item.local_path):
                        _append_unique(item.local_path)
                        found = True
                        break
                if not found:
                    # 自动跨会话回退查找当前用户最近发过的图
                    for item in reversed(self._global_images):
                        if item.sender_id == sender_id and os.path.exists(
                            item.local_path
                        ):
                            _append_unique(item.local_path)
                            break
                continue

            m_neg = re.match(r"^-(\d+)$", t_low)
            if m_neg:
                idx_back = int(m_neg.group(1))
                valid_q = [it for it in q if os.path.exists(it.local_path)]
                if 1 <= idx_back <= len(valid_q):
                    _append_unique(valid_q[-idx_back].local_path)
                continue

            m_last_n = re.match(r"^(?:last|recent|最近)[:：_-]?(\d+)$", t_low)
            if m_last_n:
                count_n = min(10, max(1, int(m_last_n.group(1))))
                valid_q = [it for it in q if os.path.exists(it.local_path)]
                for it in valid_q[-count_n:]:
                    _append_unique(it.local_path)
                continue

            if t_low in {"bot_latest", "bot", "机器人上一张", "刚生成的"}:
                for item in reversed(q):
                    if item.is_bot_generated and os.path.exists(item.local_path):
                        _append_unique(item.local_path)
                        break
                continue

            if t_low in {"user_latest", "user", "群友上一张"}:
                for item in reversed(q):
                    if not item.is_bot_generated and os.path.exists(item.local_path):
                        _append_unique(item.local_path)
                        break
                continue

            # 3. 跨会话图片调取（私聊发图到群里用、或指定群/私聊最近图片）
            if t_low in {"my_latest", "sender_latest", "我刚发的", "私聊图片"}:
                for item in reversed(self._global_images):
                    if item.sender_id == sender_id and os.path.exists(item.local_path):
                        _append_unique(item.local_path)
                        break
                continue

            if t_low in {"global_latest", "global", "全局最近"}:
                for item in reversed(self._global_images):
                    if os.path.exists(item.local_path):
                        _append_unique(item.local_path)
                        break
                continue

            m_cross = re.match(r"^(group|private|dm|群|私聊)[:：_-](\d{5,12})$", t_low)
            if m_cross:
                target_num = m_cross.group(2)
                for item in reversed(self._global_images):
                    if target_num in item.session_umo and os.path.exists(
                        item.local_path
                    ):
                        _append_unique(item.local_path)
                        break
                continue

            # 4. 区间编号 IMG_1-IMG_4 或 1-4
            m_range = re.match(
                r"^(?:img[_-]?)?(\d+)\s*[-~至到]\s*(?:img[_-]?)?(\d+)$", t_low
            )
            if m_range:
                start_i, end_i = int(m_range.group(1)), int(m_range.group(2))
                if start_i > end_i:
                    start_i, end_i = end_i, start_i
                target_ids = {f"IMG_{i}" for i in range(start_i, min(end_i + 1, start_i + 10))}
                for item in q:
                    if item.image_id.upper() in target_ids and os.path.exists(
                        item.local_path
                    ):
                        _append_unique(item.local_path)
                continue

            # 5. 单个图片池编号 IMG_1 / 1（区别于 5~12 位 QQ 号）
            m_id = re.match(r"^(?:img[_-]?)?(\d{1,4})$", t_low)
            if m_id:
                target_id = f"IMG_{m_id.group(1)}"
                found = False
                for item in reversed(q):
                    if item.image_id.upper() == target_id and os.path.exists(
                        item.local_path
                    ):
                        _append_unique(item.local_path)
                        found = True
                        break
                if not found:
                    for item in reversed(self._global_images):
                        if item.image_id.upper() == target_id and os.path.exists(
                            item.local_path
                        ):
                            _append_unique(item.local_path)
                            break
                continue

            # 6. 头像全系（自己头像、机器人头像、被@群友头像[支持多人]、指定QQ头像、群头像）
            if t_low in {"self_avatar", "my_avatar", "sender_avatar", "头像", "我的头像"}:
                if sender_id.isdigit():
                    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={sender_id}&s=640"
                    _append_unique(await self._download_or_copy_to_cache(avatar_url))
                continue

            if t_low in {"bot_avatar", "机器人头像", "你的头像"}:
                if bot_id.isdigit():
                    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={bot_id}&s=640"
                    _append_unique(await self._download_or_copy_to_cache(avatar_url))
                continue

            if t_low in {
                "at_avatar",
                "mention_avatar",
                "mention",
                "@头像",
                "群友头像",
                "at",
            }:
                for comp in event.get_messages():
                    if isinstance(comp, Comp.At):
                        at_qq = str(getattr(comp, "qq", "") or "").strip()
                        if at_qq.isdigit() and at_qq != bot_id:
                            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={at_qq}&s=640"
                            _append_unique(
                                await self._download_or_copy_to_cache(avatar_url)
                            )
                continue

            if t_low in {"group_avatar", "群头像", "本群头像"}:
                gid = str(event.get_group_id() or "").strip()
                if gid.isdigit():
                    g_url = f"https://p.qlogo.cn/gh/{gid}/{gid}/640/"
                    _append_unique(await self._download_or_copy_to_cache(g_url))
                continue

            m_gav = re.match(r"^(?:group_avatar|群头像)[:：_-](\d{5,12})$", t_low)
            if m_gav:
                gid = m_gav.group(1)
                g_url = f"https://p.qlogo.cn/gh/{gid}/{gid}/640/"
                _append_unique(await self._download_or_copy_to_cache(g_url))
                continue

            m_av = re.match(r"^(?:avatar:|qq:|@)?(\d{5,12})$", t_low)
            if m_av:
                qq_num = m_av.group(1)
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq_num}&s=640"
                _append_unique(await self._download_or_copy_to_cache(avatar_url))
                continue

            # 7. 外部图片 URL、Data URI、Base64、本地文件路径
            _append_unique(await self._download_or_copy_to_cache(token))

        # 若明确传了非空 source 但未能解析到，尝试从当前消息/引用消息及最近一张图兜底
        if not resolved_paths:
            for p in await self._extract_current_and_quoted_images(event):
                _append_unique(p)
        if not resolved_paths and q:
            for item in reversed(q):
                if os.path.exists(item.local_path):
                    _append_unique(item.local_path)
                    break

        return resolved_paths

    @filter.llm_tool(name="forward_images")
    async def forward_images(
        self,
        event: AstrMessageEvent,
        image_source: str = "latest",
        forward_mode: str = "auto",
        caption: str = "",
        target: str = "",
    ) -> str:
        """【工具是什么】：任意来源图片提取与转发工具。
        【功能】：提取单张或多张图片并发送至目标会话，支持逐张原图直发或打包为合并转发卡片（不消耗生图配额）。
        【返回状态】：已投递的目标会话、采用的发送形式（直接发图 / 合并转发卡片）、发送图片数量及对应的缓存编号列表。
        【最小例子】：
        - forward_images() 或 forward_images(image_source="latest")：重发最近一张图片到当前会话
        - forward_images(image_source="IMG_1,IMG_2", forward_mode="merged")：打包指定编号图片为合并转发卡片发送
        - forward_images(image_source="forward")：提取合并转发记录内的全部图片发送
        - forward_images(image_source="self_avatar", target="private")：提取发送者头像私聊发送

        Args:
            image_source(string): 图片来源（支持逗号或加号组合多图）。可填："IMG_1"、"IMG_1,IMG_2"、"1-4"、"latest"、"last:5"、"forward"（合并转发图）、"quoted"（引用图）、"my_latest"（用户跨会话图）、"self_avatar"（自己头像）、"at_avatar"（@群友头像）、"avatar:QQ号"、"group_avatar"（群头像）或任意图片 URL/本地路径
            forward_mode(string): 发送形式。默认 "auto"（1~3张直接发图，超过3张合并转发）；也可填 "direct"（原图直发）或 "merged"（合并转发卡片）
            caption(string): 随图附带的文本配文
            target(string): 投递目标。默认留空("")发到当前会话；可填 "private" 私聊发给发送者本人；管理员可填 "group:群号" 或 "private:QQ号" 跨会话转发
        """
        paths = await self._resolve_image_sources(event, image_source)
        if not paths:
            return f"未找到指定来源 ({image_source!r}) 的有效图片。"

        umo = event.unified_msg_origin
        is_adm = self._is_sender_admin(event)
        sender_id = str(event.get_sender_id() or "").strip()
        bot_id = str(event.get_self_id() or "10000").strip()

        # 同时将外部抓取或提取出的新图片登记入当前会话图片池，方便后续继续引用
        registered_ids: list[str] = []
        for p in paths:
            item = self._register_image_in_session(
                umo=umo,
                local_path=p,
                original_ref=p,
                sender_id=bot_id,
                sender_name="图片提取/转发",
                context_text=caption.strip() or f"转发自:{image_source[:30]}",
                is_bot_generated=False,
            )
            registered_ids.append(f"#{item.image_id}")

        # 解析跨会话投递目标
        raw_target = str(target or "").strip().lower()
        target_is_group = bool(event.get_group_id())
        target_session_id = (
            str(event.get_group_id()) if target_is_group else sender_id
        )
        cross_target_desc = "当前会话"

        if raw_target and raw_target not in {"current", "here", "当前"}:
            if raw_target in {"private", "dm", "self", "me", "私聊"}:
                target_is_group = False
                target_session_id = sender_id
                cross_target_desc = f"提问者私聊({sender_id})"
            else:
                m_tgt = re.match(
                    r"^(group|private|user|dm|群|私聊)[:：_-](\d{5,12})$", raw_target
                )
                if m_tgt:
                    kind, num = m_tgt.group(1), m_tgt.group(2)
                    want_group = kind in {"group", "群"}
                    # 若向其他群或其他人定向投递，必须验证管理员权限
                    if (want_group and num != str(event.get_group_id() or "")) or (
                        not want_group and num != sender_id
                    ):
                        if not is_adm:
                            return "权限不足：向其他群聊或其他用户跨会话转发图片仅限机器人管理员使用。"
                    target_is_group = want_group
                    target_session_id = num
                    cross_target_desc = (
                        f"群聊({num})" if want_group else f"私聊({num})"
                    )

        mode = str(forward_mode or "auto").strip().lower()
        use_merged = mode in {"merged", "node", "nodes", "forward", "合并", "合并转发"} or (
            mode == "auto" and len(paths) > 3
        )

        try:
            bot = getattr(event, "bot", None)
            same_session = (
                target_is_group == bool(event.get_group_id())
                and target_session_id
                == (str(event.get_group_id()) if target_is_group else sender_id)
            )

            if use_merged:
                nodes_list: list[Comp.Node] = []
                if caption.strip():
                    nodes_list.append(
                        Comp.Node(
                            uin=bot_id,
                            name="图片转发助手",
                            content=[Comp.Plain(caption.strip())],
                        )
                    )
                for idx, p in enumerate(paths, 1):
                    nodes_list.append(
                        Comp.Node(
                            uin=bot_id,
                            name="图片转发助手",
                            content=[
                                Comp.Plain(f"第 {idx}/{len(paths)} 张 ({registered_ids[idx - 1]})"),
                                Comp.Image.fromFileSystem(p),
                            ],
                        )
                    )
                chain = MessageChain(chain=[Comp.Nodes(nodes_list)])
            else:
                segs: list[ Any] = []
                if caption.strip():
                    segs.append(Comp.Plain(caption.strip() + "\n"))
                for p in paths:
                    segs.append(Comp.Image.fromFileSystem(p))
                chain = MessageChain(chain=segs)

            try:
                if same_session or bot is None:
                    await event.send(chain)
                else:
                    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                        AiocqhttpMessageEvent,
                    )

                    raw_ev = getattr(event.message_obj, "raw_message", None)
                    await AiocqhttpMessageEvent.send_message(
                        bot=bot,
                        message_chain=chain,
                        event=raw_ev,
                        is_group=target_is_group,
                        session_id=target_session_id,
                    )
            except Exception as send_err:
                err_text = str(send_err)
                if "1200" in err_text or "Timeout" in err_text or "NTEvent" in err_text:
                    self._log.warning(
                        f"[MinimalDraw] 转发图片触发 NTQQ 异步确认超时 (retcode=1200)，"
                        f"但内核后台已在投递并发送: {send_err}"
                    )
                else:
                    raise send_err

            mode_label = "合并转发卡片" if use_merged else "直接发图"
            return (
                f"已成功将 {len(paths)} 张图片通过【{mode_label}】发送至 {cross_target_desc}（编号: {', '.join(registered_ids)}）。"
            )
        except Exception as e:
            self._log.error(f"[MinimalDraw] 转发图片失败: {e}", exc_info=True)
            return f"图片转发失败: {e!s}"

    @staticmethod
    def _normalize_size(size_raw: str, default_size: str) -> str:
        s = str(size_raw or "").strip().lower()
        if not s:
            return default_size
        if s in {"1024x1024", "1024x1536", "1536x1024", "auto"}:
            return s
        if s in {"square", "1:1", "方图", "正方形"}:
            return "1024x1024"
        if s in {"portrait", "vertical", "2:3", "9:16", "竖图", "竖屏"}:
            return "1024x1536"
        if s in {"landscape", "horizontal", "3:2", "16:9", "横图", "横屏"}:
            return "1536x1024"
        return default_size

    @filter.llm_tool(name="draw_image")
    async def draw_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        model: str = "",
        image_source: str = "",
        output_format: str = "png",
        background: str = "auto",
        size: str = "",
        high_precision: bool = False,
        quality: str = "",
        complexity: str = "auto",
        frame_count: int = 0,
        fps: float = 0.0,
        frame_duration_ms: int = 0,
    ) -> str:
        """【工具是什么】：OpenAI 旗舰图像模型生图、智能抠图、多图融合、幽灵图与原生 GIF 动图生成工具。
        【功能】：文生静态图/动图、透明底幽灵图、免抠贴纸，或基于任意单张/多张参考图进行修改、风格转换、抠图去底、动图化与多图融合。生成的图片或动图直接发送至当前会话并存入图片池。
        【返回状态】：生成模式、输出格式（动图附带帧数/单帧时长/帧率/总时长）、背景模式（透明底/实底）、使用模型、画质档位及图片编号（#IMG_n）。
        【客观参数范围与换算】：
        - 模型选择 model：显式指定生图模型ID（如 "gpt-image-2.6-sunburst"、"gpt-image-2.6-flare"、"dall-e-3"）。管理员可在对话中根据需求自由指定任意模型；留空则根据 high_precision 自动选取。
        - 背景模式 background：
          * transparent：【透明背景 / 幽灵图 / 抠图贴纸 / 原生Alpha通道】。去除画面背景，输出带原生 Alpha 透明通道的 PNG/WebP（在 QQ 中呈现浮空幽灵图/透明贴纸表情包）。用户要求“透明底”、“透明背景”、“幽灵图”、“抠图”、“贴纸”、“表情包”、“无背景”、“透明通道”时必须使用此参数。
          * opaque：【实色背景】。强制生成带有完整环境或实色背景的图像。
          * auto：【自动模式，默认】。由模型根据画面描述自主决定是否有背景。
        - 帧数 frame_count：理论有效区间 2~16 帧（API 物理上限 16 帧，默认 9 帧）。
        - 单帧时长 frame_duration_ms：理论有效区间 20~2000 ms（对应 0.5~50 fps，默认 180ms）。
        - 物理换算公式：frame_duration_ms = round(1000 / fps)，动图总时长(秒) = frame_count * (frame_duration_ms / 1000) = frame_count / fps。
        【最小例子】：
        - draw_image(prompt="可爱的幽灵猫咪表情包", background="transparent")：纯透明底幽灵图/贴纸 (原生透明通道)
        - draw_image(prompt="把这张图的主体抠出来做成透明底贴纸", image_source="latest", background="transparent")：图生图智能抠图去底
        - draw_image(prompt="水彩风风景画")：基础文生图
        - draw_image(prompt="用sunburst画一张未来赛博朋克城市", model="gpt-image-2.6-sunburst")：指定模型ID生图
        - draw_image(prompt="融合两图特征", image_source="IMG_1,IMG_2")：多图融合
        - draw_image(prompt="日落分镜叙事", output_format="gif", frame_count=16, fps=1)：慢速叙事动图（16帧/1fps/16秒故事流）
        - draw_image(prompt="日常动作表情包", output_format="gif", background="transparent", frame_count=8, fps=8)：透明底动态表情包

        Args:
            prompt(string): 画面描述或修改/融合/抠图指令（如 "一只穿宇航服的橘猫"、"可爱的幽灵柴犬透明贴纸" 或 "把图1与图2融合"）
            model(string): 显式指定使用的生图模型 ID（如 "gpt-image-2.6-sunburst"、"gpt-image-2.6-flare"、"dall-e-3"）。管理员可在对话中自由指定任意模型；留空则根据 high_precision 自动匹配
            image_source(string): 参考图来源（支持逗号或加号组合多图融合）。留空("")自动提取当前或引用消息中的全部图片；可填 "IMG_1"、"1-3"、"latest"、"forward"（合并转发图）、"my_latest"、"self_avatar"、"at_avatar"、"avatar:QQ号"、"group_avatar" 或网络 URL/本地路径；强制纯文生图填 "none"
            output_format(string): 输出格式。默认 "png"（静态图推荐，完整保留透明通道）；也支持 "webp"（轻量透明图）、"jpeg"（仅限纯实底图）、"gif"（动图）、"svg"（矢量图）。注意：当 background="transparent" 时，格式必须为 png 或 webp，严禁使用 jpeg
            background(string): 画面背景模式与透明通道控制。可选 'transparent' (透明底/幽灵图/抠图贴纸/原生Alpha通道)、'opaque' (实色背景)、'auto' (自动判断，默认)。当用户提及透明底、透明图、幽灵图、抠图、贴纸、表情包、无背景或透明通道时必须设为 'transparent'
            size(string): 画面尺寸或比例。留空自动匹配（静态图默认 1024x1024，GIF 动图自动使用 auto），静态图可填 "1024x1024"、"1024x1536"、"1536x1024"、"1:1"、"16:9"、"9:16"、"3:2"、"4:3"
            high_precision(boolean): 是否请求高级/高精度精绘模型。默认 false，true 则请求高级旗舰模型 (如 gpt-image-2.6-sunburst)
            quality(string): 画质档位。可选 'low', 'medium', 'high', 'xhigh', 'max', 'auto'。留空根据 high_precision 自动决定
            complexity(string): 画面思考与规划复杂度。可选 'low' (快速迭代/抠图去底/轻量修改)、'medium'、'high' (复杂构图/高难文字排版)、'auto' (默认)
            frame_count(number): GIF 总帧数。有效范围 2~16 帧（API 上限 16 帧）。留空或填 0 时使用默认值（9 帧）
            fps(number): GIF 播放帧率（每秒播放帧数，范围 0.5~50 fps）。自动换算单帧时长 round(1000 / fps)。若与 frame_duration_ms 同时提供，优先使用 frame_duration_ms。留空或填 0 时使用默认帧率（约 5.5 fps）
            frame_duration_ms(number): GIF 单帧停留时长（毫秒，有效范围 20~2000 ms）。与 fps 二选一。留空或填 0 时使用默认值（180ms）
        """
        umo = event.unified_msg_origin
        is_adm = self._is_sender_admin(event)
        sender_id = str(event.get_sender_id() or "").strip()

        # 非管理员冷却保护（防刷图）
        if not is_adm and sender_id:
            cd = self._non_admin_cooldown()
            if cd > 0:
                now = time.time()
                last_ts = self._user_last_draw_ts.get(sender_id, 0.0)
                remain = int(cd - (now - last_ts))
                if remain > 0:
                    return f"生图冷却中：普通成员每 {cd} 秒可生图一次，请等待 {remain} 秒后再试。"
                self._user_last_draw_ts[sender_id] = now

        api_key, api_base = self._resolve_openai_credentials(umo)
        if not api_key:
            return "生图失败：未找到可用的 OpenAI API Key，请在插件设置或 AstrBot 模型提供商中配置。"

        explicit_model_clean = str(model or "").strip()
        want_high = (
            bool(high_precision)
            or (str(quality).strip().lower() in {"high", "xhigh", "max"})
            or any(k in explicit_model_clean.lower() for k in ("sunburst", "pro", "dall-e-3", "plus", "ultra"))
        )
        model_name, final_quality, req_timeout, perm_notice = (
            self._resolve_model_and_quality(event, want_high, explicit_model=explicit_model_clean)
        )
        if perm_notice == "REJECT_NON_ADMIN":
            return "权限不足：高级/高精度旗舰生图模型仅限机器人管理员调用，普通生图请直接描述画面。"

        if quality and quality.strip().lower() in {"low", "medium", "high", "xhigh", "max", "auto"}:
            q_clean = quality.strip().lower()
            if q_clean in {"high", "xhigh", "max"}:
                if self._is_sender_admin(event):
                    final_quality = q_clean
            else:
                final_quality = q_clean

        default_size = (
            str(self.config.get("default_size", "1024x1024") or "").strip()
            or "1024x1024"
        )
        final_prompt = (
            str(prompt or "").strip()
            or event.message_str.strip()
            or "high quality creative illustration"
        )

        # 解析背景与透明度模式 (transparent / opaque / auto)
        bg_param = str(background or "auto").strip().lower()
        if bg_param in {"transparent", "opaque", "auto"}:
            bg = bg_param
        elif any(k in bg_param for k in ("透", "transparent", "alpha", "幽灵", "抠图", "无底", "无背景")):
            bg = "transparent"
        elif any(k in bg_param for k in ("实", "opaque", "实色", "白底", "黑底")):
            bg = "opaque"
        else:
            bg = "auto"

        default_bg = str(self.config.get("default_background", "auto") or "auto").strip().lower()
        if bg == "auto" and default_bg in {"transparent", "opaque"}:
            bg = default_bg

        # 智能语义双重保护：若提示词包含透明/幽灵图意图且未强制指定 opaque，自动启用 transparent
        trans_keywords = (
            "透明底", "透明背景", "透明图", "幽灵图", "透明通道", "透明png",
            "透明", "抠图", "贴纸", "表情包", "无底", "无背景",
            "transparent", "alpha channel", "alpha", "cutout", "isolated"
        )
        if bg == "auto" and any(k in final_prompt.lower() for k in trans_keywords):
            bg = "transparent"

        fmt = str(output_format or "png").strip().lower().lstrip(".")
        if fmt not in {"png", "jpeg", "webp", "gif", "svg"}:
            fmt = "gif" if "gif" in fmt or "动图" in fmt else "png"

        # 若显式传入了动图参数，自动确定为 GIF
        if (frame_count and frame_count > 0) or (fps and fps > 0) or (frame_duration_ms and frame_duration_ms > 0):
            fmt = "gif"

        # 若指定了透明背景，输出格式强制为支持 Alpha 通道的 png 或 webp (严禁转为 jpeg)
        if bg == "transparent" and fmt in {"jpeg", "jpg"}:
            fmt = "png"

        if fmt == "gif":
            final_size = "auto"
        else:
            final_size = self._normalize_size(size, default_size)

        ref_paths = await self._resolve_image_sources(event, image_source)
        client = AsyncOpenAI(api_key=api_key, base_url=api_base, timeout=req_timeout)

        extra_kwargs: dict[str, Any] = {"output_format": fmt}
        if bg in {"transparent", "opaque", "auto"}:
            extra_kwargs["background"] = bg

        if complexity and complexity.lower() in {"low", "medium", "high"}:
            extra_kwargs["complexity"] = complexity.lower()

        final_fc: int = 0
        final_dur: int = 0
        if fmt == "gif":
            # 1. 确定 frame_count (限制在 API 物理允许的 2 ~ 16 帧)
            if frame_count and frame_count > 0:
                final_fc = max(2, min(16, int(round(frame_count))))
            else:
                final_fc = self._gif_frame_count()

            # 2. 确定 frame_duration_ms (限制在 API 物理允许的 20 ~ 2000 ms)
            if frame_duration_ms and frame_duration_ms > 0:
                final_dur = max(20, min(2000, int(round(frame_duration_ms))))
            elif fps and fps > 0:
                calc_ms = round(1000.0 / float(fps))
                final_dur = max(20, min(2000, int(calc_ms)))
            else:
                final_dur = self._gif_frame_duration_ms()

            extra_kwargs["frame_count"] = final_fc
            extra_kwargs["frame_duration_ms"] = final_dur

        async def _call_api_with_fallback(call_fn, **kwargs):
            try:
                return await call_fn(**kwargs)
            except Exception as err:
                err_msg = str(err).lower()
                removed = False
                for p in ("complexity", "background"):
                    if p in kwargs and (p in err_msg or "unexpected keyword" in err_msg or "unrecognized" in err_msg or "extra" in err_msg or "400" in err_msg):
                        kwargs.pop(p, None)
                        removed = True
                if removed:
                    self._log.warning(f"[MinimalDraw] 上游生图接口不支持扩展参数 ({err})，自动降级核心参数重试")
                    return await call_fn(**kwargs)
                raise err

        try:
            if ref_paths:
                upload_files: list[tuple[str, bytes, str]] = []
                max_ref_edge = 2048 if final_quality == "high" else 1536
                for idx, p in enumerate(ref_paths[:6]):
                    png_bytes = self._convert_to_png_bytes(p, max_edge=max_ref_edge)
                    if png_bytes:
                        upload_files.append((f"ref_{idx}.png", png_bytes, "image/png"))

                if upload_files:
                    mode_desc = (
                        f"多图融合图生图(融合了 {len(upload_files)} 张参考图)"
                        if len(upload_files) > 1
                        else "图生图"
                    )
                    if fmt == "gif":
                        calc_fps = round(1000.0 / final_dur, 1)
                        total_sec = round(final_fc * final_dur / 1000.0, 1)
                        mode_desc += f"+GIF动图化({final_fc}帧/{final_dur}ms/约{calc_fps}fps/总长{total_sec}s)"
                    self._log.info(
                        f"[MinimalDraw] 正在调用 {model_name} 执行{mode_desc}: prompt={final_prompt[:60]!r}, format={fmt}, size={final_size}, quality={final_quality}"
                    )
                    try:
                        resp = await _call_api_with_fallback(
                            client.images.edit,
                            model=model_name,
                            image=upload_files
                            if len(upload_files) > 1
                            else upload_files[0],
                            prompt=final_prompt,
                            size=final_size,  # type: ignore[arg-type]
                            quality=final_quality,  # type: ignore[arg-type]
                            **extra_kwargs,
                        )
                    except Exception as edit_err:
                        # 若上游兼容接口不支持多文件数组上传，自动使用网格画布拼接兜底重试
                        if len(upload_files) > 1:
                            self._log.info(
                                f"[MinimalDraw] 多图数组上传触发网格拼接兜底 ({edit_err!s})"
                            )
                            stitched_png = self._stitch_images_grid_png(
                                ref_paths[:6], max_canvas_edge=max_ref_edge
                            )
                            if stitched_png:
                                resp = await _call_api_with_fallback(
                                    client.images.edit,
                                    model=model_name,
                                    image=("stitched_grid.png", stitched_png, "image/png"),
                                    prompt=final_prompt,
                                    size=final_size,  # type: ignore[arg-type]
                                    quality=final_quality,  # type: ignore[arg-type]
                                    **extra_kwargs,
                                )
                            else:
                                raise edit_err
                        else:
                            raise edit_err
                else:
                    mode_desc = "文生图(参考图读取失败自动降级)"
                    resp = await _call_api_with_fallback(
                        client.images.generate,
                        model=model_name,
                        prompt=final_prompt,
                        size=final_size,  # type: ignore[arg-type]
                        quality=final_quality,  # type: ignore[arg-type]
                        **extra_kwargs,
                    )
            else:
                if fmt == "gif":
                    calc_fps = round(1000.0 / final_dur, 1)
                    total_sec = round(final_fc * final_dur / 1000.0, 1)
                    mode_desc = f"文生GIF动图({final_fc}帧/{final_dur}ms/约{calc_fps}fps/总长{total_sec}s)"
                else:
                    mode_desc = "文生图"
                self._log.info(
                    f"[MinimalDraw] 正在调用 {model_name} 执行{mode_desc}: prompt={final_prompt[:60]!r}, format={fmt}, size={final_size}, quality={final_quality}"
                )
                resp = await _call_api_with_fallback(
                    client.images.generate,
                    model=model_name,
                    prompt=final_prompt,
                    size=final_size,  # type: ignore[arg-type]
                    quality=final_quality,  # type: ignore[arg-type]
                    **extra_kwargs,
                )

            if not resp.data:
                return "生图接口未返回任何图像数据。"

            first_img = resp.data[0]
            if fmt == "gif":
                file_ext = "gif"
            elif fmt == "webp":
                file_ext = "webp"
            elif fmt in ("jpeg", "jpg"):
                file_ext = "jpg"
            elif fmt == "svg":
                file_ext = "svg"
            else:
                file_ext = "png"
            out_path = self._cache_dir / f"gen_{uuid.uuid4().hex[:10]}.{file_ext}"

            if getattr(first_img, "b64_json", None):
                out_path.write_bytes(base64.b64decode(first_img.b64_json))
            elif getattr(first_img, "url", None):
                dl = await self._download_or_copy_to_cache(first_img.url)
                if not dl:
                    return "生图成功但下载结果图片失败。"
                out_path = Path(dl)
            else:
                return "生图接口返回的数据中既无 b64_json 也无 url。"

            cached_item = self._register_image_in_session(
                umo=umo,
                local_path=str(out_path),
                original_ref=str(out_path),
                sender_id=str(event.get_self_id() or "bot"),
                sender_name="机器人生成",
                context_text=final_prompt[:100],
                is_bot_generated=True,
            )

            send_path = self._prepare_send_image_path(out_path, fmt)

            try:
                await event.send(
                    MessageChain(chain=[Comp.Image.fromFileSystem(str(send_path))])
                )
            except Exception as send_err:
                err_text = str(send_err)
                # 若因 NTQQ 上传大文件或网络波动导致 NapCat 返回 1200 NTEvent sendMsg 超时，
                # 但实际上 NTQQ 内核已经在后台排队上传并发送了，此时不应作为生图失败打断并道歉
                if "1200" in err_text or "Timeout" in err_text or "NTEvent" in err_text:
                    self._log.warning(
                        f"[MinimalDraw] QQ 发送大图触发 NTQQ 异步确认超时 (retcode=1200)，"
                        f"但内核后台已在排队上传并发送: {send_err}"
                    )
                else:
                    raise send_err

            gif_detail = (
                f"，{final_fc}帧/{final_dur}ms/约{round(1000.0 / final_dur, 1)}fps/总长{round(final_fc * final_dur / 1000.0, 1)}s"
                if fmt == "gif"
                else ""
            )
            bg_desc = ""
            if bg == "transparent":
                bg_desc = "，背景: TRANSPARENT(透明底/原生Alpha通道/幽灵图)"
            elif bg == "opaque":
                bg_desc = "，背景: OPAQUE(实色)"

            notice_str = f" | {perm_notice}" if perm_notice else ""
            return (
                f"图片已生成并发送（模式: {mode_desc}，格式: {fmt.upper()}{gif_detail}{bg_desc}，模型: {model_name}，画质: {final_quality}"
                f"{notice_str}，编号: #{cached_item.image_id}）。【已发送完成】图片已成功投递至群聊，任务已全部完成，请勿在同一轮回复中再次调用 draw_image。"
            )
        except Exception as e:
            self._log.error(f"[MinimalDraw] 生图失败: {e}", exc_info=True)
            return f"生图调用失败: {e!s}"
        finally:
            await client.close()
