"""
Figure Data Extractor - Server v7
Claude focuses ONLY on precise data extraction.
Interactive charts are rendered CLIENT-SIDE from extracted data using Chart.js.
This ensures charts ALWAYS render (no more missing chart_html).
"""

import os
import json
import base64
import uuid
import re
import time
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory
from anthropic import Anthropic
from dotenv import load_dotenv
from PIL import Image
import io

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "config.json") as f:
    CFG = json.load(f)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = CFG["max_file_size_mb"] * 1024 * 1024

UPLOAD_DIR = BASE_DIR / CFG["upload_folder"]
OUTPUT_DIR = BASE_DIR / CFG["output_folder"]
EXAMPLES_DIR = BASE_DIR / "examples"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

client = Anthropic()
ALLOWED_EXT = set(CFG["allowed_extensions"])
PRIMARY_MODEL = os.getenv("EXTRACTION_MODEL", "claude-opus-4-20250514")
FALLBACK_MODEL = "claude-sonnet-4-20250514"
MAX_RETRIES = 3
RETRY_DELAY = 5


def allowed_file(fn):
    return "." in fn and fn.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def img_to_b64(path: Path) -> tuple[str, str]:
    img = Image.open(path)
    if max(img.size) > 4096:
        img.thumbnail((4096, 4096), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8"), "image/png"


def call_api(system, messages, max_tokens=16384):
    """Retry with Opus -> Sonnet fallback."""
    model = PRIMARY_MODEL
    for attempt in range(MAX_RETRIES):
        try:
            r = client.messages.create(
                model=model, max_tokens=max_tokens,
                system=system, messages=messages
            )
            return r.content[0].text, model
        except Exception as e:
            err = str(e).lower()
            if "overloaded" in err or "529" in err:
                if attempt < 1:
                    print(f"  [{model}] overloaded, retry in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"  Falling back to {FALLBACK_MODEL}")
                    model = FALLBACK_MODEL
                    time.sleep(2)
            else:
                raise
    raise Exception("All retries exhausted")


# ---------------------------------------------------------------------------
# EXTRACTION-ONLY PROMPT (no chart generation - that's done client-side)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = r"""You are an expert at analyzing scientific research figures and extracting precise numerical data from chart images.

Your ONLY job is to extract data with maximum precision. You do NOT generate charts or HTML.

CRITICAL EXTRACTION RULES:
- Use the fraction-between-ticks method to determine coordinates precisely
- 36% between tick 0 and tick 50 = x=18 (NOT 15 or 20)
- 26% between tick 50 and tick 100 = x=63 (NOT 60 or 65)
- Markers ON a tick mark use the exact tick value (250 = 250, NOT 260)
- Vertically stacked markers share the EXACT SAME x-value
- Count EVERY marker - missing even one is unacceptable
- For multi-series: extract ALL points for ALL series separately
- If error bars exist, extract the error bar extents for each point
- If a fit/trend curve exists, sample it at regular intervals to capture its shape

OUTPUT FORMAT (JSON only, no markdown, no extra text):
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
      "marker": "diamond|circle|square|triangle|rect",
      "color": "black|red|blue|green|gray|orange|purple",
      "line_style": "solid|dashed|none",
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

EXTRACT_TEXT = """Analyze this research figure with your vision capabilities.

Extract ALL data with maximum precision:
1. Every data point from every series (count markers carefully)
2. Error bar extents (y_low and y_high absolute values) if present
3. Fit/trend curve sampled at ~20 points if present
4. Axis labels, units, ranges, and scale type

Output ONLY valid JSON. No markdown fences, no commentary."""


def build_few_shot():
    """Load example images + ground truth for few-shot prompting."""
    gt_path = EXAMPLES_DIR / "ground_truth.json"
    if not gt_path.exists():
        return []
    with open(gt_path, encoding="utf-8") as f:
        examples = json.load(f)

    messages = []
    for ex in examples:
        img_path = EXAMPLES_DIR / ex["image_filename"]
        if not img_path.exists() or img_path.stat().st_size < 20000:
            continue
        b64, mtype = img_to_b64(img_path)
        gt = ex["ground_truth"]

        messages.append({
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mtype, "data": b64}},
                {"type": "text", "text": "Extract all data points from this figure."},
            ]
        })
        messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": json.dumps(gt, indent=2)}]
        })

    return messages


