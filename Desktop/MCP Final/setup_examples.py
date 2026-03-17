"""
Setup Examples - Run ONCE to copy your test images into the examples folder.

Usage:
  python setup_examples.py <path_to_auc_image> <path_to_plasma_pq_image>

Example:
  python setup_examples.py uploads/b9564794.png uploads/d3df1e6d.png

Or simply copy the images manually:
  1. Copy the AUC dose-response figure to: examples/example_auc.png
  2. Copy the Plasma-PQ 3-series figure to: examples/example_plasma_pq.png
"""

import sys
import shutil
from pathlib import Path

EXAMPLES_DIR = Path(__file__).parent / "examples"
EXAMPLES_DIR.mkdir(exist_ok=True)

EXPECTED = {
    "example_auc.png": "AUC dose-response scatter plot (the one with R²=0.9713, diamond markers)",
    "example_plasma_pq.png": "Plasma-PQ concentration 3-series line plot (Intoxication/Treatment/Inclusion complex)"
}


def main():
    if len(sys.argv) >= 3:
        src_auc = Path(sys.argv[1])
        src_pq = Path(sys.argv[2])
        
        if src_auc.exists():
            shutil.copy2(src_auc, EXAMPLES_DIR / "example_auc.png")
            print(f"  Copied {src_auc} -> examples/example_auc.png")
        else:
            print(f"  ERROR: {src_auc} not found")
        
        if src_pq.exists():
            shutil.copy2(src_pq, EXAMPLES_DIR / "example_plasma_pq.png")
            print(f"  Copied {src_pq} -> examples/example_plasma_pq.png")
        else:
            print(f"  ERROR: {src_pq} not found")
    else:
        print("  No arguments provided. Checking examples folder...\n")
        for name, desc in EXPECTED.items():
            path = EXAMPLES_DIR / name
            if path.exists():
                print(f"  OK  {name} ({path.stat().st_size/1024:.1f} KB)")
            else:
                print(f"  MISSING  {name}")
                print(f"           -> {desc}")
                print(f"           Place it at: {path}\n")

    print("\n  After placing both images, restart the server to load them as few-shot examples.")


if __name__ == "__main__":
    main()
