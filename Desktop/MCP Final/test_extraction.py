"""
Test script - Run this to test extraction quality against known ground truth.
Usage: python test_extraction.py [path_to_image]

Tests the extraction prompt against images and compares to known answers.
"""

import json
import base64
import sys
import os
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
from PIL import Image
import io
import re

load_dotenv()
client = Anthropic()

# ---------------------------------------------------------------------------
# Ground truth for test images
# ---------------------------------------------------------------------------
GROUND_TRUTH = {
    "auc_dose_response": {
        "description": "AUC0-20h scatter with polynomial fit R²=0.9713",
        "expected_points": 7,
        "expected_data": [
            {"x": 18, "y": 1}, {"x": 18, "y": 1.5},
            {"x": 63, "y": 1.5}, {"x": 63, "y": 3.5}, {"x": 63, "y": 12},
            {"x": 250, "y": 56}, {"x": 250, "y": 69}
        ],
        "tolerance_x": 5,
        "tolerance_y": 2
    },
    "plasma_pq": {
        "description": "Plasma-PQ concentration vs time, 3 series with error bars",
        "expected_points": 39,
        "expected_series": 3,
        "tolerance_x": 10,
        "tolerance_y": 0.5
    }
}


def image_to_base64(path: Path):
    ext = path.suffix.lower()
    media_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    }
    media_type = media_map.get(ext, "image/png")
    img = Image.open(path)
    if max(img.size) > 2048:
        img.thumbnail((2048, 2048), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8"), media_type


# ---------------------------------------------------------------------------
# THE PROMPT — this is what we're iterating on
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = r"""You are an expert at reading scientific figures with pixel-level precision.

TASK: Extract every data point from this figure. Follow these steps EXACTLY.

━━━ STEP 1: CALIBRATE YOUR COORDINATE SYSTEM ━━━

Read the axis tick marks and record them precisely:
- X-axis: list every visible tick label and its value
- Y-axis: list every visible tick label and its value  
- Determine the scale type (linear / logarithmic)

For EACH pair of adjacent ticks, note the distance in axis units. This is your ruler.

━━━ STEP 2: COUNT EVERY MARKER ━━━

Scan the plot area systematically from left to right.
For each cluster of markers at approximately the same x-position:
- How many individual markers are there?  
- Are they stacked vertically (same x, different y)?

CRITICAL: Markers stacked vertically at the same x-position share the EXACT SAME x-value. 
Do NOT spread them to nearby x-values. If 3 diamonds are all above x=63, they are ALL at x=63.

State your total marker count. You must find EVERY marker.

━━━ STEP 3: MEASURE EACH POINT ━━━

For each marker, determine its coordinates:

1. X-coordinate: Which two tick marks is it between? How far between them (as a fraction)?
   - Example: "Between tick 50 and tick 100. The marker is about 26% of the way from 50 to 100, so x ≈ 63"
   - If the marker is directly ON a tick mark, use that exact value.
   
2. Y-coordinate: Same method — which two y-ticks is it between, and what fraction?
   - Example: "Between y=0 and y=10, about 15% up, so y ≈ 1.5"

Round x-values to the nearest whole number. Round y-values to 1 decimal place max.

━━━ STEP 4: IDENTIFY SERIES ━━━

- How many distinct series? (Check the legend if present)
- What is each series' name, marker shape, and color?
- Are there error bars? If yes, measure upper/lower bounds.
- Is there a fit line? What type? Any R² value shown?

━━━ STEP 5: SELF-CHECK ━━━

1. Does your total point count match Step 2?
2. Plot your points mentally — does the shape match the figure?
3. Are there any clusters where you assigned different x-values? If so, look again — they probably share the same x.
4. Do values at the axis boundaries match the axis range?

━━━ OUTPUT ━━━

Respond with ONLY valid JSON (no markdown, no backticks, no explanation):
{
  "figure_info": {
    "title": "string or null",
    "x_axis": {"label": "string", "unit": "string or null", "min": number, "max": number, "scale": "linear"},
    "y_axis": {"label": "string", "unit": "string or null", "min": number, "max": number, "scale": "linear"},
    "r_squared": null,
    "fit_type": "string or null"
  },
  "series": [
    {
      "name": "string",
      "marker": "diamond|circle|square|triangle|cross|star",
      "color": "black|red|blue|green|etc",
      "line_style": "solid|dashed|dotted|none",
      "data_points": [{"x": number, "y": number}],
      "error_bars": [{"x": number, "y_low": number, "y_high": number}]
    }
  ]
}
"""


def extract(image_path: Path, model="claude-sonnet-4-20250514"):
    b64, media_type = image_to_base64(image_path)

    print(f"\n{'='*60}")
    print(f"Extracting from: {image_path.name}")
    print(f"Model: {model}")
    print(f"{'='*60}")

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": EXTRACTION_PROMPT},
            ],
        }],
    )

    raw = response.content[0].text.strip()
    
    # Save raw response for debugging
    debug_path = image_path.parent.parent / "outputs" / f"{image_path.stem}_debug.txt"
    with open(debug_path, "w") as f:
        f.write(raw)
    print(f"Raw response saved to: {debug_path}")

    # Parse JSON
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON PARSE ERROR: {e}")
        print(f"First 500 chars: {raw[:500]}")
        return None

    return result


def evaluate(result, ground_truth_key=None):
    """Print a summary of the extraction."""
    if not result:
        print("NO RESULT")
        return

    total_pts = sum(len(s["data_points"]) for s in result["series"])
    print(f"\nSeries count: {len(result['series'])}")
    print(f"Total points: {total_pts}")
    
    for s in result["series"]:
        print(f"\n  Series: {s['name']} ({s['marker']}, {s['color']})")
        print(f"  Points ({len(s['data_points'])}):")
        for p in s["data_points"]:
            print(f"    ({p['x']}, {p['y']})")
        if s.get("error_bars"):
            print(f"  Error bars: {len(s['error_bars'])} entries")

    if ground_truth_key and ground_truth_key in GROUND_TRUTH:
        gt = GROUND_TRUTH[ground_truth_key]
        print(f"\n--- GROUND TRUTH CHECK ---")
        print(f"Expected points: {gt['expected_points']}, Got: {total_pts}")
        if "expected_data" in gt:
            got = result["series"][0]["data_points"] if result["series"] else []
            for i, exp in enumerate(gt["expected_data"]):
                if i < len(got):
                    dx = abs(got[i]["x"] - exp["x"])
                    dy = abs(got[i]["y"] - exp["y"])
                    ok_x = dx <= gt["tolerance_x"]
                    ok_y = dy <= gt["tolerance_y"]
                    status = "OK" if (ok_x and ok_y) else "MISS"
                    print(f"  Point {i+1}: expected ({exp['x']},{exp['y']}) got ({got[i]['x']},{got[i]['y']}) -> {status} (dx={dx}, dy={dy})")
                else:
                    print(f"  Point {i+1}: expected ({exp['x']},{exp['y']}) -> MISSING")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Default: test all images in uploads/
        uploads = Path(__file__).parent / "uploads"
        images = list(uploads.glob("*.*"))
        if not images:
            print("No images in uploads/. Upload an image first via the web UI, or provide a path.")
            sys.exit(1)
    else:
        images = [Path(sys.argv[1])]

    for img in images:
        if img.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
            result = extract(img)
            evaluate(result, "auc_dose_response")
            
            # Save result
            out_path = img.parent.parent / "outputs" / f"{img.stem}_test.json"
            if result:
                with open(out_path, "w") as f:
                    json.dump(result, f, indent=2)
                print(f"\nSaved to: {out_path}")
