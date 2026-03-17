"""Copy downloaded full-size images and verify."""
import shutil
from pathlib import Path

downloads = Path.home() / "Downloads"
examples = Path(__file__).parent / "examples"

for name in ["example_plasma_pq.png", "example_lindane.png"]:
    # Check multiple possible locations
    for src_dir in [downloads, downloads / "chrome"]:
        src = src_dir / name
        if src.exists() and src.stat().st_size > 20000:  # Must be > 20KB (not thumbnail)
            dst = examples / name
            shutil.copy2(src, dst)
            print(f"  COPIED: {name} ({dst.stat().st_size/1024:.0f} KB)")
            break
    else:
        # Check if already exists and is big enough
        dst = examples / name
        if dst.exists() and dst.stat().st_size > 20000:
            print(f"  EXISTS: {name} ({dst.stat().st_size/1024:.0f} KB)")
        else:
            print(f"  MISSING/TOO SMALL: {name}")

print()
for f in sorted(examples.glob("*.png")):
    print(f"  {f.name} ({f.stat().st_size/1024:.0f} KB)")
