"""
TEST: Extract data from Plasma-PQ figure and verify against ground truth.
This script tests the extraction prompt directly and outputs results.
Run: python test_prompt.py
"""
import json
import base64
import re
import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
from PIL import Image
import io

load_dotenv()
client = Anthropic()
BASE_DIR = Path(__file__).parent

# Ground truth for Plasma-PQ
EXPECTED = {
    "Intoxication group": [
        (0,0),(15,3.5),(30,5.2),(45,7.0),(60,6.8),(90,6.5),
        (120,5.2),(180,3.5),(240,2.5),(300,2.5),(360,1.6),(480,1.5),(600,1.2)
    ],
    "Treatment group": [
        (0,0),(15,2.2),(30,5.0),(45,5.5),(60,5.0),(90,3.5),
        (120,3.3),(180,2.0),(240,1.5),(300,1.0),(360,0.8),(480,0.6),(600,0.6)
    ],
    "Inclusion complex group": [
        (0,0),(15,2.5),(30,2.6),(45,2.5),(60,2.5),(90,2.0),
        (120,1.5),(180,1.3),(240,1.0),(300,0.8),(360,0.5),(480,0.5),(600,0.4)
    ]
}

# AUC ground truth
EXPECTED_AUC = {
    "Data points": [
        (18,1.0),(18,1.5),(63,1.5),(63,3.5),(63,12),(250,56),(250,69)
    ]
}


def img_to_b64(path):
    img = Image.open(path)
    if max(img.size) > 4096:
        img.thumbnail((4096,4096), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def load_few_shot_examples():
    """Load example images + ground truth as few-shot messages."""
    gt_path = BASE_DIR / "examples" / "ground_truth.json"
    if not gt_path.exists():
        return []
    with open(gt_path, encoding="utf-8") as f:
        examples = json.load(f)
    
    messages = []
    for ex in examples:
        img_path = BASE_DIR / "examples" / ex["image_filename"]
        if not img_path.exists():
            continue
        b64 = img_to_b64(img_path)
        gt = ex["ground_truth"]
        messages.append({
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": "Extract all data points from this figure with maximum precision."},
            ]
        })
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": json.dumps(gt, indent=2)}]
        })
    return messages


SYSTEM_PROMPT = r"""You are a precision data-point extractor for scientific research figures.
You extract data points from figure images with pixel-level accuracy.
You have been trained on verified examples of correct extractions. Match that precision.

CRITICAL RULES:

1. FRACTION-BETWEEN-TICKS METHOD:
   For each marker, determine which two axis ticks bracket it, then estimate the fraction:
   x = left_tick + fraction * (right_tick - left_tick)
   Example: 36% between tick 0 and tick 50 = x=18 (NOT 15, NOT 20)
   Example: 26% between tick 50 and tick 100 = x=63 (NOT 60, NOT 65)
   Example: marker ON tick 250 = x=250 (NOT 260)

2. VERTICALLY STACKED MARKERS: Same x-value. Never spread to nearby values.

3. COUNT EVERY MARKER. Missing even one is unacceptable.

4. DO NOT ROUND to nearest 5 or 10 unless marker is exactly there.

5. ERROR BARS: measure top and bottom y-values.

6. MULTI-SERIES: Extract ALL points for ALL series.

7. FIGURE TYPES: scatter, line, bar, scatter_with_fit, etc.

8. For line plots: data_points connected by lines, set line_style="solid".

9. For fit curves: provide interpolated points in fit_curves array.

OUTPUT: ONLY valid JSON (no markdown, no backticks):
{
  "figure_info": {
    "title": "string or null",
    "x_axis": {"label": "string", "unit": "string or null", "min": number, "max": number, "scale": "linear|log"},
    "y_axis": {"label": "string", "unit": "string or null", "min": number, "max": number, "scale": "linear|log"},
    "r_squared": null,
    "fit_type": "string or null",
    "chart_type": "scatter|line|bar|scatter_with_fit"
  },
  "series": [
    {
      "name": "string",
      "marker": "diamond|circle|square|triangle|cross|star",
      "color": "black|red|blue|green|gray|etc",
      "line_style": "solid|dashed|dotted|none",
      "data_points": [{"x": number, "y": number}],
      "error_bars": [{"x": number, "y_low": number, "y_high": number}]
    }
  ],
  "fit_curves": [
    {
      "name": "string",
      "color": "string",
      "line_style": "solid|dashed",
      "points": [{"x": number, "y": number}]
    }
  ]
}"""

