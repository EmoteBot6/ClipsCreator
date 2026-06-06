import hashlib
import html
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
OLLAMA_NUM_PREDICT = max(1024, int(os.getenv("TSHIRT_OLLAMA_NUM_PREDICT", "6500")))
OLLAMA_TEMPERATURE = float(os.getenv("TSHIRT_OLLAMA_TEMPERATURE", "0.85"))
GENERATE_INTERVAL_SECONDS = max(60, int(os.getenv("TSHIRT_GENERATE_INTERVAL_SECONDS", "3600")))
IMAGE_PROVIDER = os.getenv("TSHIRT_IMAGE_PROVIDER", "local_diffusion").strip().lower()
LOCAL_IMAGE_HOST = os.getenv("TSHIRT_LOCAL_IMAGE_HOST", "http://image_generator:7860").rstrip("/")
LOCAL_IMAGE_FALLBACK_HOSTS = os.getenv(
    "TSHIRT_LOCAL_IMAGE_FALLBACK_HOSTS",
    "http://clips_image_generator:7860,http://host.docker.internal:7860,http://localhost:7860",
)
LOCAL_IMAGE_TIMEOUT_SECONDS = int(os.getenv("TSHIRT_LOCAL_IMAGE_TIMEOUT_SECONDS", "3600"))
LOCAL_IMAGE_SIZE = max(512, int(os.getenv("TSHIRT_LOCAL_IMAGE_SIZE", "1536")))
LOCAL_IMAGE_STEPS = max(1, int(os.getenv("TSHIRT_LOCAL_IMAGE_STEPS", "28")))
LOCAL_IMAGE_GUIDANCE_SCALE = float(os.getenv("TSHIRT_LOCAL_IMAGE_GUIDANCE_SCALE", "7.0"))
POLLINATIONS_API_KEY = os.getenv("TSHIRT_POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_BASE_URL = os.getenv("TSHIRT_POLLINATIONS_BASE_URL", "https://gen.pollinations.ai").rstrip("/")
POLLINATIONS_MODEL = os.getenv("TSHIRT_POLLINATIONS_MODEL", "flux")
POLLINATIONS_IMAGE_SIZE = max(768, int(os.getenv("TSHIRT_POLLINATIONS_IMAGE_SIZE", "2048")))
MAX_DESIGNS = max(1, int(os.getenv("TSHIRT_MAX_DESIGNS", "80")))
MIN_EXPORT_IMAGE_SIZE = 8000
IMAGE_SIZE = max(MIN_EXPORT_IMAGE_SIZE, int(os.getenv("TSHIRT_IMAGE_SIZE", str(MIN_EXPORT_IMAGE_SIZE))))
RECENT_PROMPT_HISTORY = max(0, int(os.getenv("TSHIRT_RECENT_PROMPT_HISTORY", "12")))
OLLAMA_DESIGN_ATTEMPTS = max(1, int(os.getenv("TSHIRT_OLLAMA_DESIGN_ATTEMPTS", "3")))

store_lock = threading.Lock()
generation_lock = threading.Lock()

SUBJECT_LANES = [
    "mythic animal emblem",
    "retro space souvenir patch",
    "botanical machine hybrid",
    "stormy mountain expedition graphic",
    "surreal ocean creature crest",
    "desert night motorcycle badge",
    "cyberpunk street market icon",
    "ancient astronomy diagram",
    "skate-zine comic mascot",
    "geometric jungle scene",
    "vintage workwear tool insignia",
    "abstract music festival poster mark",
    "deep-sea exploration symbol",
    "minimalist martial arts dojo crest",
    "dreamlike city skyline print",
    "folk-art firebird illustration",
]
COMPOSITION_LANES = [
    "large centered crest with small orbiting details",
    "stacked poster composition with bold foreground silhouette",
    "circular badge with layered interior scene",
    "diagonal motion composition with speed lines",
    "symmetrical mascot framed by decorative shapes",
    "single oversized icon with subtle texture fields",
    "split sun-and-shadow composition",
    "arched souvenir graphic with a strong base banner",
]
STYLE_LANES = [
    "risograph ink texture",
    "clean vector streetwear",
    "1970s outdoor catalog illustration",
    "Japanese woodblock inspired linework",
    "neo-vintage tattoo flash",
    "bold comic screen print",
    "minimal Swiss poster geometry",
    "hand-inked zine illustration",
]
COLOR_LANES = [
    "bone white, graphite black, teal, and amber",
    "cream, oxblood, navy, and dusty cyan",
    "charcoal, electric blue, hot coral, and pale yellow",
    "forest green, ivory, copper, and midnight blue",
    "black, white, acid green, and safety orange",
    "deep purple, mint, warm gray, and rose red",
]


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


def recent_design_history(limit=None):
    if limit is None:
        limit = RECENT_PROMPT_HISTORY
    if limit <= 0:
        return []
    with store_lock:
        catalog = load_catalog()
    history = []
    for item in catalog[:limit]:
        title = str(item.get("title") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not title and not prompt:
            continue
        history.append({"title": title, "prompt": prompt})
    return history


def prompt_signature(value):
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:360]


def is_duplicate_prompt(prompt, history):
    signature = prompt_signature(prompt)
    if not signature:
        return False
    return any(signature == prompt_signature(item.get("prompt", "")) for item in history)


def creative_constraints(seed):
    rng = random.Random(seed)
    return {
        "subject": rng.choice(SUBJECT_LANES),
        "composition": rng.choice(COMPOSITION_LANES),
        "style": rng.choice(STYLE_LANES),
        "colors": rng.choice(COLOR_LANES),
    }


def history_text(history):
    if not history:
        return "No previous designs are saved yet."
    lines = []
    for index, item in enumerate(history[:RECENT_PROMPT_HISTORY], start=1):
        title = str(item.get("title") or "Untitled").strip()
        prompt = str(item.get("prompt") or "").strip()
        if len(prompt) > 220:
            prompt = f"{prompt[:220]}..."
        lines.append(f"{index}. {title}: {prompt}")
    return "\n".join(lines)


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


def ask_ollama_for_design(history=None, attempt=1):
    history = history or []
    seed_source = f"{time.time()}-{random.getrandbits(64)}-{attempt}".encode("utf-8")
    seed = int(hashlib.sha256(seed_source).hexdigest()[:8], 16)
    constraints = creative_constraints(seed)
    prompt = f"""
Create one original print-ready T-shirt design concept as a senior apparel graphic designer.
Return only a JSON object, with no Markdown.

This is generation attempt {attempt}.
Creative seed for this run: {seed}

Recent saved designs to avoid:
{history_text(history)}

Novelty requirements:
- Do not reuse the same subject, scene, title, wording, or prompt structure as any recent saved design.
- Make the new concept clearly different enough that a buyer would describe it as a separate idea.
- Use the run-specific direction below even if you have a favorite default concept.

Run-specific direction:
- subject lane: {constraints["subject"]}
- composition lane: {constraints["composition"]}
- art style lane: {constraints["style"]}
- color lane: {constraints["colors"]}

The JSON must have:
- title: 3 to 8 words
- prompt: a detailed text-to-image prompt for a premium T-shirt print graphic
- palette: 3 to 6 hex colors
- tags: 3 to 7 short lowercase tags
- svg: optional complete self-contained SVG illustration for the shirt front

Design direction:
- create a distinctive central graphic, not a generic logo
- prioritize bold silhouettes, strong composition, crisp edges, and screen-printable contrast
- include style, subject, composition, texture, color palette, and print constraints in the prompt
- avoid mockups, blank shirts, models, hangers, storefront photos, watermarks, copyrighted characters, brand names, and team logos
- keep words out of the artwork unless they are short and generic

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
                "temperature": OLLAMA_TEMPERATURE,
                "num_predict": OLLAMA_NUM_PREDICT,
                "top_p": 0.92,
                "repeat_penalty": 1.16,
                "seed": seed,
            },
        },
        timeout=OLLAMA_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        details = response.text.strip()
        try:
            parsed = response.json()
            if isinstance(parsed, dict) and parsed.get("error"):
                details = str(parsed["error"])
        except ValueError:
            pass
        if len(details) > 500:
            details = details[:500]
        raise RuntimeError(
            f"Ollama request failed ({response.status_code}) for model {OLLAMA_MODEL}: {details}"
        )
    payload = response.json()
    return normalize_ollama_payload(payload.get("response", ""))


def fallback_brief(seed=None):
    rng = random.Random(seed if seed is not None else random.getrandbits(64))
    themes = [
        ("Midnight Circuit Bloom", "a futuristic botanical circuit-board flower emblem"),
        ("Solar Drift Club", "a retro sun, drifting clouds, and bold geometric waves"),
        ("Quiet Thunder", "a minimalist lightning crest with layered ink texture"),
        ("Neon Workshop", "a clean vaporwave tool badge with abstract sparks"),
        ("Orbit Motel", "a space-travel souvenir patch with moons and crisp line art"),
        ("Iron Orchard", "a mechanical apple tree with gears, leaves, and clean workwear lines"),
        ("Lantern Tide", "a glowing coastal lantern surrounded by stylized waves and stars"),
        ("Ghost Signal Radio", "a vintage radio transmitting abstract spectral sound waves"),
        ("Canyon Night Run", "a desert road badge with a moonlit canyon and bold tire tracks"),
        ("Paper Tiger Relay", "an origami tiger mascot with sharp fold lines and racing motion"),
        ("Cloud Forge", "a blacksmith anvil surrounded by storm clouds and geometric sparks"),
        ("Orbit Garden", "a greenhouse dome floating among planets and botanical linework"),
        ("Static Rodeo", "a surreal lightning horse with western poster typography shapes"),
    ]
    title, idea = rng.choice(themes)
    palette = rng.choice(
        [
            ["#f8fafc", "#111827", "#14b8a6", "#f59e0b"],
            ["#fff7ed", "#172554", "#ef4444", "#22c55e"],
            ["#fefce8", "#18181b", "#38bdf8", "#fb7185"],
            ["#f8fafc", "#27272a", "#a3e635", "#f97316"],
            ["#ecfeff", "#1e1b4b", "#fb7185", "#facc15"],
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


def normalized_palette(brief):
    colors = [
        str(color).strip()
        for color in brief.get("palette") or []
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(color).strip())
    ]
    defaults = ["#f8fafc", "#111827", "#14b8a6", "#f59e0b", "#ef4444"]
    while len(colors) < 5:
        colors.append(defaults[len(colors)])
    return colors[:5]


def generated_svg_from_brief(brief):
    palette = normalized_palette(brief)
    title = str(brief.get("title") or "T-Shirt Design").strip()[:44] or "T-Shirt Design"
    prompt = str(brief.get("prompt") or title).strip()
    tags = [
        re.sub(r"[^a-z0-9 -]", "", str(tag).lower()).strip()
        for tag in (brief.get("tags") or [])
    ]
    tags = [tag for tag in tags if tag][:4] or ["print", "vector", "shirt"]
    seed = int(hashlib.sha256(f"{title}-{prompt}".encode("utf-8")).hexdigest()[:8], 16)
    variant = seed % 4
    angle = 18 + (seed % 18)
    title_xml = html.escape(title.upper())
    tag_xml = html.escape(" / ".join(tags).upper())

    p0, p1, p2, p3, p4 = palette
    motif = ""
    if variant == 0:
        motif = f"""
  <g transform="translate(1024 910)">
    <circle r="520" fill="none" stroke="{p1}" stroke-width="42"/>
    <circle r="385" fill="{p0}" stroke="{p2}" stroke-width="30"/>
    <path d="M0 -560 L92 -176 L480 -300 L206 28 L430 380 L0 220 L-430 380 L-206 28 L-480 -300 L-92 -176 Z" fill="{p2}"/>
    <circle r="164" fill="{p3}"/>
    <circle r="76" fill="{p1}"/>
  </g>
"""
    elif variant == 1:
        motif = f"""
  <g transform="translate(1024 900) rotate(-{angle})">
    <path d="M-420 -500 C-120 -650 190 -600 440 -340 C210 -270 106 -156 62 0 C260 -80 442 -18 560 160 C292 190 160 304 116 526 C-100 334 -292 284 -562 332 C-410 120 -386 -80 -420 -500 Z" fill="{p2}" stroke="{p1}" stroke-width="34" stroke-linejoin="round"/>
    <path d="M-250 -280 C-46 -360 190 -312 332 -154 C112 -126 -12 -10 -82 186" fill="none" stroke="{p0}" stroke-width="42" stroke-linecap="round"/>
    <path d="M-112 310 C40 210 168 194 316 250" fill="none" stroke="{p3}" stroke-width="38" stroke-linecap="round"/>
  </g>
"""
    elif variant == 2:
        motif = f"""
  <g transform="translate(1024 900)">
    <path d="M-610 36 C-420 -240 -220 -380 0 -380 C220 -380 420 -240 610 36 C418 282 220 404 0 404 C-220 404 -418 282 -610 36 Z" fill="{p1}"/>
    <path d="M-460 40 C-290 -140 -142 -224 0 -224 C142 -224 290 -140 460 40 C288 182 138 250 0 250 C-138 250 -288 182 -460 40 Z" fill="{p0}"/>
    <circle r="176" fill="{p2}"/>
    <path d="M-760 -210 L-560 -150 M560 -150 L760 -210 M-760 270 L-560 196 M560 196 L760 270" stroke="{p3}" stroke-width="44" stroke-linecap="round"/>
  </g>
"""
    else:
        motif = f"""
  <g transform="translate(1024 900)">
    <rect x="-410" y="-410" width="820" height="820" rx="118" fill="{p1}" transform="rotate(45)"/>
    <rect x="-300" y="-300" width="600" height="600" rx="86" fill="{p0}" transform="rotate(45)"/>
    <path d="M-72 -418 L256 -72 L72 -72 L72 418 L-256 72 L-72 72 Z" fill="{p2}" stroke="{p1}" stroke-width="24" stroke-linejoin="round"/>
    <circle cx="-310" cy="-310" r="74" fill="{p3}"/>
    <circle cx="310" cy="310" r="74" fill="{p4}"/>
  </g>
"""

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2048 2048">
  <desc>{html.escape(prompt[:420])}</desc>
  <defs>
    <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="26" stdDeviation="22" flood-color="#000000" flood-opacity="0.20"/>
    </filter>
  </defs>
  <g filter="url(#softShadow)">
    <path d="M1024 150 C1362 150 1628 346 1744 648 C1864 960 1750 1292 1484 1500 C1254 1680 832 1680 602 1500 C256 1228 218 734 448 438 C570 280 780 150 1024 150 Z" fill="{p0}" opacity="0.96"/>
    <path d="M408 1398 C674 1520 1288 1520 1554 1398" fill="none" stroke="{p1}" stroke-width="44" stroke-linecap="round"/>
{motif}
    <text x="1024" y="1698" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="116" font-weight="900" fill="{p1}">{title_xml}</text>
    <text x="1024" y="1792" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="40" font-weight="700" letter-spacing="8" fill="{p2}">{tag_xml}</text>
  </g>
</svg>
"""


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


def local_image_hosts():
    hosts = []
    for raw_host in [LOCAL_IMAGE_HOST, *LOCAL_IMAGE_FALLBACK_HOSTS.split(",")]:
        host = raw_host.strip().rstrip("/")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def post_local_image(payload):
    hosts = local_image_hosts()
    last_error = None
    for host in hosts:
        try:
            return requests.post(
                f"{host}/api/generate",
                json=payload,
                timeout=LOCAL_IMAGE_TIMEOUT_SECONDS,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout) as exc:
            last_error = exc
    tried = ", ".join(hosts)
    raise RuntimeError(
        f"Could not connect to the local image generator. Tried: {tried}. Last error: {last_error}"
    )


def resample_filter():
    if hasattr(Image, "Resampling"):
        return Image.Resampling.LANCZOS
    return Image.LANCZOS


def ensure_export_image_size(target_path):
    with Image.open(target_path) as source:
        width, height = source.size
        target_edge = max(IMAGE_SIZE, width, height)
        if source.format == "PNG" and width == target_edge and height == target_edge:
            return
        image = source.convert("RGBA")

    scale = min(target_edge / image.width, target_edge / image.height)
    resized_size = (
        max(1, int(round(image.width * scale))),
        max(1, int(round(image.height * scale))),
    )
    if image.size != resized_size:
        image = image.resize(resized_size, resample_filter())

    if image.size != (target_edge, target_edge):
        canvas = Image.new("RGBA", (target_edge, target_edge), (0, 0, 0, 0))
        offset = (
            (target_edge - image.width) // 2,
            (target_edge - image.height) // 2,
        )
        canvas.alpha_composite(image, offset)
        image = canvas

    image.save(target_path, "PNG")


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
    if not POLLINATIONS_API_KEY:
        raise RuntimeError(
            "Pollinations now requires an API key with available Pollen. "
            "Set TSHIRT_POLLINATIONS_API_KEY or use TSHIRT_IMAGE_PROVIDER=ollama_svg."
        )
    prompt = str(brief.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("The generated brief did not include an image prompt.")
    seed_source = f"{prompt}-{time.time()}".encode("utf-8")
    seed = int(hashlib.sha256(seed_source).hexdigest()[:8], 16)
    full_prompt = (
        f"{prompt}, award winning apparel illustration, isolated centered t-shirt print artwork, "
        "transparent or plain background, no shirt mockup, no blank shirt, no person, no hanger, "
        "no watermark, no brand logo, no copyrighted character, crisp vector-like edges, bold silhouette, "
        "screen print friendly, premium streetwear graphic, high detail, high contrast, print ready"
    )
    url = (
        f"{POLLINATIONS_BASE_URL}/image/"
        f"{quote(full_prompt, safe='')}?width={POLLINATIONS_IMAGE_SIZE}&height={POLLINATIONS_IMAGE_SIZE}"
        f"&seed={seed}&model={quote(POLLINATIONS_MODEL, safe='')}&enhance=true"
    )
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"},
        timeout=300,
    )
    if response.status_code >= 400:
        details = response.text.strip()
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict) and error.get("message"):
                    details = str(error["message"])
                elif error:
                    details = str(error)
        except ValueError:
            pass
        if response.status_code == 402:
            details = details or "Insufficient Pollen balance or API key budget exhausted."
        if len(details) > 500:
            details = details[:500]
        raise RuntimeError(f"Pollinations request failed ({response.status_code}): {details}")
    content_type = response.headers.get("Content-Type", "")
    if "image" not in content_type.lower():
        raise RuntimeError("Image provider did not return an image.")
    target_path.write_bytes(response.content)
    ensure_export_image_size(target_path)


