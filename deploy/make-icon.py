#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make-icon.py —— 生成 gt-wb-gateway 的图标（.ico，多尺寸）。

设计：深色圆角方底 + 中央「网关节点」三重意象
  · 外圈：一条循环的数据轨道（代表"中转/转发"）
  · 内芯：发光六边形节点（代表"核心网关"）
  · 触点：三个方向的进出小圆点（代表多上游/多客户端）
配色沿用两个看板的主题色：蓝 #5b8cff / 青 #22d3ee / 绿 #34d399。

用法：python deploy/make-icon.py
产物：deploy/gtwb.ico（16/24/32/48/64/128/256 多尺寸）
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# ---- 主题色（与 status_server.py / wb-usage-widget 的 CSS 变量一致）----
BG_TOP = (16, 21, 29)      # #10151d
BG_BOT = (7, 10, 15)       # #070a0f
BLUE = (91, 140, 255)      # #5b8cff
CYAN = (34, 211, 238)      # #22d3ee
GREEN = (52, 211, 153)     # #34d399
WHITE = (232, 236, 243)    # #e8ecf3

SS = 4  # 超采样倍数，先画大图再缩小，边缘更干净
SIZES = [16, 24, 32, 48, 64, 128, 256]


def _round_rect_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    return m


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    g = Image.new("RGB", (1, size))
    px = g.load()
    for y in range(size):
        t = y / max(size - 1, 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return g.resize((size, size), Image.BILINEAR)


def _glow(layer: Image.Image, radius: int, strength: float = 1.0) -> Image.Image:
    """把图层做成辉光：模糊后按强度叠加。"""
    g = layer.filter(ImageFilter.GaussianBlur(radius))
    if strength != 1.0:
        g = g.point(lambda v: int(min(255, v * strength)))
    return g


def render(size: int) -> Image.Image:
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ---------- 1) 底：深色圆角方 + 竖向渐变 ----------
    base = _vertical_gradient(S, BG_TOP, BG_BOT).convert("RGBA")
    base.putalpha(_round_rect_mask(S))
    img.alpha_composite(base)

    # 内描边（细亮线，让图标有"玻璃面板"感）
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dr = ImageDraw.Draw(rim)
    inset = max(1, int(S * 0.012))
    dr.rounded_rectangle(
        [inset, inset, S - 1 - inset, S - 1 - inset],
        radius=int(S * 0.22) - inset,
        outline=(*BLUE, 90),
        width=max(1, int(S * 0.012)),
    )
    img.alpha_composite(rim)

    cx = cy = S / 2

    # ---------- 2) 外轨道：循环虚线圆环（中转/转发）----------
    orbit = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    do = ImageDraw.Draw(orbit)
    R = S * 0.335
    lw = max(2, int(S * 0.030))
    seg = 12            # 12 段虚线
    gap = 0.30          # 每段留 30% 空隙
    for i in range(seg):
        a0 = (i / seg) * 360
        a1 = a0 + (360 / seg) * (1 - gap)
        do.arc(
            [cx - R, cy - R, cx + R, cy + R],
            start=a0, end=a1,
            fill=(*BLUE, 235),
            width=lw,
        )
    img.alpha_composite(_glow(orbit, int(S * 0.030), 0.85))
    img.alpha_composite(orbit)

    # ---------- 3) 三个进出触点（多上游 / 多客户端）----------
    dots = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    dot_r = S * 0.042
    for ang_deg, col in ((-90, CYAN), (30, GREEN), (150, BLUE)):
        a = math.radians(ang_deg)
        px, py = cx + R * math.cos(a), cy + R * math.sin(a)
        dd.ellipse([px - dot_r, py - dot_r, px + dot_r, py + dot_r], fill=(*col, 255))
    img.alpha_composite(_glow(dots, int(S * 0.045), 1.15))
    img.alpha_composite(dots)

    # ---------- 4) 内芯：发光六边形节点 ----------
    hexa = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dh = ImageDraw.Draw(hexa)
    hR = S * 0.205
    pts = [
        (cx + hR * math.cos(math.radians(60 * i - 90)),
         cy + hR * math.sin(math.radians(60 * i - 90)))
        for i in range(6)
    ]
    dh.polygon(pts, fill=(*BLUE, 255))
    img.alpha_composite(_glow(hexa, int(S * 0.055), 1.5))

    # 六边形内芯：亮色渐变靠"叠加一个更小的青色六边形"
    inner = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    di = ImageDraw.Draw(inner)
    hR2 = hR * 0.62
    pts2 = [
        (cx + hR2 * math.cos(math.radians(60 * i - 90)),
         cy + hR2 * math.sin(math.radians(60 * i - 90)))
        for i in range(6)
    ]
    di.polygon(pts2, fill=(*WHITE, 250))
    inner = inner.filter(ImageFilter.GaussianBlur(S * 0.005))
    img.alpha_composite(inner)

    # ---------- 5) 收尾：裁圆角 + 缩到目标尺寸 ----------
    img.putalpha(Image.composite(
        img.getchannel("A"), Image.new("L", (S, S), 0), _round_rect_mask(S)
    ))
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """
    手写 ICO 容器。

    Pillow 的 ICO 保存对 append_images 处理不稳定（实测只落了 16x16，
    结果就是 Windows 里图标糊成一团）。这里按 ICONDIR/ICONDIRENTRY 规范
    自行拼装，所有尺寸都存 PNG 帧（Vista+ 原生支持）。
    """
    import io as _io
    import struct as _struct

    blobs: list[bytes] = []
    for im in images:
        b = _io.BytesIO()
        im.save(b, format="PNG")
        blobs.append(b.getvalue())

    n = len(blobs)
    header = _struct.pack("<HHH", 0, 1, n)  # reserved, type=1(icon), count
    offset = 6 + 16 * n
    entries = bytearray()
    for im, blob in zip(images, blobs):
        w, h = im.size
        entries += _struct.pack(
            "<BBBBHHII",
            0 if w >= 256 else w,   # 256 用 0 表示
            0 if h >= 256 else h,
            0,                      # 调色板数
            0,                      # reserved
            1,                      # color planes
            32,                     # bits per pixel
            len(blob),
            offset,
        )
        offset += len(blob)

    path.write_bytes(header + bytes(entries) + b"".join(blobs))


def main() -> int:
    here = Path(__file__).resolve().parent
    out = here / "gtwb.ico"
    png_out = here / "gtwb-icon.png"

    frames = [render(s) for s in SIZES]
    write_ico(out, frames)
    # 顺带存一张 256 的 PNG，方便预览/别的用途
    frames[-1].save(png_out, format="PNG")

    print(f"OK: {out}  ({out.stat().st_size} bytes, {len(SIZES)} sizes)")
    print(f"OK: {png_out}  ({png_out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
