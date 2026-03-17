from PIL import Image
from pathlib import Path
uploads = Path(__file__).parent / "uploads"
for name in ["425fae56.png", "f5d98961.png"]:
    f = uploads / name
    if not f.exists(): continue
    img = Image.open(f).convert("RGB")
    w, h = img.size
    r_count = b_count = 0
    total = 0
    for x in range(0, w, 2):
        for y in range(0, h, 2):
            rv, g, bv = img.getpixel((x, y))
            total += 1
            if rv > 180 and g < 60 and bv < 60: r_count += 1
            if bv > 180 and rv < 60 and g < 60: b_count += 1
    print(f"{name}: {w}x{h} red={r_count}({r_count/total*100:.2f}%) blue={b_count}({b_count/total*100:.2f}%)")
