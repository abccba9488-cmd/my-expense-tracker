"""Generate PWA icons (candlestick chart design) into static/img/icons/.

Run once with: C:\\Users\\user\\anaconda3\\python.exe generate_pwa_icons.py
"""
from PIL import Image, ImageDraw

PRIMARY = (79, 124, 255)    # #4f7cff
PRIMARY2 = (58, 97, 212)    # #3a61d4
GREEN = (34, 197, 94)       # #22c55e
GREEN_DARK = (22, 163, 74)  # #16a34a

OUT_DIR = "static/img/icons"

# Candlesticks defined in a 100x100 unit space (uptrend bar chart)
CANDLES = [
    # (x, width, body_top, body_bottom, wick_top, wick_bottom)
    (8,  12, 70, 85, 64, 90),
    (26, 12, 58, 85, 52, 90),
    (44, 12, 44, 85, 37, 90),
    (62, 12, 28, 85, 21, 90),
    (80, 12, 10, 85, 4,  90),
]


def draw_candles(img, content_size, offset_x, offset_y):
    draw = ImageDraw.Draw(img)
    scale = content_size / 100.0
    for x, w, body_top, body_bottom, wick_top, wick_bottom in CANDLES:
        cx = offset_x + (x + w / 2) * scale
        x0 = offset_x + x * scale
        x1 = offset_x + (x + w) * scale
        wick_x0 = offset_x + wick_top * scale
        wick_x1 = offset_x + wick_bottom * scale
        # wick (vertical line through body)
        draw.line(
            [(cx, offset_y + wick_top * scale), (cx, offset_y + wick_bottom * scale)],
            fill=GREEN_DARK, width=max(2, int(2 * scale / 10)),
        )
        # body
        draw.rounded_rectangle(
            [x0, offset_y + body_top * scale, x1, offset_y + body_bottom * scale],
            radius=max(1, int(2 * scale / 10)),
            fill=GREEN,
        )


def make_icon(size, maskable=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if maskable:
        # full-bleed background, content within ~60% safe zone
        draw.rectangle([0, 0, size, size], fill=PRIMARY)
        content = size * 0.6
        offset = (size - content) / 2
    else:
        # rounded "squircle" background with padding
        radius = int(size * 0.22)
        draw.rounded_rectangle([0, 0, size, size], radius=radius, fill=PRIMARY)
        content = size * 0.74
        offset = (size - content) / 2

    draw_candles(img, content, offset, offset)
    return img


def main():
    import os
    os.makedirs(OUT_DIR, exist_ok=True)

    make_icon(512).save(f"{OUT_DIR}/icon-512.png")
    make_icon(192).save(f"{OUT_DIR}/icon-192.png")
    make_icon(180).save(f"{OUT_DIR}/apple-touch-icon.png")
    make_icon(512, maskable=True).save(f"{OUT_DIR}/icon-maskable-512.png")

    # favicon.ico (multi-size)
    favicon_sizes = [16, 32, 48]
    imgs = [make_icon(s) for s in favicon_sizes]
    imgs[0].save(f"{OUT_DIR}/favicon.ico", format="ICO", sizes=[(s, s) for s in favicon_sizes])

    print("done")


if __name__ == "__main__":
    main()
