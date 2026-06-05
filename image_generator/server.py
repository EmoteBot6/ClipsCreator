import io
import os
import random
import threading
import time

from flask import Flask, jsonify, request, send_file
from PIL import Image


app = Flask(__name__)

MODEL_ID = os.getenv("IMAGEGEN_MODEL", "stabilityai/stable-diffusion-xl-base-1.0")
DEVICE_SETTING = os.getenv("IMAGEGEN_DEVICE", "auto").strip().lower()
DTYPE_SETTING = os.getenv("IMAGEGEN_DTYPE", "auto").strip().lower()
DEFAULT_WIDTH = int(os.getenv("IMAGEGEN_WIDTH", "1024"))
DEFAULT_HEIGHT = int(os.getenv("IMAGEGEN_HEIGHT", "1024"))
DEFAULT_STEPS = int(os.getenv("IMAGEGEN_STEPS", "28"))
DEFAULT_GUIDANCE = float(os.getenv("IMAGEGEN_GUIDANCE_SCALE", "7.0"))
MAX_WIDTH = int(os.getenv("IMAGEGEN_MAX_WIDTH", "1536"))
MAX_HEIGHT = int(os.getenv("IMAGEGEN_MAX_HEIGHT", "1536"))
MODEL_CPU_OFFLOAD = os.getenv("IMAGEGEN_MODEL_CPU_OFFLOAD", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

pipeline = None
pipeline_lock = threading.Lock()
generation_lock = threading.Lock()
pipeline_status = {
    "loaded": False,
    "model": MODEL_ID,
    "device": "",
    "dtype": "",
    "last_error": "",
    "last_loaded_at": "",
}


def _load_torch():
    import torch

    return torch


def _select_device(torch):
    if DEVICE_SETTING != "auto":
        return DEVICE_SETTING
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _select_dtype(torch, device):
    if DTYPE_SETTING == "float16":
        return torch.float16
    if DTYPE_SETTING == "bfloat16":
        return torch.bfloat16
    if DTYPE_SETTING == "float32":
        return torch.float32
    if device == "cuda":
        return torch.float16
    return torch.float32


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_pipeline():
    global pipeline
    if pipeline is not None:
        return pipeline

    with pipeline_lock:
        if pipeline is not None:
            return pipeline

        torch = _load_torch()
        from diffusers import AutoPipelineForText2Image

        device = _select_device(torch)
        dtype = _select_dtype(torch, device)
        pipeline_status.update(
            {
                "loaded": False,
                "model": MODEL_ID,
                "device": device,
                "dtype": str(dtype).replace("torch.", ""),
                "last_error": "",
            }
        )

        try:
            pipe = AutoPipelineForText2Image.from_pretrained(
                MODEL_ID,
                torch_dtype=dtype,
                use_safetensors=True,
            )
            if device == "cuda" and MODEL_CPU_OFFLOAD:
                pipe.enable_model_cpu_offload()
            else:
                pipe = pipe.to(device)
            if hasattr(pipe, "enable_attention_slicing"):
                pipe.enable_attention_slicing()
            pipeline = pipe
            pipeline_status.update(
                {
                    "loaded": True,
                    "last_loaded_at": _now(),
                    "last_error": "",
                }
            )
            return pipeline
        except Exception as exc:
            pipeline_status.update({"loaded": False, "last_error": str(exc)})
            raise


def _clamp_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clamp_float(value, default, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok", **pipeline_status})


@app.route("/api/generate", methods=["POST"])
def generate():
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Missing prompt."}), 400

    negative_prompt = str(payload.get("negative_prompt") or "").strip()
    width = _clamp_int(payload.get("width"), DEFAULT_WIDTH, 512, MAX_WIDTH)
    height = _clamp_int(payload.get("height"), DEFAULT_HEIGHT, 512, MAX_HEIGHT)
    width = (width // 8) * 8
    height = (height // 8) * 8
    steps = _clamp_int(payload.get("steps"), DEFAULT_STEPS, 1, 80)
    guidance = _clamp_float(payload.get("guidance_scale"), DEFAULT_GUIDANCE, 0.0, 20.0)
    seed = payload.get("seed")
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = random.randint(0, 2**31 - 1)

    try:
        pipe = _load_pipeline()
    except Exception as exc:
        return jsonify({"error": "Could not load image model.", "details": str(exc)}), 503

    torch = _load_torch()
    device = pipeline_status.get("device") or _select_device(torch)
    generator_device = "cuda" if device == "cuda" else "cpu"
    generator = torch.Generator(device=generator_device).manual_seed(seed)

    with generation_lock:
        try:
            result = pipe(
                prompt=prompt,
                negative_prompt=negative_prompt or None,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=guidance,
                generator=generator,
            )
        except TypeError:
            result = pipe(
                prompt=prompt,
                width=width,
                height=height,
                num_inference_steps=steps,
                guidance_scale=guidance,
                generator=generator,
            )
        except Exception as exc:
            return jsonify({"error": "Image generation failed.", "details": str(exc)}), 500

    image = result.images[0]
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    response = send_file(buffer, mimetype="image/png", as_attachment=False, download_name="design.png")
    response.headers["X-Image-Seed"] = str(seed)
    response.headers["X-Image-Model"] = MODEL_ID
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=False, threaded=True)
