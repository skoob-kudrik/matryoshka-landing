#!/usr/bin/env python3
# Генерирует og-image.png (1200×630) — картинку-превью для мессенджеров/соцсетей.
# Брендовая карточка: фиолетовый градиент, логотип на белой плашке, название и тезис.
# Без volatile-цифр (KPI) — превью кешируется надолго, стейл-числа в финпродукте недопустимы.
#
# Запуск:  python3 make_og.py        # → og-image.png
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "ассеты"
OUT = HERE / "og-image.png"

W, H = 1200, 630
PURPLE_TOP = (68, 43, 119)      # --purple  #442B77
PURPLE_BOT = (98, 73, 151)      # --purple-mid #624997
ORANGE = (244, 156, 45)         # --orange #F49C2D
INK = (31, 35, 45)              # --ink
GRAY = (87, 89, 91)             # --gray
LAV = (214, 205, 240)           # светлая лаванда для тезиса на фиолетовом

TITLE = "ОПИФ «Матрёшка а-ля Рус»"
TAG = "Активно управляемый фонд российских акций"
DOMAIN = "matreshkarusfund.ru"

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"


def font(path, size):
    return ImageFont.truetype(path, size)


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def main():
    img = Image.new("RGB", (W, H), PURPLE_TOP)

    # вертикальный градиент
    top, bot = PURPLE_TOP, PURPLE_BOT
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b)

    d = ImageDraw.Draw(img)

    M = 90  # поля

    # ── белая плашка с логотипом (слева сверху) ──
    plate = 190
    plate_xy = (M, M)
    d.rounded_rectangle(
        [plate_xy[0], plate_xy[1], plate_xy[0] + plate, plate_xy[1] + plate],
        radius=32, fill=(255, 255, 255),
    )
    logo = Image.open(ASSETS / "znak.png").convert("RGBA")
    pad = 28
    box = plate - pad * 2
    lw, lh = logo.size
    scale = min(box / lw, box / lh)
    logo = logo.resize((int(lw * scale), int(lh * scale)), Image.LANCZOS)
    lx = plate_xy[0] + (plate - logo.width) // 2
    ly = plate_xy[1] + (plate - logo.height) // 2
    img.paste(logo, (lx, ly), logo)

    # ── заголовок ──
    max_w = W - M * 2
    ty = plate_xy[1] + plate + 46
    f_title = font(FONT_BOLD, 62)
    for line in wrap(d, TITLE, f_title, max_w):
        d.text((M, ty), line, font=f_title, fill=(255, 255, 255))
        ty += 74

    # ── тезис ──
    ty += 10
    f_tag = font(FONT_REG, 34)
    for line in wrap(d, TAG, f_tag, max_w):
        d.text((M, ty), line, font=f_tag, fill=LAV)
        ty += 46

    # ── оранжевая акцент-линия + домен снизу ──
    d.rectangle([M, H - 70, M + 64, H - 66], fill=ORANGE)
    f_dom = font(FONT_BOLD, 26)
    d.text((M + 84, H - 82), DOMAIN, font=f_dom, fill=(255, 255, 255))

    img.save(OUT, "PNG", optimize=True)
    print(f"✓ {OUT.name}  {img.size[0]}×{img.size[1]}  {OUT.stat().st_size // 1024} КБ")


if __name__ == "__main__":
    main()
