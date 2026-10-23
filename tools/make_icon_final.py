# -*- coding: utf-8 -*-
"""爱快DNS反代 图标 v2（闪电 + DNS，干净扁平 + 轻微立体）

修正 v1 问题：描边发黑、穿透色块像木棍、文字投影成方块。
"""
import os, math
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageChops

S = 1024
SS = 4
OUT = os.path.join(os.path.dirname(__file__), "icon-final")
os.makedirs(OUT, exist_ok=True)

FONTS = [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"]


def get_font(sz):
    for f in FONTS:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, sz)
            except Exception:
                pass
    return ImageFont.load_default()


def vgrad(n, stops, mask):
    """纵向渐变填充到 mask 区域"""
    g = Image.new("RGB", (1, n))
    gp = g.load()
    for y in range(n):
        t = y / (n - 1)
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / max(1e-9, (t1 - t0))
                gp[0, y] = tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
                break
        else:
            gp[0, y] = stops[-1][1]
    g = g.resize((n, n), Image.BILINEAR).convert("RGBA")
    return Image.composite(g, Image.new("RGBA", (n, n), (0, 0, 0, 0)), mask)


def squircle(size, ratio=0.235, exp=3.6, blur=1.6):
    ss = 3
    W = size * ss
    m = Image.new("L", (W, W), 0)
    px = m.load()
    c = W / 2.0
    a = W / 2.0
    for y in range(W):
        ny = abs((y - c) / a)
        for x in range(W):
            nx = abs((x - c) / a)
            px[x, y] = 255 if (nx ** exp + ny ** exp) <= 1.0 else 0
    m = m.filter(ImageFilter.GaussianBlur(ss * blur * 0.6))
    return m.resize((size, size), Image.LANCZOS)


