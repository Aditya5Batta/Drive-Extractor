"""
Save example images by copying from uploads and converting.
Run this ONCE after uploading all example images via the web UI.
"""
import shutil
import sys
from pathlib import Path

base = Path(__file__).parent
examples = base / "examples"
uploads = base / "uploads"
examples.mkdir(exist_ok=True)

# List all uploads
print("\nAvailable uploads:")
for f in sorted(uploads.glob("*.*")):
    if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
        print(f"  {f.name} ({f.stat().st_size/1024:.0f} KB)")

print("\nUsage:")
print("  python save_examples.py <upload_filename> <example_name>")
print()
print("Example names needed:")
print("  example_auc.png              - AUC dose-response (diamond markers, R2=0.9713)")
print("  example_plasma_pq.png        - Plasma-PQ 3 series (black/red/blue)")
print("  example_dual_axis.png        - 1uM + 500nM dual axis scatter")
print("  example_lindane.png          - Lindane blood concentration PK curve")
print("  example_saturation.png       - Saturation 2 series (squares+circles)")
print("  example_barchart.png         - Bar chart PQ vs Inclusion complex")
print()

if len(sys.argv) == 3:
    src = uploads / sys.argv[1]
    dst = examples / sys.argv[2]
    if src.exists():
        shutil.copy2(src, dst)
        print(f"COPIED: {src.name} -> {dst.name} ({dst.stat().st_size/1024:.0f} KB)")
    else:
        print(f"ERROR: {src} not found")
elif len(sys.argv) == 1:
    print("\nCurrent examples:")
    for f in sorted(examples.glob("*.png")):
        print(f"  {f.name} ({f.stat().st_size/1024:.0f} KB)")
