import hashlib
import json
import os
import random
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageDraw, ImageFont


app = Flask(__name__)

DATA_DIR = Path(os.getenv("TSHIRT_DATA_DIR", "/data")).resolve()
DESIGNS_DIR = DATA_DIR / "designs"
CATALOG_PATH = DATA_DIR / "designs.json"
STATUS_PATH = DATA_DIR / "status.json"

OLLAMA_HOST = os.getenv("TSHIRT_OLLAMA_HOST", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("TSHIRT_OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("TSHIRT_OLLAMA_TIMEOUT_SECONDS", "120"))
GENERATE_INTERVAL_SECONDS = max(60, int(os.getenv("TSHIRT_GENERATE_INTERVAL_SECONDS", "3600")))
IMAGE_PROVIDER = os.getenv("TSHIRT_IMAGE_PROVIDER", "ollama_svg").strip().lower()
POLLINATIONS_MODEL = os.getenv("TSHIRT_POLLINATIONS_MODEL", "flux")
MAX_DESIGNS = max(1, int(os.getenv("TSHIRT_MAX_DESIGNS", "80")))
IMAGE_SIZE = max(768, int(os.getenv("TSHIRT_IMAGE_SIZE", "2048")))

store_lock = threading.Lock()
generation_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DESIGNS_DIR.mkdir(parents=True, exist_ok=True)


def safe_id(raw):
    value = str(raw or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("Invalid design id.")
    return value


def read_json(path, fallback):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return fallback


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def load_catalog():
    catalog = read_json(CATALOG_PATH, [])
    if not isinstance(catalog, list):
        return []
    return [item for item in catalog if isinstance(item, dict)]


def save_catalog(catalog):
    trimmed = sorted(
        catalog,
        key=lambda item: str(item.get("created_at_utc") or ""),
        reverse=True,
    )[:MAX_DESIGNS]
    write_json(CATALOG_PATH, trimmed)
    return trimmed


def load_status():
    status = read_json(STATUS_PATH, {})
    if not isinstance(status, dict):
        status = {}
    status.setdefault("is_running", False)
    status.setdefault("last_attempt_at_utc", "")
    status.setdefault("last_success_at_utc", "")
    status.setdefault("last_error", "")
    status.setdefault("current_message", "Idle")
    return status


def save_status(**updates):
    with store_lock:
        status = load_status()
        status.update(updates)
        status["interval_seconds"] = GENERATE_INTERVAL_SECONDS
        status["provider"] = IMAGE_PROVIDER
        last_attempt = parse_iso(status.get("last_attempt_at_utc"))
        if last_attempt:
            status["next_attempt_at_utc"] = (
                last_attempt + timedelta(seconds=GENERATE_INTERVAL_SECONDS)
            ).isoformat().replace("+00:00", "Z")
        else:
            status["next_attempt_at_utc"] = iso_now()
        write_json(STATUS_PATH, status)
        return status


def catalog_payload():
    with store_lock:
        catalog = load_catalog()
    visible = []
    for item in catalog:
        design_id = item.get("id")
        filename = item.get("filename")
        if not design_id or not filename:
            continue
        try:
            design_id = safe_id(design_id)
        except ValueError:
            continue
        if not (DESIGNS_DIR / design_id / filename).exists():
            continue
        visible.append(item)
    return visible


def normalize_ollama_payload(text):
    body = str(text or "").strip()
    body = re.sub(r"^```(?:json)?\s*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\s*```$", "", body)
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    match = re.search(r"\{.*\}", body, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def ask_ollama_for_design():
    prompt = f"""
Create one original print-ready T-shirt design concept.
Return only a JSON object, with no Markdown.

The JSON must have:
- title: 3 to 8 words
- prompt: a detailed image-generation prompt for a premium T-shirt graphic
- palette: 3 to 6 hex colors
- tags: 3 to 7 short lowercase tags
- svg: a complete self-contained SVG illustration for the shirt front

SVG rules:
- viewBox must be 0 0 2048 2048
- use a transparent background
- avoid copyrighted logos, celebrity likenesses, team names, brand names, and trademarked characters
- no scripts, external links, foreignObject, animations, or embedded images
- keep text to a minimum; if text is used, make it generic and short
- make it bold enough for screen printing and suitable for a black or white shirt
"""
    response = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.9,
                "num_predict": 4200,
            },
        },
        timeout=OLLAMA_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    return normalize_ollama_payload(payload.get("response", ""))


def fallback_brief():
    themes = [
        ("Midnight Circuit Bloom", "a futuristic botanical circuit-board flower emblem"),
        ("Solar Drift Club", "a retro sun, drifting clouds, and bold geometric waves"),
        ("Quiet Thunder", "a minimalist lightning crest with layered ink texture"),
        ("Neon Workshop", "a clean vaporwave tool badge with abstract sparks"),
        ("Orbit Motel", "a space-travel souvenir patch with moons and crisp line art"),
    ]
    title, idea = random.choice(themes)
    palette = random.choice(
        [
            ["#f8fafc", "#111827", "#14b8a6", "#f59e0b"],
            ["#fff7ed", "#172554", "#ef4444", "#22c55e"],
            ["#fefce8", "#18181b", "#38bdf8", "#fb7185"],
        ]
    )
    return {
        "title": title,
        "prompt": (
            f"premium print-ready T-shirt graphic, {idea}, centered composition, "
            "transparent background, bold vector forms, crisp edges, high contrast, screen print style"
        ),
        "palette": palette,
        "tags": ["fallback", "vector", "shirt"],
        "svg": "",
    }


def sanitize_svg(svg):
    raw = str(svg or "").strip()
    match = re.search(r"<svg\b.*?</svg>", raw, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    svg = match.group(0)
    blocked = [
        r"<\s*script\b",
        r"<\s*foreignObject\b",
        r"javascript:",
        r"<\s*iframe\b",
        r"<\s*object\b",
        r"<\s*embed\b",
        r"<\s*image\b",
        r"<\s*animate",
    ]
    if any(re.search(pattern, svg, flags=re.IGNORECASE) for pattern in blocked):
        return ""
    svg = re.sub(r"\s+on[a-z]+\s*=\s*(['\"]).*?\1", "", svg, flags=re.IGNORECASE | re.DOTALL)
    svg = re.sub(r"\s+href\s*=\s*(['\"])\s*https?://.*?\1", "", svg, flags=re.IGNORECASE | re.DOTALL)
    svg = re.sub(r"\s+xlink:href\s*=\s*(['\"])\s*https?://.*?\1", "", svg, flags=re.IGNORECASE | re.DOTALL)
    if "xmlns=" not in svg[:300].lower():
        svg = re.sub(r"<svg\b", '<svg xmlns="http://www.w3.org/2000/svg"', svg, count=1, flags=re.IGNORECASE)
    if "viewBox" not in svg[:500]:
        svg = re.sub(r"<svg\b", '<svg viewBox="0 0 2048 2048"', svg, count=1, flags=re.IGNORECASE)
    return svg


def wrap_text(draw, text, font, max_width):
    words = str(text or "").split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def load_font(size, bold=False):
    candidates = [
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_prompt_card(brief, target_path):
    palette = [str(color) for color in brief.get("palette") or [] if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(color))]
    if len(palette) < 3:
        palette = ["#f8fafc", "#111827", "#14b8a6", "#f59e0b"]
    title = str(brief.get("title") or "T-Shirt Design").strip()[:80]
    prompt = str(brief.get("prompt") or "").strip()

    image = Image.new("RGBA", (IMAGE_SIZE, IMAGE_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = IMAGE_SIZE // 2
    scale = IMAGE_SIZE / 2048

    def s(value):
        return int(value * scale)

    primary = palette[1]
    accent = palette[2]
    warm = palette[3] if len(palette) > 3 else palette[0]

    draw.ellipse((s(370), s(260), s(1678), s(1552)), fill=primary)
    draw.ellipse((s(520), s(380), s(1528), s(1380)), fill=palette[0])
    draw.polygon(
        [
            (center, s(280)),
            (s(1450), s(780)),
            (s(1180), s(1470)),
            (s(680), s(1470)),
            (s(420), s(780)),
        ],
        fill=accent,
    )
    draw.polygon(
        [
            (center, s(455)),
            (s(1260), s(835)),
            (s(1110), s(1240)),
            (s(800), s(1240)),
            (s(650), s(835)),
        ],
        fill=warm,
    )
    draw.line((s(430), s(1560), s(1618), s(1560)), fill=primary, width=s(28))
    draw.line((s(540), s(1640), s(1508), s(1640)), fill=accent, width=s(18))

    title_font = load_font(s(118), bold=True)
    body_font = load_font(s(42), bold=False)
    title_lines = wrap_text(draw, title.upper(), title_font, s(1400))[:3]
    y = s(1660)
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        draw.text((center - (bbox[2] - bbox[0]) // 2, y), line, fill=primary, font=title_font)
        y += s(128)

    tagline = " / ".join((brief.get("tags") or ["print", "design"])[:4])
    tagline = tagline.upper()
    bbox = draw.textbbox((0, 0), tagline, font=body_font)
    draw.text((center - (bbox[2] - bbox[0]) // 2, min(y + s(18), s(1930))), tagline, fill=accent, font=body_font)

    if not prompt:
        prompt = "Fallback print graphic generated from a local design brief."
    metadata_path = target_path.with_suffix(".prompt.txt")
    metadata_path.write_text(prompt, encoding="utf-8")
    image.save(target_path, "PNG")


def generate_pollinations_image(brief, target_path):
    prompt = str(brief.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("The generated brief did not include an image prompt.")
    seed_source = f"{prompt}-{time.time()}".encode("utf-8")
    seed = int(hashlib.sha256(seed_source).hexdigest()[:8], 16)
    full_prompt = (
        f"{prompt}, transparent background, no mockup, no shirt model, no watermark, "
        "centered isolated print graphic, high detail, commercial t-shirt art"
    )
    url = (
        "https://image.pollinations.ai/prompt/"
        f"{quote(full_prompt, safe='')}?width={IMAGE_SIZE}&height={IMAGE_SIZE}"
        f"&seed={seed}&model={quote(POLLINATIONS_MODEL, safe='')}&nologo=true&private=true"
    )
    response = requests.get(url, timeout=180)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    if "image" not in content_type.lower():
        raise RuntimeError("Image provider did not return an image.")
    target_path.write_bytes(response.content)


def create_svg_file(brief, target_path):
    svg = sanitize_svg(brief.get("svg", ""))
    if not svg:
        raise RuntimeError("Ollama did not return a usable SVG.")
    target_path.write_text(svg, encoding="utf-8")


def generate_design(trigger="scheduled"):
    if not generation_lock.acquire(blocking=False):
        return {"status": "busy"}

    design_id = ""
    try:
        ensure_dirs()
        save_status(
            is_running=True,
            last_attempt_at_utc=iso_now(),
            current_message="Generating design brief with Ollama...",
            last_error="",
        )

        try:
            brief = ask_ollama_for_design()
        except Exception as exc:
            brief = fallback_brief()
            brief["ollama_warning"] = str(exc)

        if not brief:
            brief = fallback_brief()
            brief["ollama_warning"] = "Ollama returned an empty design brief."

        title = str(brief.get("title") or "T-Shirt Design").strip()[:90] or "T-Shirt Design"
        prompt = str(brief.get("prompt") or "").strip()
        if not prompt:
            prompt = fallback_brief()["prompt"]
            brief["prompt"] = prompt

        timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
        digest = hashlib.sha256(f"{timestamp}-{title}-{prompt}".encode("utf-8")).hexdigest()[:8]
        design_id = f"{timestamp}-{digest}"
        design_dir = DESIGNS_DIR / design_id
        design_dir.mkdir(parents=True, exist_ok=False)

        provider = IMAGE_PROVIDER
        filename = "design.svg" if provider == "ollama_svg" else "design.png"
        mime_type = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        status = "generated"
        error = ""

        save_status(is_running=True, current_message=f"Creating image with {provider}...")
        try:
            if provider == "pollinations":
                generate_pollinations_image(brief, design_dir / filename)
            elif provider == "prompt_card":
                render_prompt_card(brief, design_dir / filename)
            else:
                filename = "design.svg"
                mime_type = "image/svg+xml"
                create_svg_file(brief, design_dir / filename)
        except Exception as exc:
            filename = "design.png"
            mime_type = "image/png"
            status = "fallback"
            error = str(exc)
            render_prompt_card(brief, design_dir / filename)

        metadata = {
            "id": design_id,
            "title": title,
            "prompt": prompt,
            "palette": brief.get("palette") if isinstance(brief.get("palette"), list) else [],
            "tags": brief.get("tags") if isinstance(brief.get("tags"), list) else [],
            "provider": provider,
            "status": status,
            "error": error,
            "ollama_warning": brief.get("ollama_warning", ""),
            "filename": filename,
            "mime_type": mime_type,
            "created_at_utc": iso_now(),
            "trigger": trigger,
            "url": f"/designs/{design_id}/{filename}",
            "download_url": f"/designs/{design_id}/{filename}",
        }
        write_json(design_dir / "metadata.json", metadata)

        with store_lock:
            catalog = load_catalog()
            catalog.insert(0, metadata)
            save_catalog(catalog)

        save_status(
            is_running=False,
            last_success_at_utc=metadata["created_at_utc"],
            current_message=f"Created {title}.",
            last_error="",
        )
        return metadata
    except Exception as exc:
        save_status(
            is_running=False,
            current_message="Generation failed.",
            last_error=str(exc),
        )
        if design_id:
            try:
                shutil.rmtree(DESIGNS_DIR / design_id)
            except OSError:
                pass
        return {"status": "failed", "error": str(exc)}
    finally:
        generation_lock.release()


def should_generate(status):
    if status.get("is_running"):
        return False
    last_attempt = parse_iso(status.get("last_attempt_at_utc"))
    if not last_attempt:
        return True
    return utc_now() - last_attempt >= timedelta(seconds=GENERATE_INTERVAL_SECONDS)


def scheduler_loop():
    ensure_dirs()
    save_status(current_message="Idle")
    time.sleep(5)
    while True:
        try:
            if should_generate(load_status()):
                generate_design(trigger="scheduled")
        except Exception as exc:
            save_status(is_running=False, current_message="Scheduler error.", last_error=str(exc))
        time.sleep(min(60, max(10, GENERATE_INTERVAL_SECONDS // 12)))


def start_manual_generation():
    if generation_lock.locked():
        return False
    thread = threading.Thread(target=generate_design, kwargs={"trigger": "manual"}, daemon=True)
    thread.start()
    return True


def remove_design_folder(design_id):
    design_id = safe_id(design_id)
    base_dir = DESIGNS_DIR.resolve()
    target_dir = (DESIGNS_DIR / design_id).resolve()
    if os.path.commonpath([str(base_dir), str(target_dir)]) != str(base_dir):
        raise ValueError("Invalid design path.")
    if target_dir.exists():
        shutil.rmtree(target_dir)
    return design_id


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    status = load_status()
    status["interval_seconds"] = GENERATE_INTERVAL_SECONDS
    status["provider"] = IMAGE_PROVIDER
    status["model"] = OLLAMA_MODEL
    status["count"] = len(catalog_payload())
    last_attempt = parse_iso(status.get("last_attempt_at_utc"))
    if last_attempt:
        status["next_attempt_at_utc"] = (
            last_attempt + timedelta(seconds=GENERATE_INTERVAL_SECONDS)
        ).isoformat().replace("+00:00", "Z")
    else:
        status["next_attempt_at_utc"] = iso_now()
    return jsonify(status)


@app.route("/api/designs", methods=["GET"])
def api_designs():
    return jsonify(catalog_payload())


@app.route("/api/generate-now", methods=["POST"])
def api_generate_now():
    if not start_manual_generation():
        return jsonify({"status": "busy"}), 409
    return jsonify({"status": "queued"}), 202


@app.route("/api/designs/<design_id>", methods=["DELETE"])
def api_delete_design(design_id):
    try:
        design_id = remove_design_folder(design_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with store_lock:
        catalog = [item for item in load_catalog() if item.get("id") != design_id]
        save_catalog(catalog)
    return jsonify({"status": "ok", "deleted": design_id})


@app.route("/api/designs", methods=["DELETE"])
def api_delete_all_designs():
    deleted = 0
    with store_lock:
        catalog = load_catalog()
        for item in catalog:
            design_id = item.get("id")
            if not design_id:
                continue
            try:
                remove_design_folder(design_id)
                deleted += 1
            except (OSError, ValueError):
                continue
        save_catalog([])
    return jsonify({"status": "ok", "deleted": deleted})


@app.route("/designs/<design_id>/<filename>", methods=["GET"])
def design_file(design_id, filename):
    try:
        design_id = safe_id(design_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    safe_filename = os.path.basename(filename)
    target_dir = (DESIGNS_DIR / design_id).resolve()
    base_dir = DESIGNS_DIR.resolve()
    try:
        if os.path.commonpath([str(base_dir), str(target_dir)]) != str(base_dir):
            return jsonify({"error": "Invalid design path."}), 400
    except ValueError:
        return jsonify({"error": "Invalid design path."}), 400
    if not (target_dir / safe_filename).exists():
        return jsonify({"error": "File not found."}), 404
    return send_from_directory(target_dir, safe_filename, as_attachment=False)


ensure_dirs()
scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
scheduler_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3002, debug=False)