def build():
    n = S * SS

    # ===== 背景：径向深蓝 =====
    bg = Image.new("RGB", (n, n), (14, 32, 118))
    px = bg.load()
    cX, cY = 0.40 * n, 0.24 * n
    maxd = math.hypot(n, n) * 0.80
    for y in range(n):
        for x in range(n):
            d = min(1.0, math.hypot(x - cX, y - cY) / maxd) ** 1.25
            # (#4A92FA) → (#0E207692)
            px[x, y] = (int(74 + (14 - 74) * d),
                        int(146 + (32 - 146) * d),
                        int(250 + (118 - 250) * d))
    # 左上柔光
    g = Image.new("RGB", (n, n), (0, 0, 0))
    ImageDraw.Draw(g).ellipse([-.40 * n, -.45 * n, .68 * n, .52 * n], fill=(140, 196, 255))
    g = g.filter(ImageFilter.GaussianBlur(n * .11))
    bg = Image.blend(bg, Image.blend(bg, g, .40), .40)
    base = bg.convert("RGBA")

    # ===== 闪电几何（统一 6 点）=====
    # 底端 x≈0.452，落在 D 与 N 的间隙（文字带 y 0.73~0.90），穿过时不压笔画
    P = [(0.560, 0.108), (0.396, 0.505), (0.504, 0.505),
         (0.456, 0.742), (0.638, 0.436), (0.524, 0.436)]
    pts = [(x * n, y * n) for x, y in P]

    bolt_mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(bolt_mask).polygon(pts, fill=255)

    # 外光晕
    halo = bolt_mask.filter(ImageFilter.GaussianBlur(n * .040))
    halo_img = Image.new("RGBA", (n, n), (255, 205, 105, 0))
    halo_img.putalpha(halo.point(lambda v: int(v * 0.50)))
    base = Image.alpha_composite(base, halo_img)

    # 紧贴光（下采样膨胀，快且圆滑）
    small = bolt_mask.resize((n // 6, n // 6), Image.LANCZOS)
    small = small.filter(ImageFilter.MaxFilter(7))
    tight = small.resize((n, n), Image.LANCZOS).filter(ImageFilter.GaussianBlur(n * .010))
    ti = Image.new("RGBA", (n, n), (255, 236, 165, 0))
    ti.putalpha(tight.point(lambda v: int(v * 0.34)))
    base = Image.alpha_composite(base, ti)

    # 白色描边：把多边形沿质心向外放大，形成一圈均匀白边（strokewidth）
    cx0 = sum(p[0] for p in pts) / len(pts)
    cy0 = sum(p[1] for p in pts) / len(pts)
    grow = 1.062
    big_pts = [(cx0 + (x - cx0) * grow, cy0 + (y - cy0) * grow) for x, y in pts]

    def clip_below(shape, y_start, y_end):
        """y_start 以上保留 100%，y_start~y_end 线性衰减，y_end 以下归零"""
        m = Image.new("L", (n, n), 0)
        md = ImageDraw.Draw(m)
        md.rectangle([0, 0, n, int(y_start)], fill=255)
        for yy in range(int(y_start), int(y_end)):
            md.line([(0, yy), (n, yy)], fill=int(255 * (1 - (yy - y_start) / (y_end - y_start))))
        return Image.composite(shape, Image.new("L", (n, n), 0), m)

    # 白色描边 = 放大形 - 原形（只留外圈），再在闪电内折线处补两笔
    stroke = Image.new("L", (n, n), 0)
    ImageDraw.Draw(stroke).polygon(big_pts, fill=255)
    inner_neg = Image.new("L", (n, n), 0)
    ImageDraw.Draw(inner_neg).polygon(pts, fill=255)
    # 相减：stroke 有 且 inner 无
    ring = ImageChops.subtract(stroke, inner_neg)
    # 内折线（闪电中轴的两条凹口）也要白边
    seam = Image.new("L", (n, n), 0)
    sd = ImageDraw.Draw(seam)
    sd.line([pts[1], pts[2]], fill=255, width=max(2, int(n * .006)))
    sd.line([pts[4], pts[5]], fill=255, width=max(2, int(n * .006)))
    ring = ImageChops.lighter(ring, seam)
    ring = clip_below(ring, 0.560 * n, 0.700 * n)
    ring = ring.filter(ImageFilter.GaussianBlur(n * .0028))
    oi = Image.new("RGBA", (n, n), (255, 255, 255, 255))
    oi.putalpha(ring)
    base = Image.alpha_composite(base, oi)

    # 闪电金色渐变
    bolt = vgrad(n, [(0.0, (255, 252, 232)), (0.28, (255, 230, 140)),
                     (0.60, (255, 208, 92)), (1.0, (250, 182, 48))], bolt_mask)
    # 左上高光条
    hi = Image.new("L", (n, n), 0)
    ImageDraw.Draw(hi).polygon([
        (0.548 * n, 0.150 * n), (0.402 * n, 0.503 * n),
        (0.455 * n, 0.503 * n), (0.556 * n, 0.225 * n)], fill=255)
    hi = hi.filter(ImageFilter.GaussianBlur(n * .008))
    hi_layer = Image.new("RGBA", (n, n), (255, 255, 255, 0))
    hi_layer.putalpha(hi.point(lambda v: int(v * 0.55)))
    bolt = Image.alpha_composite(bolt, hi_layer)
    bolt.putalpha(Image.composite(bolt.split()[3], Image.new("L", (n, n), 0), bolt_mask))
    base = Image.alpha_composite(base, bolt)

    # ===== DNS 文字 =====
    word = "DNS"
    fs = int(n * 0.225)
    f = get_font(fs)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    spacing = int(n * 0.014)
    widths = [probe.textlength(ch, font=f) for ch in word]
    total = sum(widths) + spacing * (len(word) - 1)
    bb = f.getbbox("H")
    caph = bb[3] - bb[1]
    cy = n * 0.800
    top = cy - caph / 2 - bb[1]

    txt = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt)
    x = n / 2 - total / 2
    for ch, w in zip(word, widths):
        td.text((x, top), ch, font=f, fill=(255, 255, 255, 255))
        x += w + spacing

    # 只给文字加轻微下投影（用 alpha 做，避免方块）
    sh = txt.filter(ImageFilter.GaussianBlur(n * .007))
    sh.putalpha(sh.split()[3].point(lambda v: int(v * 0.35)))
    sh = sh.transform((n, n), Image.AFFINE, (1, 0, 0, 0, 1, -int(n * .006)))
    # 文字层画在闪电之后（闪电在文字下方），文字完整性不受闪电干扰
    base = Image.alpha_composite(base, sh)
    base = Image.alpha_composite(base, txt)

    return base


def main():
    final = build().resize((S, S), Image.LANCZOS)
    final.putalpha(squircle(S))
    final.save(os.path.join(OUT, "icon-1024.png"))

    for fn, sz in {"icon-512.png": 512, "icon-384.png": 384, "icon-256.png": 256,
                   "icon-192.png": 192, "icon-144.png": 144, "icon-96.png": 96,
                   "icon-72.png": 72, "icon-48.png": 48, "favicon-32.png": 32,
                   "apple-touch-icon.png": 180}.items():
        final.resize((sz, sz), Image.LANCZOS).save(os.path.join(OUT, fn))

    msz = 512
    mask = Image.new("RGBA", (msz, msz), (30, 68, 176, 255))
    inner = final.crop((int(S * .11), int(S * .11), int(S * .89), int(S * .89)))
    inner = inner.resize((int(msz * .78), int(msz * .78)), Image.LANCZOS)
    mask.paste(inner, ((msz - inner.width) // 2, (msz - inner.height) // 2), inner)
    mask.save(os.path.join(OUT, "icon-512-maskable.png"))

    final.resize((256, 256), Image.LANCZOS).save(
        os.path.join(OUT, "favicon.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    prev = Image.new("RGB", (800, 330), (16, 20, 30))
    pd = ImageDraw.Draw(prev)
    xoff = 26
    for sz in (180, 120, 80, 48, 32, 16):
        ic = final.resize((sz, sz), Image.LANCZOS)
        prev.paste(ic, (xoff, 90 + (180 - sz) // 2), ic)
        pd.text((xoff, 285), "%dpx" % sz, fill=(140, 158, 185))
        xoff += sz + 30
    big = final.resize((250, 250), Image.LANCZOS)
    prev.paste(big, (530, 30), big)
    pd.text((600, 295), "250px", fill=(140, 158, 185))
    prev.save(os.path.join(OUT, "_preview.png"))
    print("done")


if __name__ == "__main__":
    main()
