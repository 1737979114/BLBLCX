# -*- coding: utf-8 -*-
"""astrbot_plugin_bilibili —— 在聊天里查哔哩哔哩视频。

用法::

    b站 罗翔 刑法          # 按关键词搜视频，出图
    b站 BV1GJ411x7h7      # 直接查这条视频的详情
    b站 热门              # 全站热门榜
    b站热门               # 同上，快捷写法

另外，群里有人直接发 B 站链接或 BV 号时，插件会自动解析成详情卡片
（可通过配置关掉）。

只调用哔哩哔哩对外公开的 Web 接口：搜索、视频详情、排行榜。
不登录、不下载视频、不绕过任何权限，拿到的都是网页上就能看到的信息。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools

try:  # GreedyStr 用来接收带空格的关键词
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover

    class GreedyStr(str):  # type: ignore
        """旧版本兜底，行为退化成普通 str。"""


_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from bili_api import (  # noqa: E402
    BV_RE,
    BiliClient,
    BiliError,
    Video,
    format_count,
    format_date,
    format_duration,
)
from bili_render import render_detail, render_hot, render_search  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_bilibili"

#: 清理消息开头的 @昵称
AT_PREFIX_RE = re.compile(r"^@\S+\s*")

#: 带唤醒前缀的写法（/b站 xxx、！b站 xxx），
#: 因为服务器 wake_prefix 为空，@filter.command 只认不带前缀的
PREFIXED_CMD_RE = re.compile(
    r"^[/!！.。]\s*(?:哔哩哔哩|bilibili|bili|b站)", re.I
)

#: 链接里可能是 av 号
AV_IN_URL_RE = re.compile(r"(?:/video/)?av(\d{3,})", re.I)
AV_ONLY_RE = re.compile(r"^av(\d{3,})$", re.I)

#: 已经由 @filter.command 接管的写法，全消息入口要放行，
#: 否则 b站 BV号 会同时被命令和自动解析处理，发两张卡片
COMMAND_PREFIX_RE = re.compile(
    r"^(?:b站|哔哩哔哩|bili(?:bili)?)(?:热门)?(?:\s|$)", re.I
)

#: 热门榜的几种说法
HOT_WORDS = frozenset(
    {"热门", "热榜", "热门榜", "排行榜", "热门视频", "热门排行", "b站热门", "B站热门"}
)

#: 候选会话上限
SESSION_MAX = 300


class BilibiliPlugin(Star):
    """哔哩哔哩视频查询插件。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}

        self.enable = self._cfg_bool("enable", True)
        self.search_limit = self._cfg_int("search_limit", 8, 1, 10)
        self.send_mode = str(self._cfg("send_mode", "card") or "card").strip().lower()
        if self.send_mode not in ("card", "text"):
            self.send_mode = "card"
        self.search_order = str(self._cfg("search_order", "totalrank") or "").strip()
        if self.search_order not in ("totalrank", "click", "pubdate", "dm", "stow"):
            self.search_order = "totalrank"
        self.enable_parse = self._cfg_bool("enable_parse_link", True)
        self.enable_llm_tool = self._cfg_bool("enable_llm_tool", True)
        self.enable_choice = self._cfg_bool("enable_choice", True)
        self.choice_timeout = self._cfg_int("choice_timeout", 120, 20, 600)
        self.rate_limit_seconds = self._cfg_int("rate_limit_seconds", 3, 0, 120)

        timeout = self._cfg_int("request_timeout", 10, 3, 60)
        self.client = BiliClient(timeout=float(timeout))

        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # pragma: no cover
            self.data_dir = _PLUGIN_DIR / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._last_request: dict[str, float] = {}
        self._sessions: dict[str, tuple[list[Video], float]] = {}

        logger.info(
            "[bilibili] 已加载 | 启用=%s | 条数=%s | 发送=%s | 自动解析链接=%s",
            self.enable,
            self.search_limit,
            self.send_mode,
            self.enable_parse,
        )

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "开", "是")
        return bool(value)

    def _cfg_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    # ------------------------------------------------------------------ 指令

    @filter.command(
        "b站",
        alias={"B站", "哔哩哔哩", "bili", "bilibili", "Bilibili", "BILI"},
    )
    async def cmd_bili(
        self, event: AstrMessageEvent, keyword: GreedyStr = GreedyStr
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查哔哩哔哩：b站 关键词 / b站 BV号 / b站 热门"""
        # GreedyStr 在极老版本里会退化成类本身，兜一层类型判断
        payload = keyword if isinstance(keyword, str) else ""
        async for result in self._dispatch(event, payload):
            yield result

    @filter.command("b站热门", alias={"B站热门", "哔哩哔哩热门"})
    async def cmd_hot(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """看哔哩哔哩全站热门榜"""
        async for result in self._dispatch(event, "热门"):
            yield result

    # ------------------------------------------------------------------ 兜底入口

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理三件事：带前缀的指令、自动解析链接、回复序号看详情。"""
        try:
            if not self.enable:
                return

            # 别解析自己发出去的消息：text 模式的回复里带视频链接，
            # 不挡住就会自己触发自己，无限循环
            try:
                me = str(event.get_self_id() or "")
                sender = str(event.get_sender_id() or "")
                if me and sender and me == sender:
                    return
            except Exception:
                pass

            text = self._clean_text(event)

            # 1) /b站 xxx 这类带前缀的写法
            if PREFIXED_CMD_RE.match(text):
                payload = PREFIXED_CMD_RE.sub("", text).strip()
                async for result in self._dispatch(event, payload):
                    yield result
                return

            # 2) 已经命中 @filter.command 的消息交回命令处理，别重复响应
            if COMMAND_PREFIX_RE.match(text):
                return

            if not text:
                return

            # 3) 用户回复序号，看之前搜到的某一条
            if self.enable_choice and re.fullmatch(r"\d{1,2}", text):
                async for result in self._handle_choice(event, int(text)):
                    yield result
                return

            # 4) 消息里只有 B 站链接或 BV 号，自动解析
            if self.enable_parse and len(text) <= 200:
                target = self._extract_target(text)
                if target and target[0] in ("video", "url"):
                    # 用独立的冷却 key，别把用户手动查的额度吃掉
                    if self._is_flooding(f"auto|{self._sender_key(event)}"):
                        return
                    async for result in self._send_video_detail(event, target):
                        yield result
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[bilibili] 处理消息出错：{exc}")

    # ------------------------------------------------------------------ 主流程

    async def _dispatch(
        self, event: AstrMessageEvent, raw: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        if not self.enable:
            return

        key = self._sender_key(event)
        if self._is_flooding(key):
            yield event.plain_result("查得有点频繁，缓几秒再试。")
            return

        target = self._extract_target(raw)
        if target is None:
            yield event.plain_result(
                "想查什么？这样用：\n"
                "b站 罗翔 刑法   —— 搜视频\n"
                "b站 BV1GJ411x7h7 —— 看这条视频\n"
                "b站 热门         —— 全站热门榜"
            )
            return

        kind, payload = target

        if kind == "hot":
            async for result in self._send_hot(event):
                yield result
            return

        if kind in ("video", "url"):
            async for result in self._send_video_detail(event, target):
                yield result
            return

        # 搜索
        keyword = str(payload).strip()
        try:
            videos = await self.client.search(
                keyword, limit=self.search_limit, order=self.search_order
            )
        except BiliError as exc:
            logger.warning(f"[bilibili] 搜索失败：{exc}")
            yield event.plain_result(f"没搜成：{exc}")
            return
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[bilibili] 搜索异常：{exc}")
            yield event.plain_result("搜索出错了，稍后再试。")
            return

        if not videos:
            yield event.plain_result(f"没搜到「{keyword}」相关的视频，换个说法试试。")
            return

        if self.enable_choice:
            self._remember(key, videos)

        if self.send_mode == "text":
            yield event.plain_result(self._format_search_text(keyword, videos))
            return

        covers = await self._fetch_covers(videos)
        try:
            image = render_search(keyword, videos, covers)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[bilibili] 出图失败，降级为文本：{exc}")
            yield event.plain_result(self._format_search_text(keyword, videos))
            return

        async for result in self._send_image(event, image):
            yield result

        hint = "回复序号看详情" if self.enable_choice else "点链接直接看"
        yield event.plain_result(
            f"「{keyword}」找到 {len(videos)} 个视频，{hint}。"
        )

    async def _send_video_detail(
        self,
        event: AstrMessageEvent,
        target: tuple[str, str],
    ) -> AsyncGenerator[MessageEventResult, None]:
        kind, payload = target

        bvid, aid = "", 0
        try:
            if kind == "url":
                resolved = await self.client.resolve_share_url(payload)
                if resolved:
                    bvid = resolved
                else:
                    match = AV_IN_URL_RE.search(payload)
                    if not match:
                        yield event.plain_result("这个链接里没找到视频，是不是分享错了？")
                        return
                    aid = int(match.group(1))
            else:
                text = str(payload)
                if text.lower().startswith("av"):
                    aid = int(re.sub(r"\D", "", text) or 0)
                else:
                    bvid = text

            video = await self.client.video_info(bvid=bvid, aid=aid)
        except BiliError as exc:
            logger.warning(f"[bilibili] 查详情失败：{exc}")
            yield event.plain_result(f"没查到这条视频：{exc}")
            return
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[bilibili] 查详情异常：{exc}")
            yield event.plain_result("查询出错了，稍后再试。")
            return

        if self.send_mode == "text":
            yield event.plain_result(self._format_detail_text(video))
            return

        cover = await self.client.fetch_image(video.cover)
        try:
            image = render_detail(video, cover)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[bilibili] 详情出图失败，降级为文本：{exc}")
            yield event.plain_result(self._format_detail_text(video))
            return

        async for result in self._send_image(event, image):
            yield result

    async def _send_hot(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        try:
            videos = await self.client.hot(limit=self.search_limit)
        except BiliError as exc:
            logger.warning(f"[bilibili] 取热门失败：{exc}")
            yield event.plain_result(f"没拿到热门榜：{exc}")
            return
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[bilibili] 取热门异常：{exc}")
            yield event.plain_result("热门榜取不到了，稍后再试。")
            return

        if not videos:
            yield event.plain_result("热门榜是空的，稍后再试。")
            return

        if self.send_mode == "text":
            yield event.plain_result(self._format_search_text("热门", videos, prefix="热门榜"))
            return

        covers = await self._fetch_covers(videos)
        try:
            image = render_hot(videos, covers)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[bilibili] 热门出图失败，降级为文本：{exc}")
            yield event.plain_result(self._format_search_text("热门", videos, prefix="热门榜"))
            return

        async for result in self._send_image(event, image):
            yield result

    # ------------------------------------------------------------------ 序号选择

    async def _handle_choice(
        self, event: AstrMessageEvent, index: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        key = self._sender_key(event)
        session = self._sessions.get(key)
        if session is None:
            return

        videos, expire_at = session
        if expire_at < time.monotonic():
            self._sessions.pop(key, None)
            return

        # 超出范围就不吭声：群里随手发个「9」不该被机器人接话
        if index < 1 or index > len(videos):
            return

        event.stop_event()
        video = videos[index - 1]

        # 列表里的数据不全（没有点赞收藏），再查一次详情
        try:
            video = await self.client.video_info(bvid=video.bvid, aid=video.aid)
        except Exception as exc:
            logger.debug(f"[bilibili] 补查详情失败，用列表数据：{exc}")

        if self.send_mode == "text":
            yield event.plain_result(self._format_detail_text(video))
            return

        cover = await self.client.fetch_image(video.cover)
        try:
            image = render_detail(video, cover)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[bilibili] 详情出图失败，降级为文本：{exc}")
            yield event.plain_result(self._format_detail_text(video))
            return

        async for result in self._send_image(event, image):
            yield result

    def _remember(self, key: str, videos: list[Video]) -> None:
        now = time.monotonic()
        if len(self._sessions) > SESSION_MAX:
            self._sessions = {
                k: v for k, v in self._sessions.items() if v[1] > now
            }
        self._sessions[key] = (videos, now + self.choice_timeout)

    # ------------------------------------------------------------------ AI 工具

    @filter.llm_tool(name="bilibili_search")
    async def bilibili_search_tool(
        self,
        event: AstrMessageEvent,
        keyword: str,
        count: int = 5,
    ) -> str:
        """在哔哩哔哩（B站）搜索视频，返回标题、UP 主、播放量和链接。

        当用户想找 B 站上的视频、想看某个话题的相关视频、问「B站有没有讲 XX 的视频」时调用。

        Args:
            keyword(string): 搜索关键词，尽量具体，例如"罗翔 刑法 入门"
            count(number): 返回几条，1-10，默认 5
        """
        if not self.enable_llm_tool:
            return "B站搜索功能已被管理员关闭。"

        keyword = str(keyword or "").strip()
        if not keyword:
            return "关键词是空的，没法搜。"

        try:
            limit = max(1, min(int(count or 5), 10))
        except (TypeError, ValueError):
            limit = 5

        try:
            videos = await self.client.search(keyword, limit=limit, order=self.search_order)
        except BiliError as exc:
            return f"搜索失败：{exc}"
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[bilibili] 工具调用失败：{exc}")
            return "搜索出错了，稍后再试。"

        if not videos:
            return f"在 B 站没搜到「{keyword}」相关的视频。"

        return self._format_search_text(keyword, videos, for_llm=True)

    # ------------------------------------------------------------------ 发送

    async def _send_image(
        self, event: AstrMessageEvent, png: bytes
    ) -> AsyncGenerator[MessageEventResult, None]:
        path = self.data_dir / f"bili_{int(time.time() * 1000)}.png"
        try:
            path.write_bytes(png)
        except OSError as exc:
            logger.warning(f"[bilibili] 图片写盘失败：{exc}")
            yield event.plain_result("图片生成失败，稍后再试。")
            return

        self._cleanup_old_images()
        yield event.image_result(str(path))

    def _cleanup_old_images(self) -> None:
        """顺手清掉一天前的图，免得 data/plugin_data 越攒越大。"""
        cutoff = time.time() - 86400
        try:
            for item in self.data_dir.glob("bili_*.png"):
                try:
                    if item.stat().st_mtime < cutoff:
                        item.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    async def _fetch_covers(self, videos: list[Video]) -> list[bytes | None]:
        if not videos:
            return []
        results = await asyncio.gather(
            *(self.client.fetch_image(video.cover) for video in videos),
            return_exceptions=True,
        )
        return [item if isinstance(item, bytes) else None for item in results]

    # ------------------------------------------------------------------ 文本格式

    def _format_search_text(
        self,
        keyword: str,
        videos: list[Video],
        prefix: str = "搜索",
        for_llm: bool = False,
    ) -> str:
        lines = [
            f"哔哩哔哩{prefix}「{keyword}」，共 {len(videos)} 条："
            if not for_llm
            else f"哔哩哔哩搜索「{keyword}」的结果（{len(videos)} 条）："
        ]
        for index, video in enumerate(videos, 1):
            bits = [f"{format_count(video.play)}播放"]
            if video.duration:
                bits.append(format_duration(video.duration))
            if video.pubdate and not for_llm:
                bits.append(format_date(video.pubdate))
            lines.append("")
            lines.append(f"{index}. {video.title}")
            lines.append(f"   {video.author}  ·  {'  ·  '.join(bits)}")
            lines.append(f"   {video.page_url}")
        return "\n".join(lines).strip()

    def _format_detail_text(self, video: Video) -> str:
        lines = [f"【{video.title}】"]
        meta = [video.author]
        if video.category:
            meta.append(video.category)
        if video.pubdate:
            meta.append(format_date(video.pubdate))
        lines.append("  ·  ".join(part for part in meta if part))
        lines.append(
            "播放 {}  ·  弹幕 {}  ·  点赞 {}  ·  收藏 {}".format(
                format_count(video.play),
                format_count(video.danmaku),
                format_count(video.like),
                format_count(video.favorite),
            )
        )
        if video.duration:
            lines.append(f"时长 {format_duration(video.duration)}")
        if video.desc:
            desc = video.desc.strip().replace("\r", "")
            lines.append("")
            lines.append(desc[:120] + ("…" if len(desc) > 120 else ""))
        lines.append("")
        lines.append(video.page_url)
        return "\n".join(lines)

    # ------------------------------------------------------------------ 工具

    def _extract_target(self, raw: str) -> tuple[str, str] | None:
        """把用户输入归类成 (hot|video|url|search, payload)。"""
        text = str(raw or "").strip()
        if not text:
            return None

        # 去掉开头的唤醒词和首尾标点，让「b站：罗翔」也能正常搜
        text = re.sub(
            r"^(?:哔哩哔哩|bilibili|bili|b站)[\s:：,，]*", "", text, flags=re.I
        ).strip("：:，,。. ")

        if not text:
            return None

        if text in HOT_WORDS:
            return ("hot", "")

        # BV 号可以直接定位
        match = BV_RE.search(text)
        if match:
            return ("video", match.group(0))

        # av 号
        match = AV_ONLY_RE.match(text)
        if match:
            return ("video", f"av{match.group(1)}")

        # 分享链接
        if "bilibili.com/video" in text or "b23.tv" in text or "bili2233" in text:
            url = re.search(r"https?://\S+", text)
            return ("url", url.group(0) if url else text)

        return ("search", text)

    @staticmethod
    def _clean_text(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        text = AT_PREFIX_RE.sub("", text)
        return text.strip()

    @staticmethod
    def _sender_key(event: AstrMessageEvent) -> str:
        try:
            origin = str(event.unified_msg_origin or "")
        except Exception:
            origin = ""
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        return f"{origin}|{sender}"

    def _is_flooding(self, key: str) -> bool:
        if self.rate_limit_seconds <= 0:
            return False
        now = time.monotonic()
        last = self._last_request.get(key, 0.0)
        if now - last < self.rate_limit_seconds:
            return True
        if len(self._last_request) > SESSION_MAX:
            self._last_request.clear()
        self._last_request[key] = now
        return False

    # ------------------------------------------------------------------ 生命周期

    async def terminate(self) -> None:
        self._last_request.clear()
        self._sessions.clear()
        await self.client.close()
        logger.info("[bilibili] 插件已卸载")
