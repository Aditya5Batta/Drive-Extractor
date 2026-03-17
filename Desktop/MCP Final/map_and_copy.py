"""
Map uploads to examples based on image properties.
Run: python map_and_copy.py
"""
from PIL import Image
from pathlib import Path
import shutil

base = Path(__file__).parent
uploads = base / "uploads"
examples = base / "examples"

# Analyze each upload
print("\\n=== Analyzing uploads ===\\n")
infos = []
for f in sorted(uploads.glob("*.png")):
    img = Image.open(f)
    w, h = img.size
    sz = f.stat().st_size / 1024
    
    # Sample some pixels to guess content
    pixels = img.convert("RGB")
    # Check if image has color (not grayscale)
    sample_points = [(w//4, h//4), (w//2, h//2), (3*w//4, h//4), (w//2, 3*h//4)]
    has_red = False
    has_blue = False
    is_dark_bg = False
    
    for px, py in sample_points:
        r, g, b = pixels.getpixel((px, py))
        if r > 150 and g < 100 and b < 100:
            has_red = True
        if b > 150 and r < 100 and g < 100:
            has_blue = True
        if r < 40 and g < 40 and b < 40:
            is_dark_bg = True
    
    # Check corners for dark bg (localhost screenshots have dark bg)
    corner_r, corner_g, corner_b = pixels.getpixel((5, 5))
    is_screenshot = (corner_r < 30 and corner_g < 30 and corner_b < 40)
    
    guess = "unknown"
    if is_screenshot:
        guess = "LOCALHOST SCREENSHOT (skip)"
    elif has_red and has_blue and w > 400:
        guess = "Plasma-PQ (3 colored lines)"
    elif sz < 25:
        guess = "Small figure (maybe 6-series PK)"
    elif w > 1200 and not is_screenshot:
        guess = "AUC dose-response (wide)"
    
    infos.append({"path": f, "w": w, "h": h, "sz": sz, "guess": guess, 
                  "has_red": has_red, "has_blue": has_blue, "is_screenshot": is_screenshot})
    
    print(f"  {f.name:20s} {w}x{h} {sz:.0f}KB  red={has_red} blue={has_blue} screenshot={is_screenshot}")
    print(f"    -> GUESS: {guess}")
    print()

# Auto-copy based on best guesses
print("=== Auto-copying best matches ===\\n")

# AUC: largest non-screenshot wide image
auc_candidates = [i for i in infos if not i["is_screenshot"] and i["w"] > 1000]
if auc_candidates:
    best = max(auc_candidates, key=lambda x: x["w"])
    shutil.copy2(best["path"], examples / "example_auc.png")
    print(f"  example_auc.png <- {best['path'].name} ({best['w']}x{best['h']})")

# Plasma-PQ: has red AND blue colors
pq_candidates = [i for i in infos if i["has_red"] and i["has_blue"] and not i["is_screenshot"]]
if pq_candidates:
    best = max(pq_candidates, key=lambda x: x["sz"])
    shutil.copy2(best["path"], examples / "example_plasma_pq.png")
    print(f"  example_plasma_pq.png <- {best['path'].name} ({best['w']}x{best['h']})")

# Show what's NOT matched
matched = set()
for i in infos:
    if i["is_screenshot"]:
        matched.add(i["path"].name)
for c in auc_candidates[:1]:
    matched.add(c["path"].name)
for c in pq_candidates[:1]:
    matched.add(c["path"].name)

unmatched = [i for i in infos if i["path"].name not in matched]
if unmatched:
    print(f"\\n  Unmatched uploads ({len(unmatched)}):")
    for u in unmatched:
        print(f"    {u['path'].name} ({u['w']}x{u['h']}, {u['sz']:.0f}KB)")

print(f"\\n=== Current examples ===\\n")
for f in sorted(examples.glob("*.png")):
    print(f"  {f.name} ({f.stat().st_size/1024:.0f}KB)")

print(f"\\n  Still needed: example_dual_axis.png, example_lindane.png, example_saturation.png, example_barchart.png")
print(f"  Save these from the Claude chat to: {examples}\\\\")