EXTRACT_TEXT = """Extract all data points from this figure with maximum precision.

Remember:
- Use fraction-between-ticks method
- Vertically stacked markers share the SAME x-value  
- Count every single marker
- If a marker sits RIGHT ON a tick mark, use that exact tick value
- Look carefully at clusters - two overlapping markers can look like one
- For line plots: set line_style="solid" and chart_type="line"
- For scatter with fit: set chart_type="scatter_with_fit" and provide fit_curves
- Output ONLY valid JSON, no other text"""


def extract(image_path, model="claude-opus-4-20250514"):
    b64 = img_to_b64(image_path)
    
    # Build messages with few-shot
    few_shot = load_few_shot_examples()
    messages = list(few_shot)
    messages.append({
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": EXTRACT_TEXT},
        ]
    })
    
    print(f"Calling {model} with {len(few_shot)//2} few-shot examples...")
    
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    
    return json.loads(raw), raw


def compare(result, expected, name):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    
    total_pts = sum(len(s["data_points"]) for s in result["series"])
    total_exp = sum(len(v) for v in expected.values())
    print(f"  Series: {len(result['series'])} (expected {len(expected)})")
    print(f"  Points: {total_pts} (expected {total_exp})")
    print(f"  Chart type: {result['figure_info'].get('chart_type', 'N/A')}")
    
    errors = 0
    for series in result["series"]:
        sname = series["name"]
        pts = [(p["x"], p["y"]) for p in series["data_points"]]
        print(f"\n  {sname} ({len(pts)} points):")
        
        # Find best matching expected series
        best_match = None
        best_score = -1
        for ename, edata in expected.items():
            if ename.lower() in sname.lower() or sname.lower() in ename.lower():
                best_match = ename
                break
        
        if best_match:
            edata = expected[best_match]
            print(f"    Matched to: {best_match} ({len(edata)} expected)")
            for j, (ex, ey) in enumerate(edata):
                if j < len(pts):
                    gx, gy = pts[j]
                    dx = abs(gx - ex)
                    dy = abs(gy - ey)
                    ok = dx <= 5 and dy <= 0.5
                    if not ok:
                        errors += 1
                    status = "OK" if ok else "MISS"
                    print(f"    [{j+1}] exp({ex},{ey}) got({gx},{gy}) {status} dx={dx} dy={dy:.1f}")
                else:
                    errors += 1
                    print(f"    [{j+1}] exp({ex},{ey}) MISSING")
        else:
            print(f"    No matching expected series found!")
            errors += len(pts)
    
    has_errorbars = any(len(s.get("error_bars", [])) > 0 for s in result["series"])
    has_fit = len(result.get("fit_curves", [])) > 0
    print(f"\n  Error bars: {'YES' if has_errorbars else 'NO'}")
    print(f"  Fit curves: {'YES' if has_fit else 'NO'}")
    print(f"  Total errors: {errors}")
    return errors


if __name__ == "__main__":
    import sys
    
    model = os.getenv("EXTRACTION_MODEL", "claude-opus-4-20250514")
    
    # Test with AUC if available
    auc_path = BASE_DIR / "examples" / "example_auc.png"
    if auc_path.exists():
        print("\n" + "="*60)
        print("  TEST 1: AUC Dose-Response")
        print("="*60)
        try:
            result, raw = extract(auc_path, model)
            # Save
            (BASE_DIR / "outputs" / "test_auc_result.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            errors = compare(result, EXPECTED_AUC, "AUC Dose-Response")
            if errors == 0:
                print("\n  *** PERFECT EXTRACTION! ***")
            else:
                print(f"\n  {errors} errors - needs improvement")
        except Exception as e:
            print(f"  ERROR: {e}")
    
    # Test with Plasma-PQ if available
    pq_path = BASE_DIR / "examples" / "example_plasma_pq.png"
    if pq_path.exists() and pq_path.stat().st_size > 50000:  # Must be the real PQ, not tiny AUC
        print("\n" + "="*60)
        print("  TEST 2: Plasma-PQ Concentration")
        print("="*60)
        try:
            result, raw = extract(pq_path, model)
            (BASE_DIR / "outputs" / "test_pq_result.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            errors = compare(result, EXPECTED, "Plasma-PQ")
            if errors == 0:
                print("\n  *** PERFECT EXTRACTION! ***")
            else:
                print(f"\n  {errors} errors - needs improvement")
        except Exception as e:
            print(f"  ERROR: {e}")
    else:
        print("\n  Skipping Plasma-PQ test (image not available or too small)")
        print(f"  Save the Plasma-PQ figure to: {pq_path}")
    
    print("\n  Done!")
