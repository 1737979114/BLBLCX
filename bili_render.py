# -*- coding: utf-8 -*-
"""把视频信息画成卡片图。

用 Pillow 本地合成，不依赖任何在线渲染服务——AstrBot 的 t2i 默认是关的，
走 PIL 这条路可以保证「一定发得出图」。
"""

from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from bili_api import Video, format_count, format_date, format_duration

# ---------------------------------------------------------------- 视觉常量

WIDTH = 800
MARGIN = 16
PAD = 28
RADIUS = 18

ACCENT = (251, 114, 153)  # B 站粉 #FB7299
INK = (28, 32, 38)
INK_SOFT = (96, 104, 114)
INK_FAINT = (152, 159, 168)
LINE = (233, 236, 240)
PAGE_BG = (242, 243, 245)
CARD_BG = (255, 255, 255)
BLOCK_BG = (247, 248, 250)

THUMB_W = 176
THUMB_H = 110
THUMB_RADIUS = 10
GAP = 22

TITLE_SIZE = 22
TITLE_LINE_H = 30

HEADER_H = 88  # 粉点行 30 + 标题行 38 + 分隔线后间距 20


# ---------------------------------------------------------------- 字体


def _font_candidates() -> dict[bool, list[str]]:
    """按平台列出可用的中文字体，粗体/常规各一组。"""
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    bold = [
        str(win / name)
        for name in ("msyhbd.ttc", "msyhbd.ttf", "simhei.ttf", "SourceHanSansCN-Bold.otf")
    ]
    regular = [
        str(win / name)
        for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "SourceHanSansCN-Regular.otf")
    ]

    unix_dirs = (
        "/usr/share/fonts/truetype/wqy/",
        "/usr/share/fonts/opentype/noto/",
        "/usr/share/fonts/truetype/noto/",
        "/usr/share/fonts/truetype/dejavu/",
        "/System/Library/Fonts/",
        "/Library/Fonts/",
    )
    unix_bold = (
        "wqy-zenhei.ttc",
        "NotoSansCJK-Bold.ttc",
        "NotoSansCJKsc-Bold.otf",
        "DejaVuSans-Bold.ttf",
        "PingFang.ttc",
    )
    unix_regular = (
        "wqy-zenhei.ttc",
        "NotoSansCJK-Regular.ttc",
        "NotoSansCJKsc-Regular.otf",
        "DejaVuSans.ttf",
        "PingFang.ttc",
    )
    for directory in unix_dirs:
        bold.extend(directory + name for name in unix_bold)
        regular.extend(directory + name for name in unix_regular)

    return {True: bold, False: regular}


_CANDIDATES = _font_candidates()


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in _CANDIDATES[bold]:
        if not Path(path).exists():
            continue
        for index in (0, 1):
            try:
                return ImageFont.truetype(path, size, index=index)
            except OSError:
                continue
    # 没有中文字体时至少别崩，方块字也比发不出去强
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 不支持 size
        return ImageFont.load_default()


# ---------------------------------------------------------------- 文本排版

