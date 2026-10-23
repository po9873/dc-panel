# -*- coding: utf-8 -*-
"""生成 DC Panel 图标候选（1024px 母版 + 192 预览），纯 PIL 矢量式绘制"""
import os, math
from PIL import Image, ImageDraw, ImageFilter

S = 1024
OUT = os.path.join(os.path.dirname(__file__), "icon-cand")
os.makedirs(OUT, exist_ok=True)


def rounded_mask(size, radius, box=None, ss=4):
    box = box or (0, 0, size, size)
    m = Image.new("L", (size * ss, size * ss), 0)
    d = ImageDraw.Draw(m)
    b = [c * ss for c in box]
    d.rounded_rectangle(b, radius=radius * ss, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def radial_bg(size, c_in, c_out, center=(0.32, 0.22)):
    img = Image.new("RGB", (size, size), c_out)
    px = img.load()
    cx, cy = center[0] * size, center[1] * size
    maxd = math.hypot(size, size)
    for y in range(size):
        for x in range(0, size):
            d = math.hypot(x - cx, y - cy) / maxd
            t = min(1.0, d * 1.35)
            px[x, y] = tuple(int(c_in[i] + (c_out[i] - c_in[i]) * t) for i in range(3))
    return img


def diag_grad(size, c1, c2):
    img = Image.new("RGB", (size, size), c1)
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return img


def sup(draw_fn, ss=4):
    """超采样绘制"""
    return ss


def make_layers(size, ss=4):
    return Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))


def finish(img_ss, size):
    return img_ss.resize((size, size), Image.LANCZOS)


# ---------------- 候选 A：DC 字母 + 立方体（容器感） ----------------
def cand_a():
    ss = 3
    n = S
    bg = diag_grad(n, (36, 88, 214), (18, 46, 132))
    # 加一点光晕
    glow = Image.new("RGB", (n, n), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-n * 0.2, -n * 0.45, n * 0.85, n * 0.55], fill=(70, 140, 255))
    glow = glow.filter(ImageFilter.GaussianBlur(n // 6))
    bg = Image.blend(bg, Image.blend(bg, glow, 0.55), 0.5)

    lay = make_layers(n, ss)
    d = ImageDraw.Draw(lay)
    k = ss
    # 立方体（isometric）
    cx, cy = n / 2, n * 0.52
    w, h = n * 0.30, n * 0.17
    top = [(cx, cy - h * 1.05), (cx + w, cy - h * 0.42), (cx, cy + h * 0.20), (cx - w, cy - h * 0.42)]
    left = [(cx - w, cy - h * 0.42), (cx, cy + h * 0.20), (cx, cy + h * 1.55), (cx - w, cy + h * 0.92)]
    right = [(cx + w, cy - h * 0.42), (cx, cy + h * 0.20), (cx, cy + h * 1.55), (cx + w, cy + h * 0.92)]
    d.polygon([tuple(p * k for p in q) for q in top], fill=(255, 255, 255, 255))
    d.polygon([tuple(p * k for p in q) for q in left], fill=(206, 226, 255, 255))
    d.polygon([tuple(p * k for p in q) for q in right], fill=(150, 185, 250, 255))
    # 分成三格：横线 + 竖线（容器分层感）
    lay = finish(lay, n)
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---------------- 候选 B：圆环 + DC 字（简洁徽章） ----------------
def cand_b():
    n = S
    bg = radial_bg(n, (58, 116, 236), (12, 26, 74))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    # 外圆环渐变(用两层圆环模拟)
    r_out = n * 0.345
    cx = cy = n / 2
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], outline=(255, 255, 255, 235), width=int(n * 0.028))
    # 内圈细环
    r2 = n * 0.29
    d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], outline=(140, 190, 255, 150), width=int(n * 0.010))
    # DC 文字用几何笔画绘制
    t = n * 0.038  # 笔画粗
    # D
    dx0 = cx - n * 0.155
    dy0 = cy - n * 0.105
    dy1 = cy + n * 0.105
    d.rounded_rectangle([dx0, dy0, dx0 + t, dy1], radius=t / 2, fill=(255, 255, 255, 255))
    d.arc([dx0, dy0, dx0 + n * 0.20, dy1], start=-90, end=90, fill=(255, 255, 255, 255), width=int(t))
    # C
    cx0 = cx + n * 0.035
    d.arc([cx0, dy0, cx0 + n * 0.215, dy1], start=48, end=312, fill=(255, 255, 255, 255), width=int(t))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---------------- 候选 C：立方体轮廓 + 星点（科技感） ----------------
