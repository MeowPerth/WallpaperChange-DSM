#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯标准库生成套件图标（无 PIL / ImageMagick 依赖）。

设计：蓝绿渐变圆角底 + 白色相框 + 太阳/山峦剪影 + 绿色对勾徽标。
输出：RGBA PNG，4x 超采样抗锯齿，覆盖 DSM 主菜单与套件中心所需尺寸。
"""
import math
import os
import struct
import sys
import zlib

SIZES = [16, 24, 32, 48, 64, 72, 256]
SS = 4  # 超采样倍数

C1 = (47, 138, 240)     # 左上
C2 = (23, 179, 154)     # 右下
WHITE = (255, 255, 255)
SUN = (255, 209, 102)
GREEN = (34, 180, 92)


def lerp(a, b, t):
    return a + (b - a) * t


def rounded_rect_sdf(px, py, cx, cy, hw, hh, r):
    """到圆角矩形边缘的有符号距离（负=内部）。"""
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ox, oy) + min(max(qx, qy), 0.0) - r


def seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def in_triangle(px, py, a, b, c):
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1 = sign((px, py), a, b)
    d2 = sign((px, py), b, c)
    d3 = sign((px, py), c, a)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def render(s):
    S = s * SS
    u = S / 64.0
    pad = 3.0 * u
    r = 14.0 * u
    cx = cy = S / 2.0
    img = bytearray(S * S * 4)

    # 相框几何（64 设计坐标）
    fx0, fy0, fx1, fy1 = 10.0, 13.0, 54.0, 44.0
    frame_r = 3.2
    frame_w = 2.6

    sun_c = (38.5, 23.5)
    sun_r = 3.4
    far_mtn = [(22.0, 40.0), (31.5, 24.0), (41.0, 40.0)]
    near_mtn = [(12.5, 40.0), (21.0, 29.0), (29.5, 40.0)]
    # 山体只画在相框内
    inner_top, inner_bottom = fy0 + frame_w, fy1 - frame_w
    badge_c = (48.0, 46.0)
    badge_r = 7.0
    check = [(44.6, 46.0), (47.0, 48.4), (51.4, 43.4)]

    for j in range(S):
        py = j + 0.5
        for i in range(S):
            px = i + 0.5
            d_bg = rounded_rect_sdf(px, py, cx, cy, S / 2.0 - pad, S / 2.0 - pad, r)
            if d_bg > 0.75:
                continue
            a = 255 if d_bg < -0.75 else int(255 * (0.5 - d_bg / 1.5))
            a = max(0, min(255, a))
            t = ((px / S) + (py / S)) / 2.0
            cr = lerp(C1[0], C2[0], t)
            cg = lerp(C1[1], C2[1], t)
            cb = lerp(C1[2], C2[2], t)

            # 相框：外圆角矩形减去内圆角矩形
            d_out = rounded_rect_sdf(px, py, (fx0 + fx1) / 2 * u,
                                     (fy0 + fy1) / 2 * u,
                                     (fx1 - fx0) / 2 * u,
                                     (fy1 - fy0) / 2 * u, frame_r * u)
            d_in = rounded_rect_sdf(px, py, (fx0 + fx1) / 2 * u,
                                    (fy0 + fy1) / 2 * u,
                                    (fx1 - fx0) / 2 * u - frame_w * u,
                                    (fy1 - fy0) / 2 * u - frame_w * u,
                                    max(0.4, frame_r - frame_w * 0.7) * u)
            if d_out <= 0 and d_in > 0:
                cr = cg = cb = 255.0

            # 太阳
            if math.hypot(px - sun_c[0] * u, py - sun_c[1] * u) <= sun_r * u:
                cr, cg, cb = SUN

            # 山峦（白色，带上下边界裁切）
            if inner_top * u <= py <= inner_bottom * u:
                if in_triangle(px, py, *[(x * u, y * u) for (x, y) in far_mtn]):
                    cr = cg = cb = WHITE[0] * 0.86
                if in_triangle(px, py, *[(x * u, y * u) for (x, y) in near_mtn]):
                    cr = cg = cb = WHITE[0]

            # 绿色对勾徽标
            bd = math.hypot(px - badge_c[0] * u, py - badge_c[1] * u)
            if bd <= badge_r * u:
                cr, cg, cb = GREEN
                for k in range(len(check) - 1):
                    if seg_dist(px, py,
                                check[k][0] * u, check[k][1] * u,
                                check[k + 1][0] * u, check[k + 1][1] * u) <= 1.75 * u:
                        cr = cg = cb = 255.0
            elif bd <= (badge_r + 1.4) * u:
                cr = cg = cb = 255.0

            idx = (j * S + i) * 4
            img[idx] = int(cr)
            img[idx + 1] = int(cg)
            img[idx + 2] = int(cb)
            img[idx + 3] = a

    # 盒式降采样
    out = bytearray(s * s * 4)
    for j in range(s):
        for i in range(s):
            r_ = g_ = b_ = a_ = 0
            for dj in range(SS):
                for di in range(SS):
                    idx = ((j * SS + dj) * S + (i * SS + di)) * 4
                    r_ += img[idx]
                    g_ += img[idx + 1]
                    b_ += img[idx + 2]
                    a_ += img[idx + 3]
            n = SS * SS
            idx = (j * s + i) * 4
            out[idx] = r_ // n
            out[idx + 1] = g_ // n
            out[idx + 2] = b_ // n
            out[idx + 3] = a_ // n
    return bytes(out)


def png_chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path, w, h, rgba):
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    data = (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(raw, 9))
            + png_chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    for s in SIZES:
        p = os.path.join(outdir, "wallpaper_%d.png" % s)
        write_png(p, s, s, render(s))
        print("wrote %s (%d bytes)" % (p, os.path.getsize(p)))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
