"""Identify which upload is which figure by checking image dimensions."""
from PIL import Image
from pathlib import Path

uploads = Path(__file__).parent / "uploads"
for f in sorted(uploads.glob("*.png")):
    try:
        img = Image.open(f)
        w, h = img.size
        ratio = w / h
        sz = f.stat().st_size / 1024
        print(f"  {f.name:20s}  {w:5d}x{h:<5d}  ratio={ratio:.2f}  {sz:.0f}KB")
    except:
        print(f"  {f.name:20s}  ERROR")