def cand_c():
    n = S
    bg = Image.new("RGB", (n, n), (10, 16, 32))
    # 深色 + 紫蓝光斑
    glow = Image.new("RGB", (n, n), (10, 16, 32))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-n * .25, -n * .3, n * .75, n * .7], fill=(56, 92, 255))
    gd.ellipse([n * .3, n * .35, n * 1.3, n * 1.35], fill=(140, 70, 230))
    glow = glow.filter(ImageFilter.GaussianBlur(n // 5))
    bg = Image.blend(bg, glow, 0.65)

    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy = n / 2, n / 2
    s = n * 0.30
    # 六边形（容器/节点）
    pts = [(cx + s * math.cos(math.radians(60 * i - 90)), cy + s * math.sin(math.radians(60 * i - 90))) for i in range(6)]
    d.polygon(pts, outline=(255, 255, 255, 245), width=int(n * 0.030))
    # 内部三向连线（节点网络）
    for i in range(6):
        d.line([ (cx, cy), pts[i] ], fill=(130, 180, 255, 120), width=int(n * 0.010))
    d.ellipse([cx - n * .028, cy - n * .028, cx + n * .028, cy + n * .028], fill=(255, 255, 255, 255))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---------------- 候选 D：DC 字母 + 渐变（最简，字母主导） ----------------
def cand_d():
    n = S
    bg = diag_grad(n, (99, 102, 241), (37, 99, 235))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy = n / 2, n / 2
    t = n * 0.052
    # D
    dx0 = cx - n * 0.235
    dy0 = cy - n * 0.155
    dy1 = cy + n * 0.155
    d.rounded_rectangle([dx0, dy0, dx0 + t, dy1], radius=t / 2, fill=(255, 255, 255, 255))
    d.arc([dx0, dy0, dx0 + n * 0.31, dy1], start=-90, end=90, fill=(255, 255, 255, 255), width=int(t))
    # C
    cx0 = cx + n * 0.045
    d.arc([cx0, dy0, cx0 + n * 0.335, dy1], start=45, end=315, fill=(255, 255, 255, 255), width=int(t))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


for name, fn in [("A_cube", cand_a), ("B_badge", cand_b), ("C_hex", cand_c), ("D_letters", cand_d)]:
    img = fn().convert("RGBA")
    m = rounded_mask(S, int(S * 0.225))
    img.putalpha(m)
    img.save(os.path.join(OUT, f"{name}-1024.png"))
    img.resize((192, 192), Image.LANCZOS).save(os.path.join(OUT, f"{name}-192.png"))
    print("ok", name)

# 拼一张对比图（深色底 + 大图 + 192 小图）
cell = 300
sheet = Image.new("RGB", (cell * 4, cell + 120), (18, 22, 32))
sd = ImageDraw.Draw(sheet)
for i, name in enumerate(["A_cube", "B_badge", "C_hex", "D_letters"]):
    im = Image.open(os.path.join(OUT, f"{name}-1024.png")).resize((cell - 60, cell - 60), Image.LANCZOS)
    sheet.paste(im, (i * cell + 30, 20), im)
    sm = Image.open(os.path.join(OUT, f"{name}-192.png")).resize((56, 56), Image.LANCZOS)
    sheet.paste(sm, (i * cell + 30, cell + 10), sm)
    sd.text((i * cell + 96, cell + 28), name, fill=(200, 210, 230))
sheet.save(os.path.join(OUT, "_compare.png"))
print("done")
