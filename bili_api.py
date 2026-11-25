# -*- coding: utf-8 -*-
"""哔哩哔哩公开 Web 接口客户端。

只用对外公开的 Web API：搜索、视频详情、排行榜、短链跳转。
不登录、不下载视频流、不绕过任何权限限制。

B 站从 2023 年起对无 cookie 的请求做风控，这里先访问 ``/x/frontend/finger/spi``
拿一对 ``buvid3/buvid4`` 放进 cookie，之后的请求都带着它，能显著降低被
``-352`` 拦截的概率。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.bilibili.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 命中这些返回码时，友好地提示用户稍后再试，而不是甩一串报错
RISK_CODES = frozenset({-352, -412, -500, -509, -799})

#: 搜索结果标题里的关键词高亮，形如 ``<em class="keyword">罗翔</em>``
_HIGHLIGHT_RE = re.compile(r"</?em[^>]*>", re.I)

#: BV 号：BV + 10 位 base58 字符
BV_RE = re.compile(r"BV[1-9A-HJ-NP-Za-km-z]{10}")


class BiliError(Exception):
    """接口层错误，message 直接可以拿去展示给用户。"""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Video:
    """统一的视频信息，搜索结果和详情接口都归一到这里。"""

    bvid: str = ""
    aid: int = 0
    title: str = ""
    author: str = ""
    mid: int = 0
    cover: str = ""
    duration: int = 0  # 秒
    play: int = 0
    danmaku: int = 0
    like: int = 0
    coin: int = 0
    favorite: int = 0
    reply: int = 0
    pubdate: int = 0
    desc: str = ""
    category: str = ""

    @property
    def page_url(self) -> str:
        if self.bvid:
            return f"https://www.bilibili.com/video/{self.bvid}"
        if self.aid:
            return f"https://www.bilibili.com/video/av{self.aid}"
        return "https://www.bilibili.com"


# ---------------------------------------------------------------- 小工具


def to_int(value: Any, default: int = 0) -> int:
    """B 站偶尔把数字塞成 ``"1.2万"`` 或 ``"--"``，一律容错成 int。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text or text in {"-", "--", "N/A"}:
        return default
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))
    except ValueError:
        return default


def strip_highlight(text: str) -> str:
    """去掉搜索结果的 ``<em>`` 高亮标签，顺带还原实体转义。"""
    text = _HIGHLIGHT_RE.sub("", str(text or ""))
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .strip()
    )


