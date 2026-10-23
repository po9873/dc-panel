# -*- coding: utf-8 -*-
"""爱快DNS反代 图标候选（1024 母版 + 192 预览 + 对比图）"""
import os, math
from PIL import Image, ImageDraw, ImageFilter

S = 1024
OUT = os.path.join(os.path.dirname(__file__), "icon-cand")
os.makedirs(OUT, exist_ok=True)


def diag_grad(size, c1, c2):
    img = Image.new("RGB", (size, size), c1)
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return img


def radial_bg(size, c_in, c_out, cx=0.5, cy=0.35):
    img = Image.new("RGB", (size, size), c_out)
    px = img.load()
    cX, cY = cx * size, cy * size
    maxd = math.hypot(size, size) * 0.75
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - cX, y - cY) / maxd
            t = min(1.0, d)
            px[x, y] = tuple(int(c_in[i] + (c_out[i] - c_in[i]) * t) for i in range(3))
    return img


def mask_round(size, radius):
    ss = 4
    m = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size * ss, size * ss], radius=radius * ss, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def save(img, name):
    img = img.convert("RGBA")
    img.putalpha(mask_round(S, int(S * 0.225)))
    img.save(os.path.join(OUT, f"{name}-1024.png"))
    img.resize((192, 192), Image.LANCZOS).save(os.path.join(OUT, f"{name}-192.png"))


# ---- E：地球 + DNS 文字（DNS 主题）----
def cand_e():
    n = S
    bg = radial_bg(n, (56, 120, 240), (10, 28, 78))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy, r = n / 2, n * 0.44, n * 0.27
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 250), width=int(n * 0.030))
    # 经线纬线
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-90, end=90, fill=(160, 200, 255, 200), width=int(n * 0.008))  # 竖线占位
    d.ellipse([cx - r * 0.45, cy - r, cx + r * 0.45, cy + r], outline=(160, 200, 255, 220), width=int(n * 0.008))
    d.line([cx - r, cy, cx + r, cy], fill=(160, 200, 255, 220), width=int(n * 0.008))
    d.arc([cx - r, cy - r * 0.45, cx + r, cy + r * 0.45], start=0, end=180, fill=(160, 200, 255, 160), width=int(n * 0.008))
    d.arc([cx - r, cy - r * 0.45, cx + r, cy + r * 0.45], start=180, end=360, fill=(160, 200, 255, 160), width=int(n * 0.008))
    # DNS 文字条
    ty = cy + r + n * 0.045
    tw, th = n * 0.46, n * 0.135
    d.rounded_rectangle([cx - tw / 2, ty, cx + tw / 2, ty + th], radius=th / 2, fill=(255, 255, 255, 255))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---- F：反向双箭头（反代转发主题）----
def cand_f():
    n = S
    bg = diag_grad(n, (64, 108, 245), (24, 40, 140))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    # 上箭头（右→左上） 下箭头（左→右下） 表示双向代理
    t = n * 0.075
    ah = n * 0.16
    # 右上箭头
    x1, y1 = n * 0.70, n * 0.32
    d.line([n * 0.30, n * 0.42, x1, y1], fill=(255, 255, 255, 255), width=int(t))
    d.polygon([(x1 - ah * .9, y1 + ah * .35), (x1 + ah * .1, y1 - ah * .75), (x1 + ah * .55, y1 + ah * .6)], fill=(255, 255, 255, 255))
    # 左下箭头
    x2, y2 = n * 0.30, n * 0.68
    d.line([n * 0.70, n * 0.58, x2, y2], fill=(150, 200, 255, 255), width=int(t))
    d.polygon([(x2 + ah * .9, y2 - ah * .35), (x2 - ah * .1, y2 + ah * .75), (x2 - ah * .55, y2 - ah * .6)], fill=(150, 200, 255, 255))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---- G：盾牌 + 网络节点（安全域名转发）----
def cand_g():
    n = S
    bg = radial_bg(n, (70, 130, 250), (14, 24, 70), cx=0.5, cy=0.3)
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx = n / 2
    top = n * 0.20
    bw = n * 0.31
    bot = n * 0.82
    # 盾形
    pts = [(cx - bw, top), (cx + bw, top), (cx + bw, n * 0.52), (cx, bot), (cx - bw, n * 0.52)]
    d.polygon(pts, fill=(255, 255, 255, 255))
    # 盾内节点：一点发散三线
    d.ellipse([cx - n * .030, n * 0.315 - n * .030, cx + n * .030, n * 0.315 + n * .030], fill=(40, 90, 220, 255))
    for ang, ln in [(200, n * .13), (270, n * .14), (340, n * .13)]:
        ex = cx + ln * math.cos(math.radians(ang))
        ey = n * 0.315 + ln * math.sin(math.radians(ang))
        d.line([(cx, n * 0.315), (ex, ey)], fill=(40, 90, 220, 235), width=int(n * 0.022))
        d.ellipse([ex - n * .026, ey - n * .026, ex + n * .026, ey + n * .026], fill=(40, 90, 220, 255))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---- H：云 + 域名箭头（穿透/解析）----
def cand_h():
    n = S
    bg = diag_grad(n, (52, 150, 235), (16, 52, 150))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    # 云
    cy, cr = n * 0.38, n * 0.115
    d.ellipse([n * .27, cy - cr, n * .27 + cr * 2, cy + cr], fill=(255, 255, 255, 255))
    d.ellipse([n * .40, cy - cr * 1.25, n * .40 + cr * 2.1, cy + cr * 1.25], fill=(255, 255, 255, 255))
    d.ellipse([n * .55, cy - cr, n * .55 + cr * 1.9, cy + cr], fill=(255, 255, 255, 255))
    d.rounded_rectangle([n * .285, cy, n * .70, cy + cr * 0.95], radius=cr * 0.7, fill=(255, 255, 255, 255))
    # 向下三箭头
    for fx in (0.38, 0.50, 0.62):
        x = n * fx
        y0, y1 = n * 0.60, n * 0.76
        d.line([(x, y0), (x, y1)], fill=(255, 255, 255, 255), width=int(n * 0.030))
        d.polygon([(x - n * .045, y1 - n * .055), (x + n * .045, y1 - n * .055), (x, y1 + n * .035)], fill=(255, 255, 255, 255))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


for name, fn in [("E_globe", cand_e), ("F_arrows", cand_f), ("G_shield", cand_g), ("H_cloud", cand_h)]:
    save(fn(), name)
    print("ok", name)

cell = 300
sheet = Image.new("RGB", (cell * 4, cell + 120), (18, 22, 32))
sd = ImageDraw.Draw(sheet)
for i, name in enumerate(["E_globe", "F_arrows", "G_shield", "H_cloud"]):
    im = Image.open(os.path.join(OUT, f"{name}-1024.png")).resize((cell - 60, cell - 60), Image.LANCZOS)
    sheet.paste(im, (i * cell + 30, 20), im)
    sm = Image.open(os.path.join(OUT, f"{name}-192.png")).resize((56, 56), Image.LANCZOS)
    sheet.paste(sm, (i * cell + 30, cell + 10), sm)
    sd.text((i * cell + 96, cell + 28), name, fill=(200, 210, 230))
sheet.save(os.path.join(OUT, "_compare2.png"))
print("done")
