"""
Auto-identify and copy ALL example images using pixel analysis.
Run: python auto_setup_examples.py
"""
from PIL import Image
from pathlib import Path
import shutil

base = Path(__file__).parent
uploads = base / "uploads"
examples = base / "examples"
examples.mkdir(exist_ok=True)

def analyze(path):
    """Analyze an image to determine what kind of figure it is."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    sz = path.stat().st_size / 1024
    
    # Sample pixels across the image
    has_red = False
    has_blue = False
    has_brown = False
    is_dark_bg = False
    has_hatching = False
    red_count = 0
    blue_count = 0
    dark_count = 0
    white_count = 0
    
    step = max(1, min(w, h) // 50)
    total = 0
    for x in range(0, w, step):
        for y in range(0, h, step):
            r, g, b = img.getpixel((x, y))
            total += 1
            if r > 180 and g < 80 and b < 80:
                red_count += 1
            if b > 180 and r < 80 and g < 80:
                blue_count += 1
            if r < 30 and g < 30 and b < 40:
                dark_count += 1
            if r > 240 and g > 240 and b > 240:
                white_count += 1
            if r > 120 and g > 80 and b < 60:
                has_brown = True
    
    red_pct = red_count / total * 100
    blue_pct = blue_count / total * 100
    dark_pct = dark_count / total * 100
    white_pct = white_count / total * 100
    
    return {
        "path": path,
        "w": w, "h": h, "sz": sz,
        "ratio": w/h,
        "red_pct": red_pct,
        "blue_pct": blue_pct,
        "dark_pct": dark_pct,
        "white_pct": white_pct,
        "has_brown": has_brown,
    }

# Analyze all uploads
print("\n=== Analyzing all uploads ===\n")
infos = []
for f in sorted(uploads.glob("*.png")):
    if f.name == ".gitkeep":
        continue
    info = analyze(f)
    infos.append(info)
    print(f"  {f.name:20s} {info['w']}x{info['h']}  {info['sz']:.0f}KB  "
          f"red={info['red_pct']:.1f}% blue={info['blue_pct']:.1f}% "
          f"dark={info['dark_pct']:.1f}% white={info['white_pct']:.1f}%")

# Classification rules
print("\n=== Classifying ===\n")

assignments = {}

for info in infos:
    f = info["path"]
    
    # Skip localhost screenshots (very dark background)
    if info["dark_pct"] > 40:
        print(f"  {f.name}: LOCALHOST SCREENSHOT (skip)")
        continue
    
    # Plasma-PQ: has significant red AND blue (3 colored series)
    if info["red_pct"] > 1.5 and info["blue_pct"] > 1.0 and info["w"] > 400:
        if "plasma_pq" not in assignments or info["sz"] > assignments["plasma_pq"]["sz"]:
            assignments["plasma_pq"] = info
            print(f"  {f.name}: PLASMA-PQ (red={info['red_pct']:.1f}% blue={info['blue_pct']:.1f}%)")
        continue
    
    # Bar chart: has hatching patterns, significant white, moderate size
    # The PQ bar chart has distinctive hatched bars
    if info["white_pct"] > 60 and 400 < info["w"] < 800 and info["red_pct"] < 1:
        if info["h"] > 400:  # bar chart is roughly square
            if "barchart" not in assignments:
                assignments["barchart"] = info
                print(f"  {f.name}: BAR CHART (white={info['white_pct']:.1f}%)")
                continue
    
    # AUC: wide image, mostly white/gray, no colors
    if info["w"] > 1000 and info["red_pct"] < 0.5 and info["blue_pct"] < 0.5:
        if "auc" not in assignments or info["w"] > assignments["auc"]["w"]:
            assignments["auc"] = info
            print(f"  {f.name}: AUC DOSE-RESPONSE (wide, no color)")
        continue
    
    # Small figures
    if info["sz"] < 25:
        print(f"  {f.name}: SMALL FIGURE (maybe multi-series PK)")
        if "small_pk" not in assignments:
            assignments["small_pk"] = info
        continue
    
    # Dual axis: has gray markers, moderate size, mostly white
    if info["white_pct"] > 50 and 400 < info["w"] < 600 and info["red_pct"] < 0.5:
        if "dual_axis" not in assignments:
            assignments["dual_axis"] = info
            print(f"  {f.name}: DUAL AXIS (moderate, white bg)")
            continue
    
    # Lindane: moderate size, white bg, has error bars
    if info["white_pct"] > 40 and 400 < info["w"] < 700 and info["red_pct"] < 0.5:
        if "lindane" not in assignments:
            assignments["lindane"] = info
            print(f"  {f.name}: LINDANE (moderate, white bg)")
            continue
    
    # Saturation: moderate-wide, white bg
    if info["white_pct"] > 30 and info["w"] > 500:
        if "saturation" not in assignments:
            assignments["saturation"] = info
            print(f"  {f.name}: SATURATION CURVES")
            continue
    
    print(f"  {f.name}: UNCLASSIFIED")

# Copy to examples
print("\n=== Copying to examples ===\n")

mapping = {
    "auc": "example_auc.png",
    "plasma_pq": "example_plasma_pq.png",
    "dual_axis": "example_dual_axis.png",
    "lindane": "example_lindane.png",
    "saturation": "example_saturation.png",
    "barchart": "example_barchart.png",
}

for key, filename in mapping.items():
    if key in assignments:
        src = assignments[key]["path"]
        dst = examples / filename
        shutil.copy2(src, dst)
        print(f"  OK  {filename} <- {src.name} ({assignments[key]['sz']:.0f}KB)")
    else:
        dst = examples / filename
        if dst.exists():
            print(f"  KEPT {filename} (already exists, {dst.stat().st_size/1024:.0f}KB)")
        else:
            print(f"  MISSING {filename}")

print("\n=== Final examples folder ===\n")
for f in sorted(examples.glob("*.*")):
    print(f"  {f.name} ({f.stat().st_size/1024:.0f}KB)")

total = len(list(examples.glob("*.png")))
print(f"\n  Total example images: {total}/6")
print(f"  Restart server with: .\\run.ps1")