#: 连续 ASCII（英文单词、数字、URL 片段）当整体，避免把单词从中间劈开
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-./:?'’#&=+%~]+|\s+|.", re.S)


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按像素宽度折行，中英混排都不容易崩。"""
    lines: list[str] = []
    for paragraph in str(text or "").split("\n"):
        current = ""
        for token in _TOKEN_RE.findall(paragraph):
            if token == "\r":
                continue
            if font.getlength(current + token) <= max_width:
                current += token
                continue

            if current.strip():
                lines.append(current.rstrip())
                current = ""

            token = token.lstrip()
            # 单个 token 本身就超宽（超长 URL），只能硬切
            while token and font.getlength(token) > max_width:
                cut = len(token)
                while cut > 1 and font.getlength(token[:cut]) > max_width:
                    cut -= 1
                lines.append(token[:cut])
                token = token[cut:]
            current = token

        if current.strip():
            lines.append(current.rstrip())
    return lines


def _ellipsize(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """确定要截断时使用，末尾补省略号。"""
    text = text.rstrip()
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…" if text else ""


def fit_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """宽了就截断加省略号，宽度够就原样返回。"""
    text = str(text or "").strip()
    if font.getlength(text) <= max_width:
        return text
    return _ellipsize(text, font, max_width)


def clamp_lines(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """折行并限制行数，超出部分用省略号收尾。"""
    lines = wrap_text(text, font, max_width)
    if len(lines) <= max_lines:
        return lines
    head = lines[:max_lines]
    head[-1] = _ellipsize(head[-1] + "".join(lines[max_lines:]), font, max_width)
    return head


# ---------------------------------------------------------------- 图片处理


def _round(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, img.size[0] - 1, img.size[1] - 1], radius=radius, fill=255
    )
    img.putalpha(mask)
    return img


def _load_cover(
    data: bytes | None, size: tuple[int, int], radius: int = THUMB_RADIUS
) -> Image.Image:
    """封面裁成固定比例；没下载到就画一个占位块。"""
    if data:
        try:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            img = ImageOps.fit(
                img.convert("RGB"), size, Image.Resampling.LANCZOS, centering=(0.5, 0.35)
            )
            return _round(img, radius)
        except Exception:
            pass

    placeholder = Image.new("RGB", size, (238, 240, 243))
    ImageDraw.Draw(placeholder).line(
        [(0, size[1] // 2), (size[0], size[1] // 2)], fill=(224, 227, 232), width=2
    )
    return _round(placeholder, radius)


def clean_desc(text: str) -> str:
    """简介里常有一堆单独成行的外链，画在卡片上纯属噪音，去掉。"""
    lines: list[str] = []
    for line in str(text or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"https?://\S+", line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _to_png(canvas: Image.Image) -> bytes:
    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _card_with_shadow(width: int, height: int) -> Image.Image:
    """浅灰底 + 白卡片 + 柔和投影，PNG 出来像一张真卡片。"""
    width, height = max(width, MARGIN * 2 + 40), max(height, MARGIN * 2 + 40)
    canvas = Image.new("RGB", (width, height), PAGE_BG)

    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [MARGIN, MARGIN + 4, width - MARGIN, height - MARGIN + 4],
        radius=RADIUS,
        fill=(17, 24, 39, 30),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    canvas.paste(shadow, (0, 0), shadow)

    ImageDraw.Draw(canvas).rounded_rectangle(
        [MARGIN, MARGIN, width - MARGIN, height - MARGIN],
        radius=RADIUS,
        fill=CARD_BG,
    )
    return canvas


# ---------------------------------------------------------------- 公共片段


def _draw_header(
    draw: ImageDraw.ImageDraw,
    heading: str,
    subtitle: str,
    y: int,
    left: int,
    content_w: int,
) -> int:
    """粉点 + 哔哩哔哩 + 右侧统计 + 大标题，返回正文起始 y。"""
    draw.rounded_rectangle([left, y + 3, left + 7, y + 19], radius=4, fill=ACCENT)
    draw.text((left + 16, y), "哔哩哔哩", font=_font(16, True), fill=ACCENT)

    if subtitle:
        sub_font = _font(15)
        draw.text(
            (left + content_w - sub_font.getlength(subtitle), y + 1),
            subtitle,
            font=sub_font,
            fill=INK_FAINT,
        )

    y += 30
    heading_font = _font(25, True)
    draw.text(
        (left, y),
        fit_text(heading, heading_font, content_w),
        font=heading_font,
        fill=INK,
    )

    y += 38
    draw.line([left, y, left + content_w, y], fill=LINE, width=1)
    return y + 20


def _draw_video_row(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    video: Video,
    cover: bytes | None,
    index: int,
    y: int,
    left: int,
    content_w: int,
) -> int:
    """一条视频：封面 + 序号 + 时长 + 标题 + UP 主 + 数据，返回行高。"""
    text_x = left + THUMB_W + 18
    text_w = content_w - THUMB_W - 18

    title_font = _font(TITLE_SIZE, True)
    title_lines = clamp_lines(video.title or "（无标题）", title_font, text_w, 2)

    author_font = _font(17)
    meta_font = _font(15)

    author_line = fit_text(video.author or "未知 UP 主", author_font, text_w)

    meta_bits = [f"{format_count(video.play)}播放"]
    if video.danmaku:
        meta_bits.append(f"{format_count(video.danmaku)}弹幕")
    if video.duration:
        meta_bits.append(format_duration(video.duration))
    if video.pubdate:
        meta_bits.append(format_date(video.pubdate))
    meta_text = fit_text("  ·  ".join(meta_bits), meta_font, text_w)

    text_height = len(title_lines) * TITLE_LINE_H + 6 + 26 + 8 + 20
    row_height = max(THUMB_H, text_height)

    # 封面
    thumb = _load_cover(cover, (THUMB_W, THUMB_H))
    canvas.paste(thumb, (left, y), thumb)

    # 序号徽章
    badge_w = 26 if index < 10 else 34
    draw.rounded_rectangle(
        [left + 8, y + 8, left + 8 + badge_w, y + 32], radius=8, fill=ACCENT
    )
    number_font = _font(14, True)
    number = str(index)
    draw.text(
        (left + 8 + (badge_w - number_font.getlength(number)) / 2, y + 11),
        number,
        font=number_font,
        fill=(255, 255, 255),
    )

    # 时长角标
    if video.duration:
        duration_text = format_duration(video.duration)
        duration_font = _font(13, True)
        label_w = int(duration_font.getlength(duration_text)) + 14
        label_h = 22
        label_x = left + THUMB_W - label_w - 6
        label_y = y + THUMB_H - label_h - 6
        overlay = Image.new("RGBA", (label_w, label_h), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(
            [0, 0, label_w - 1, label_h - 1], radius=6, fill=(0, 0, 0, 150)
        )
        canvas.paste(overlay, (label_x, label_y), overlay)
        draw.text(
            (label_x + 7, label_y + 3),
            duration_text,
            font=duration_font,
            fill=(255, 255, 255),
        )

    # 标题 / UP 主 / 数据
    cursor = y + 1
    for line in title_lines:
        draw.text((text_x, cursor), line, font=title_font, fill=INK)
        cursor += TITLE_LINE_H

    cursor += 4
    draw.text((text_x, cursor), author_line, font=author_font, fill=INK_SOFT)
    cursor += 28
    draw.text((text_x, cursor), meta_text, font=meta_font, fill=INK_FAINT)

    return row_height


# ---------------------------------------------------------------- 对外渲染


def render_search(
    keyword: str,
    videos: list[Video],
    covers: list[bytes | None] | None = None,
    heading: str | None = None,
) -> bytes:
    """搜索结果卡片（热门榜复用同一版式）。"""
    covers = covers or []
    left = MARGIN + PAD
    content_w = WIDTH - 2 * (MARGIN + PAD)

    rows: list[tuple[Video, bytes | None]] = []
    total_h = MARGIN + PAD + HEADER_H

    title_font = _font(TITLE_SIZE, True)
    for index, video in enumerate(videos):
        cover = covers[index] if index < len(covers) else None
        lines = clamp_lines(video.title or "（无标题）", title_font, content_w - THUMB_W - 18, 2)
        row_h = max(THUMB_H, len(lines) * TITLE_LINE_H + 6 + 26 + 8 + 20)
        rows.append((video, cover))
        total_h += row_h + GAP

    total_h += PAD + MARGIN - GAP

    canvas = _card_with_shadow(WIDTH, int(total_h))
    draw = ImageDraw.Draw(canvas)

    y = MARGIN + PAD
    y = _draw_header(
        draw, heading or f"搜索「{keyword}」", f"{len(videos)} 个结果", y, left, content_w
    )

    for index, (video, cover) in enumerate(rows, 1):
        row_h = _draw_video_row(
            canvas, draw, video, cover, index, y, left, content_w
        )
        y += row_h + GAP

        if index < len(rows):
            separator_y = y - GAP // 2
            draw.line([left, separator_y, left + content_w, separator_y], fill=(240, 242, 245))

    return _to_png(canvas)


def render_detail(video: Video, cover: bytes | None = None) -> bytes:
    """单条视频详情卡片。"""
    left = MARGIN + PAD
    content_w = WIDTH - 2 * (MARGIN + PAD)
    cover_h = int(content_w * 9 / 16)

    title_font = _font(26, True)
    title_lines = clamp_lines(video.title or "（无标题）", title_font, content_w, 2)

    meta_font = _font(16)
    meta_parts = [
        part
        for part in (video.author, video.category, format_date(video.pubdate))
        if part
    ]
    meta_text = fit_text("  ·  ".join(meta_parts), meta_font, content_w)

    desc_font = _font(16)
    desc_text = clean_desc(video.desc)
    desc_lines = (
        clamp_lines(desc_text, desc_font, content_w - 20, 3) if desc_text else []
    )

    stats = [
        ("播放", format_count(video.play)),
        ("弹幕", format_count(video.danmaku)),
        ("点赞", format_count(video.like)),
        ("收藏", format_count(video.favorite)),
    ]
    stats_h = 84

    height = (
        MARGIN * 2
        + PAD
        + cover_h
        + 26
        + len(title_lines) * 38
        + 4
        + 34
        + stats_h
        + 22
        + (len(desc_lines) * 26 + 22 if desc_lines else 0)
        + 22
    )

    canvas = _card_with_shadow(WIDTH, int(height))
    draw = ImageDraw.Draw(canvas)

    y = MARGIN + PAD

    # 大封面
    big = _load_cover(cover, (content_w, cover_h), radius=14)
    canvas.paste(big, (left, y), big)
    y += cover_h + 26

    # 标题
    for line in title_lines:
        draw.text((left, y), line, font=title_font, fill=INK)
        y += 38

    y += 4
    draw.text((left, y), meta_text, font=meta_font, fill=INK_SOFT)
    y += 34

    # 数据四宫格
    gap = 12
    cell_w = int((content_w - gap * 3) / 4)
    for index, (label, value) in enumerate(stats):
        x = left + index * (cell_w + gap)
        draw.rounded_rectangle([x, y, x + cell_w, y + stats_h], radius=12, fill=BLOCK_BG)

        value_font = _font(22, True)
        label_font = _font(14)
        draw.text(
            (x + (cell_w - value_font.getlength(value)) / 2, y + 15),
            value,
            font=value_font,
            fill=INK,
        )
        draw.text(
            (x + (cell_w - label_font.getlength(label)) / 2, y + 50),
            label,
            font=label_font,
            fill=INK_FAINT,
        )
    y += stats_h + 22

    # 简介
    if desc_lines:
        draw.rounded_rectangle(
            [left, y, left + 3, y + len(desc_lines) * 26 - 6], radius=2, fill=(245, 214, 226)
        )
        for line in desc_lines:
            draw.text((left + 14, y), line, font=desc_font, fill=INK_SOFT)
            y += 26
        y += 22

    # 底部：BV 号 + 链接
    draw.text(
        (left, y),
        f"{video.bvid or ('av' + str(video.aid))}   {video.page_url}",
        font=_font(14),
        fill=INK_FAINT,
    )

    return _to_png(canvas)


def render_hot(videos: list[Video], covers: list[bytes | None] | None = None) -> bytes:
    """热门榜。"""
    return render_search("", videos, covers, heading="哔哩哔哩热门榜")
