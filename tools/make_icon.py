"""Generate assets/ptkit.ico (build-time only; needs Pillow).

A rounded tile with a small three-node network: drawn large and downsampled
so each icon size stays crisp.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "assets" / "ptkit.ico"
S = 1024  # master canvas


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, S - 40, S - 40), radius=200, fill=(16, 63, 115, 255))
    d.rounded_rectangle((40, 40, S - 40, S // 2), radius=200, fill=(22, 86, 150, 255))
    d.rectangle((40, S // 2 - 200, S - 40, S // 2), fill=(22, 86, 150, 255))
    nodes = [(S // 2, 300), (290, 700), (734, 700)]
    for a in nodes:
        for b in nodes:
            if a < b:
                d.line((a, b), fill=(140, 220, 255, 255), width=46)
    for x, y in nodes:
        r = 105
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, 255))
        d.ellipse((x - r + 34, y - r + 34, x + r - 34, y + r - 34), fill=(31, 168, 230, 255))
    return img


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    draw().save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
                            (256, 256)])
    print(f"wrote {OUT}")
