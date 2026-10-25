"""Draws multimic.ico (run once; the .ico is committed)."""

import os

from PIL import Image, ImageDraw

S = 1024


def draw():
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # rounded tile with a soft vertical gradient
    tile = Image.new("RGBA", (S, S))
    td = ImageDraw.Draw(tile)
    for y in range(S):
        t = y / S
        td.line([(0, y), (S, y)], fill=(int(46 - 18 * t), int(48 - 20 * t),
                                        int(56 - 22 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), 210,
                                           fill=255)
    im.paste(tile, (0, 0), mask)
    d.rounded_rectangle((40, 40, S - 40, S - 40), 210,
                        outline=(255, 255, 255, 40), width=10)

    white = (250, 250, 250, 255)
    # microphone capsule, stand and base
    d.rounded_rectangle((372, 190, 612, 560), 120, fill=white)
    d.arc((290, 330, 694, 690), 0, 180, fill=white, width=44)
    d.line((492, 690, 492, 800), fill=white, width=44)
    d.rounded_rectangle((372, 790, 612, 834), 22, fill=white)
    # grille lines on the capsule
    for y in (300, 360, 420):
        d.line((420, y, 564, y), fill=(46, 48, 56, 255), width=14)
    # red record dot
    d.ellipse((650, 640, 890, 880), fill=(229, 72, 77, 255),
              outline=(28, 28, 28, 255), width=26)
    return im


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "..", "multimic.ico")
    draw().save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
    print("wrote", os.path.abspath(out))
