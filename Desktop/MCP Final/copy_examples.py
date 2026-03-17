"""
Setup all example images for few-shot prompting.
Copies from uploads/ and tells you which ones to save manually.
"""
import shutil
from pathlib import Path

base = Path(__file__).parent
examples = base / "examples"
uploads = base / "uploads"
examples.mkdir(exist_ok=True)

# Map of example filenames and what they are
NEEDED = {
    "example_auc.png": "AUC dose-response scatter (diamond markers, R2=0.9713, x-ticks 0-300)",
    "example_plasma_pq.png": "Plasma-PQ 3-series (black/red/blue lines, error bars, 0-600 min)",
    "example_dual_axis.png": "Dual Y-axis scatter (1uM black circles + 500nM gray triangles, 0-150)",
    "example_lindane.png": "Lindane blood concentration (PK curve with error bars, 0-8 days)",
    "example_saturation.png": "Saturation curves 2 series (squares+circles, 0-96, error bars)",
    "example_barchart.png": "Bar chart PQ vs Inclusion complex (hatched bars, 0.5-2.0 h)",
}

# Try to copy AUC from uploads if available
auc_src = uploads / "b9564794.png"
if auc_src.exists() and not (examples / "example_auc.png").exists():
    shutil.copy2(auc_src, examples / "example_auc.png")
    print(f"  COPIED: example_auc.png from uploads/b9564794.png")

# Status
print("\n  === Example Images Status ===\n")
found = 0
missing = 0
for name, desc in NEEDED.items():
    path = examples / name
    if path.exists():
        sz = path.stat().st_size / 1024
        print(f"  OK      {name} ({sz:.0f} KB)")
        print(f"          {desc}")
        found += 1
    else:
        print(f"  MISSING {name}")
        print(f"          -> {desc}")
        print(f"          Save to: {path}")
        missing += 1
    print()

print(f"  Found: {found}/{len(NEEDED)}  |  Missing: {missing}/{len(NEEDED)}")
if missing > 0:
    print(f"\n  To add missing images:")
    print(f"  Right-click each figure in Claude chat -> Save Image As")
    print(f"  Save to: {examples}\\<filename>")
print(f"\n  After adding images, restart the server (.\run.ps1)")
