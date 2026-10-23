# -*- coding: utf-8 -*-
"""爱快DNS反代 图标 v2：带真实文字字形"""
import os, math
from PIL import Image, ImageDraw, ImageFilter, ImageFont

S = 1024
OUT = os.path.join(os.path.dirname(__file__), "icon-cand")
os.makedirs(OUT, exist_ok=True)

FONTS = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def font(sz):
    for f in FONTS:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, sz)
            except Exception:
                continue
    return ImageFont.load_default()


def diag_grad(size, c1, c2):
    img = Image.new("RGB", (size, size), c1)
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return img


def radial_bg(size, c_in, c_out, cx=0.5, cy=0.3):
    img = Image.new("RGB", (size, size), c_out)
    px = img.load()
    cX, cY = cx * size, cy * size
    maxd = math.hypot(size, size) * 0.8
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - cX, y - cY) / maxd
            px[x, y] = tuple(int(c_in[i] + (c_out[i] - c_in[i]) * min(1.0, d)) for i in range(3))
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


def ctext(lay, cx, cy, text, sz, fill, spacing=0):
    """居中写字"""
    d = ImageDraw.Draw(lay)
    f = font(sz)
    if spacing:
        # 手动字间距
        widths = [d.textlength(ch, font=f) for ch in text]
        total = sum(widths) + spacing * (len(text) - 1)
        x = cx - total / 2
        bbox = f.getbbox("H")
        h = bbox[3] - bbox[1]
        for ch, w in zip(text, widths):
            d.text((x, cy - h / 2 - bbox[1]), ch, font=f, fill=fill)
            x += w + spacing
    else:
        bbox = d.textbbox((0, 0), text, font=f)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=f, fill=fill)


# ---- I：地球 + DNS 弧形文字（正式徽章）----
def cand_i():
    n = S
    bg = radial_bg(n, (60, 124, 244), (10, 26, 72))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy, r = n / 2, n * 0.415, n * 0.245
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 252), width=int(n * 0.032))
    # 经线（两条椭圆）+ 纬线
    d.ellipse([cx - r * 0.46, cy - r, cx + r * 0.46, cy + r], outline=(170, 205, 255, 210), width=int(n * 0.009))
    d.line([cx - r, cy, cx + r, cy], fill=(170, 205, 255, 210), width=int(n * 0.009))
    for k in (-0.52, 0.52):
        hy = cy + r * k
        hw = r * math.sqrt(max(0.0, 1 - k * k))
        d.line([cx - hw, hy, cx + hw, hy], fill=(170, 205, 255, 175), width=int(n * 0.009))
    # DNS 大字
    ctext(lay, cx, n * 0.775, "DNS", int(n * 0.175), (255, 255, 255, 255), spacing=int(n * 0.012))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---- J：DNS 大字 + 双向反代箭头（最直白）----
def cand_j():
    n = S
    bg = diag_grad(n, (76, 122, 248), (22, 38, 132))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    ctext(lay, n / 2, n * 0.615, "DNS", int(n * 0.30), (255, 255, 255, 255), spacing=int(n * 0.010))
    # 上方双向箭头（左→右 / 右→左）
    y1, y2 = n * 0.285, n * 0.395
    xa, xb = n * 0.285, n * 0.715
    hr = n * 0.052
    d.line([(xa + hr, y1), (xb - hr, y1)], fill=(190, 218, 255, 255), width=int(n * 0.026))
    d.polygon([(xb, y1), (xb - hr * 1.5, y1 - hr), (xb - hr * 1.5, y1 + hr)], fill=(190, 218, 255, 255))
    d.line([(xb - hr, y2), (xa + hr, y2)], fill=(255, 255, 255, 255), width=int(n * 0.026))
    d.polygon([(xa, y2), (xa + hr * 1.5, y2 - hr), (xa + hr * 1.5, y2 + hr)], fill=(255, 255, 255, 255))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---- K：DNS + 闪电（加速/直连）----
def cand_k():
    n = S
    bg = radial_bg(n, (72, 132, 250), (12, 30, 86), cx=0.45, cy=0.3)
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    # 闪电
    pts = [(n * .565, n * .165), (n * .395, n * .505), (n * .515, n * .505),
           (n * .455, n * .835), (n * .645, n * .455), (n * .520, n * .455)]
    d.polygon(pts, fill=(255, 226, 120, 255))
    # 闪电外壳描边感（白）
    d.line(pts + [pts[0]], fill=(255, 255, 255, 255), width=int(n * 0.018), joint="curve")
    ctext(lay, n / 2, n * 0.80, "DNS", int(n * 0.145), (255, 255, 255, 240), spacing=int(n * 0.010))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


# ---- L：爱快风格橙 + DNS 节点（区分度高）----
def cand_l():
    n = S
    bg = diag_grad(n, (255, 148, 62), (216, 82, 24))
    lay = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy = n / 2, n * 0.44
    # 中心节点 + 四向连线
    for ang in (0, 90, 180, 270):
        ex = cx + n * 0.235 * math.cos(math.radians(ang))
        ey = cy + n * 0.235 * math.sin(math.radians(ang))
        d.line([(cx, cy), (ex, ey)], fill=(255, 255, 255, 210), width=int(n * 0.022))
        d.ellipse([ex - n * .042, ey - n * .042, ex + n * .042, ey + n * .042], outline=(255, 255, 255, 255), width=int(n * 0.020))
    d.ellipse([cx - n * .075, cy - n * .075, cx + n * .075, cy + n * .075], fill=(255, 255, 255, 255))
    ctext(lay, cx, n * 0.80, "DNS", int(n * 0.155), (255, 255, 255, 255), spacing=int(n * 0.010))
    out = Image.alpha_composite(bg.convert("RGBA"), lay)
    return out


names = ["I_globe_dns", "J_dns_arrows", "K_dns_bolt", "L_dns_node"]
for name, fn in zip(names, [cand_i, cand_j, cand_k, cand_l]):
    save(fn(), name)
    print("ok", name)

cell = 300
sheet = Image.new("RGB", (cell * 4, cell + 120), (18, 22, 32))
sd = ImageDraw.Draw(sheet)
for i, name in enumerate(names):
    im = Image.open(os.path.join(OUT, f"{name}-1024.png")).resize((cell - 60, cell - 60), Image.LANCZOS)
    sheet.paste(im, (i * cell + 30, 20), im)
    sm = Image.open(os.path.join(OUT, f"{name}-192.png")).resize((56, 56), Image.LANCZOS)
    sheet.paste(sm, (i * cell + 30, cell + 10), sm)
    sd.text((i * cell + 96, cell + 28), name, fill=(200, 210, 230))
sheet.save(os.path.join(OUT, "_compare3.png"))
print("done")
