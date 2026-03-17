"""Quick deep color scan of all uploads to find Plasma-PQ."""
from PIL import Image
from pathlib import Path

uploads = Path(__file__).parent / "uploads"

for f in sorted(uploads.glob("*.png")):
    img = Image.open(f).convert("RGB")
    w, h = img.size
    red = blue = black = 0
    total = 0
    for x in range(0, w, 3):
        for y in range(0, h, 3):
            r, g, b = img.getpixel((x, y))
            total += 1
            if r > 150 and g < 60 and b < 60: red += 1
            if b > 150 and r < 60 and g < 60: blue += 1
    print(f"{f.name}: {w}x{h} red={red}({red/total*100:.2f}%) blue={blue}({blue/total*100:.2f}%)")