def fix_cover(url: str) -> str:
    """搜索结果里的封面是协议相对地址 ``//i0.hdslb.com/...``。"""
    url = str(url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def parse_duration(raw: Any) -> int:
    """把 ``"12:34"`` / ``"1:02:03"`` / ``"2474:14"`` 统一成秒。"""
    if isinstance(raw, (int, float)):
        return max(0, int(raw))
    text = str(raw or "").strip()
    if not text:
        return 0
    total = 0
    for part in text.split(":"):
        part = part.strip()
        if not part.isdigit():
            return 0
        total = total * 60 + int(part)
    return total


def format_duration(seconds: int) -> str:
    """秒 → ``41:14:14`` / ``03:07``，用于卡片展示。"""
    seconds = max(0, int(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_count(value: int) -> str:
    """4244661 → ``424.5万``；不足一万保持原样。"""
    value = max(0, int(value or 0))
    if value < 10000:
        return str(value)
    if value < 100_000_000:
        return f"{value / 10000:.1f}万".replace(".0万", "万")
    return f"{value / 100_000_000:.1f}亿".replace(".0亿", "亿")


def format_date(timestamp: int) -> str:
    """发布时间戳 → ``2024-05-06``。"""
    if not timestamp:
        return ""
    import datetime

    try:
        return datetime.datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


# ---------------------------------------------------------------- 客户端


class BiliClient:
    """一个插件实例共用一个客户端，内部持有 cookie。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            headers=HEADERS,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._cookie_ready = False
        self._cookie_lock = asyncio.Lock()

    # ------------------------------------------------------------ 底层请求

    async def _ensure_cookie(self) -> None:
        """拿 buvid3/buvid4，失败也不阻塞主流程。"""
        if self._cookie_ready:
            return
        async with self._cookie_lock:
            if self._cookie_ready:
                return
            try:
                response = await self._client.get(
                    f"{API}/x/frontend/finger/spi", timeout=min(self.timeout, 8.0)
                )
                data = response.json().get("data") or {}
                for name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
                    value = str(data.get(key) or "").strip()
                    if value:
                        self._client.cookies.set(name, value, domain=".bilibili.com")
            except Exception:
                # 拿不到就裸奔，多数情况下仍能查成功
                pass
            finally:
                self._cookie_ready = True

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._ensure_cookie()
        try:
            response = await self._client.get(f"{API}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise BiliError("连接哔哩哔哩超时了，稍后再试。") from exc
        except httpx.HTTPError as exc:
            raise BiliError("连不上哔哩哔哩，检查一下网络？") from exc

        if response.status_code == 412:
            raise BiliError("请求被哔哩哔哩限流了，过一会儿再试。", -412)
        if response.status_code >= 400:
            raise BiliError(f"哔哩哔哩返回了 HTTP {response.status_code}。")

        try:
            payload = response.json()
        except ValueError as exc:
            raise BiliError("哔哩哔哩返回的数据看不懂，可能被风控了。") from exc

        code = to_int(payload.get("code"))
        if code != 0:
            message = str(payload.get("message") or payload.get("msg") or "").strip()
            if code in RISK_CODES:
                raise BiliError("被哔哩哔哩风控拦了，过几分钟再试。", code)
            if code == -404:
                raise BiliError("视频不存在，可能已经被删了。", code)
            if code == 62002:
                raise BiliError("这个视频是私密的，看不了。", code)
            if code == 62004:
                raise BiliError("这个视频还在审核中。", code)
            if code == -403:
                raise BiliError("没有权限查看这个视频。", code)
            raise BiliError(message or f"哔哩哔哩接口报错（{code}）", code)

        return payload.get("data")

    # ------------------------------------------------------------ 对外方法

    async def search(
        self,
        keyword: str,
        limit: int = 8,
        order: str = "totalrank",
        page: int = 1,
    ) -> list[Video]:
        """按关键词搜视频。order 可选 totalrank / click / pubdate / dm / stow。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return []

        data = await self._get_json(
            "/x/web-interface/search/type",
            {
                "search_type": "video",
                "keyword": keyword,
                "page": max(1, int(page)),
                "order": order or "totalrank",
            },
        )
        results = (data or {}).get("result") or []
        videos = [self._from_search(item) for item in results if isinstance(item, dict)]

        # 搜索结果里混着付费课程/推广卡片，这类没有 bvid，点不进详情，
        # 留着只会让用户挑到一条打不开的。有正常视频就优先用正常的。
        usable = [video for video in videos if video.bvid]
        if usable:
            videos = usable
        videos = [video for video in videos if video.bvid or video.aid]
        return videos[: max(1, int(limit))]

    async def video_info(self, bvid: str = "", aid: int = 0) -> Video:
        """按 BV 号或 av 号查详情。"""
        bvid = str(bvid or "").strip()
        if not bvid and not aid:
            raise BiliError("没给 BV 号，查不了。")

        params = {"bvid": bvid} if bvid else {"aid": int(aid)}
        data = await self._get_json("/x/web-interface/view", params)
        if not isinstance(data, dict) or not data.get("bvid"):
            raise BiliError("没查到这个视频。")
        return self._from_view(data)

    async def hot(self, limit: int = 8) -> list[Video]:
        """全站热门榜。"""
        data = await self._get_json(
            "/x/web-interface/ranking/v2", {"rid": 0, "type": "all"}
        )
        items = (data or {}).get("list") or []
        videos = [self._from_ranking(item) for item in items if isinstance(item, dict)]
        return videos[: max(1, int(limit))]

    async def resolve_share_url(self, url: str) -> str:
        """把 ``b23.tv`` 短链展开成真实地址，返回里面的 BV 号（可能为空）。"""
        url = str(url or "").strip()
        if not url:
            return ""
        match = BV_RE.search(url)
        if match:
            return match.group(0)
        if "b23.tv" not in url and "bili2233" not in url:
            return ""

        try:
            response = await self._client.get(url, follow_redirects=True)
        except httpx.HTTPError:
            return ""

        match = BV_RE.search(str(response.url))
        if match:
            return match.group(0)
        match = BV_RE.search(response.text[:2000])
        return match.group(0) if match else ""

    async def fetch_image(self, url: str) -> bytes | None:
        """下载封面。B 站图床有防盗链，必须带 Referer。"""
        url = str(url or "").strip()
        if not url.startswith("http"):
            return None
        try:
            response = await self._client.get(url, timeout=min(self.timeout, 8.0))
            if response.status_code == 200 and response.content:
                return response.content
        except httpx.HTTPError:
            return None
        return None

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------ 字段归一

    def _from_search(self, item: dict) -> Video:
        bvid = str(item.get("bvid") or "").strip()
        return Video(
            bvid=bvid,
            aid=to_int(item.get("aid") or item.get("id")),
            title=strip_highlight(item.get("title")),
            author=str(item.get("author") or "").strip(),
            mid=to_int(item.get("mid")),
            cover=fix_cover(item.get("pic")),
            duration=parse_duration(item.get("duration")),
            play=to_int(item.get("play")),
            danmaku=to_int(item.get("video_review") or item.get("danmaku")),
            like=to_int(item.get("like")),
            favorite=to_int(item.get("favorites")),
            pubdate=to_int(item.get("pubdate")),
            desc=str(item.get("description") or "").strip(),
            category=str(item.get("typename") or "").strip(),
        )

    def _from_view(self, data: dict) -> Video:
        stat = data.get("stat") or {}
        owner = data.get("owner") or {}
        return Video(
            bvid=str(data.get("bvid") or "").strip(),
            aid=to_int(data.get("aid")),
            title=str(data.get("title") or "").strip(),
            author=str(owner.get("name") or "").strip(),
            mid=to_int(owner.get("mid")),
            cover=fix_cover(data.get("pic")),
            duration=to_int(data.get("duration")),
            play=to_int(stat.get("view")),
            danmaku=to_int(stat.get("danmaku")),
            like=to_int(stat.get("like")),
            coin=to_int(stat.get("coin")),
            favorite=to_int(stat.get("favorite")),
            reply=to_int(stat.get("reply")),
            pubdate=to_int(data.get("pubdate")),
            desc=str(data.get("desc") or "").strip(),
            category=str(data.get("tname") or data.get("tname_v2") or "").strip(),
        )

    def _from_ranking(self, item: dict) -> Video:
        stat = item.get("stat") or {}
        owner = item.get("owner") or {}
        return Video(
            bvid=str(item.get("bvid") or "").strip(),
            aid=to_int(item.get("aid")),
            title=str(item.get("title") or "").strip(),
            author=str(owner.get("name") or "").strip(),
            mid=to_int(owner.get("mid")),
            cover=fix_cover(item.get("pic")),
            duration=to_int(item.get("duration")),
            play=to_int(stat.get("view")),
            danmaku=to_int(stat.get("danmaku")),
            like=to_int(stat.get("like")),
            favorite=to_int(stat.get("favorite")),
            reply=to_int(stat.get("reply")),
            pubdate=to_int(item.get("pubdate")),
            desc=str(item.get("desc") or "").strip(),
            category=str(item.get("tname") or "").strip(),
        )