FEW_SHOT = build_few_shot()


def extract_data(image_path: Path) -> dict:
    """Extract data only - chart rendering happens client-side."""
    b64, mtype = img_to_b64(image_path)

    messages = list(FEW_SHOT)
    messages.append({
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mtype, "data": b64}},
            {"type": "text", "text": EXTRACT_TEXT},
        ]
    })

    print(f"  Extracting data ({len(FEW_SHOT)//2} few-shot examples)...")
    raw, model = call_api(SYSTEM_PROMPT, messages)
    print(f"  Done ({model}, {len(raw)} chars)")

    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    data = json.loads(raw)

    result = {"data": data, "_model": model}

    # Save debug
    (OUTPUT_DIR / f"{image_path.stem}_debug.txt").write_text(
        f"Model: {model}\n\n{raw}", encoding="utf-8"
    )
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type"}), 400
    ext = file.filename.rsplit(".", 1)[1].lower()
    uid = uuid.uuid4().hex[:8]
    fname = f"{uid}.{ext}"
    (UPLOAD_DIR / fname).write_bytes(file.read())
    return jsonify({"file_id": uid, "filename": fname}), 200


@app.route("/api/extract/<file_id>", methods=["POST"])
def extract(file_id):
    matches = list(UPLOAD_DIR.glob(f"{file_id}.*"))
    if not matches:
        return jsonify({"error": "File not found"}), 404
    try:
        result = extract_data(matches[0])
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON parse: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    out_path = OUTPUT_DIR / f"{file_id}_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return jsonify(result), 200


@app.route("/api/data/<file_id>")
def get_data(file_id):
    p = OUTPUT_DIR / f"{file_id}_data.json"
    if not p.exists():
        return jsonify({"error": "Not found"}), 404
    with open(p, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/download/<file_id>")
def download_csv(file_id):
    p = OUTPUT_DIR / f"{file_id}_data.json"
    if not p.exists():
        return jsonify({"error": "Not found"}), 404
    with open(p, encoding="utf-8") as f:
        full = json.load(f)
    data = full.get("data", full)
    lines = ["series,x,y,y_low,y_high"]
    for s in data.get("series", []):
        eb_map = {}
        for eb in s.get("error_bars", []):
            eb_map[eb["x"]] = eb
        for pt in s["data_points"]:
            eb = eb_map.get(pt["x"], {})
            lines.append(f'{s["name"]},{pt["x"]},{pt["y"]},{eb.get("y_low","")},{eb.get("y_high","")}')
    csv_path = OUTPUT_DIR / f"{file_id}_data.csv"
    csv_path.write_text("\n".join(lines))
    return send_from_directory(str(OUTPUT_DIR), f"{file_id}_data.csv",
                               as_attachment=True, mimetype="text/csv")


@app.route("/api/download_json/<file_id>")
def download_json(file_id):
    p = OUTPUT_DIR / f"{file_id}_data.json"
    if not p.exists():
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(str(OUTPUT_DIR), f"{file_id}_data.json",
                               as_attachment=True, mimetype="application/json")


@app.route("/uploads/<filename>")
def serve_upload(filename):
    return send_from_directory(str(UPLOAD_DIR), filename)


@app.route("/api/test_extract")
def test_extract():
    """Test extraction with existing example image."""
    img = EXAMPLES_DIR / "example_auc.png"
    if not img.exists():
        return jsonify({"error": "No example_auc.png in examples/"}), 404
    import shutil
    uid = "test_auc"
    dst = UPLOAD_DIR / f"{uid}.png"
    shutil.copy2(img, dst)
    try:
        result = extract_data(dst)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    out_path = OUTPUT_DIR / f"{uid}_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return jsonify(result), 200


if __name__ == "__main__":
    n = len(FEW_SHOT) // 2
    print(f"\n  Figure Data Extractor v7")
    print(f"  Model: {PRIMARY_MODEL} | Fallback: {FALLBACK_MODEL}")
    print(f"  Few-shot: {n} examples (>20KB images only)")
    print(f"  Mode: Extract data -> Client-side Chart.js rendering")
    print(f"  http://{CFG['host']}:{CFG['port']}\n")
    app.run(host=CFG["host"], port=CFG["port"], debug=True)