def generate_local_diffusion_image(brief, target_path):
    prompt = str(brief.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("The generated brief did not include an image prompt.")
    seed_source = f"{prompt}-{time.time()}".encode("utf-8")
    seed = int(hashlib.sha256(seed_source).hexdigest()[:8], 16)
    full_prompt = (
        f"{prompt}, award winning apparel illustration, isolated centered t-shirt print artwork, "
        "transparent or plain background, no shirt mockup, no blank shirt, no person, no hanger, "
        "no watermark, no brand logo, no copyrighted character, crisp vector-like edges, bold silhouette, "
        "screen print friendly, premium streetwear graphic, high detail, high contrast, print ready"
    )
    negative_prompt = (
        "shirt mockup, blank shirt, person, model, mannequin, hanger, watermark, signature, logo, "
        "copyrighted character, brand name, blurry, low contrast, muddy colors, photorealistic clothing photo, "
        "cropped artwork, extra text, misspelled text"
    )
    response = post_local_image(
        {
            "prompt": full_prompt,
            "negative_prompt": negative_prompt,
            "width": LOCAL_IMAGE_SIZE,
            "height": LOCAL_IMAGE_SIZE,
            "steps": LOCAL_IMAGE_STEPS,
            "guidance_scale": LOCAL_IMAGE_GUIDANCE_SCALE,
            "seed": seed,
        }
    )
    if response.status_code >= 400:
        details = response.text.strip()
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                details = parsed.get("details") or parsed.get("error") or details
        except ValueError:
            pass
        if len(str(details)) > 500:
            details = str(details)[:500]
        raise RuntimeError(f"Local image generator failed at {response.url} ({response.status_code}): {details}")
    content_type = response.headers.get("Content-Type", "")
    if "image" not in content_type.lower():
        raise RuntimeError("Local image generator did not return an image.")
    target_path.write_bytes(response.content)
    ensure_export_image_size(target_path)


def create_svg_file(brief, target_path):
    svg = sanitize_svg(brief.get("svg", ""))
    if not svg:
        svg = generated_svg_from_brief(brief)
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

        recent_history = recent_design_history()
        brief = {}
        duplicate_warnings = []
        for attempt in range(1, OLLAMA_DESIGN_ATTEMPTS + 1):
            try:
                brief = ask_ollama_for_design(recent_history, attempt=attempt)
            except Exception as exc:
                brief = fallback_brief(seed=f"{time.time()}-{attempt}")
                brief["ollama_warning"] = str(exc)
                break

            if not brief:
                duplicate_warnings.append(f"Attempt {attempt}: Ollama returned an empty design brief.")
                continue

            candidate_prompt = str(brief.get("prompt") or "").strip()
            if not is_duplicate_prompt(candidate_prompt, recent_history):
                break

            duplicate_warnings.append(f"Attempt {attempt}: duplicate prompt rejected.")
            recent_history.insert(
                0,
                {
                    "title": str(brief.get("title") or "").strip(),
                    "prompt": candidate_prompt,
                },
            )
            brief = {}

        if not brief:
            brief = fallback_brief(seed=f"{time.time()}-unique-fallback")
            brief["ollama_warning"] = "Ollama did not produce a unique design brief."

        if duplicate_warnings:
            existing_warning = str(brief.get("ollama_warning") or "").strip()
            joined_warnings = " ".join(duplicate_warnings)
            brief["ollama_warning"] = f"{existing_warning} {joined_warnings}".strip()

        title = str(brief.get("title") or "T-Shirt Design").strip()[:90] or "T-Shirt Design"
        prompt = str(brief.get("prompt") or "").strip()
        if not prompt:
            prompt = fallback_brief(seed=f"{time.time()}-missing-prompt")["prompt"]
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
            elif provider == "local_diffusion":
                generate_local_diffusion_image(brief, design_dir / filename)
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
