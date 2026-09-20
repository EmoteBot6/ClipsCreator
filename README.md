# League Clips Project

A Docker-based side project for turning League of Legends source videos into edited clips. It includes a backend API, Celery worker, Redis state, local Ollama-assisted analysis/subtitle workflows, clip-editing frontends, and a small hourly T-shirt design generator.

## What It Does

- Imports uploaded source videos or YouTube URLs with `yt-dlp`.
- Splits longer League clip compilations into individual clips.
- Edits single clips and centered mobile clips.
- Generates subtitles and clip metadata with local AI tooling.
- Provides a separate sitcom-style editor frontend.
- Generates T-shirt design artwork on an hourly schedule with the existing Ollama container.
- Stores generated media locally outside Git.

## Services

- `backend`: Flask API on port `5000`.
- `celery`: background worker for rendering and analysis jobs.
- `frontend`: main UI on port `3000`.
- `frontend_sitcom`: sitcom editor UI on port `3001`.
- `frontend_tshirts`: T-shirt design queue UI on port `3002`.
- `image_generator`: local Diffusers/Stable Diffusion image API on port `7860`.
- `redis`: task state/cache.
- `ollama`: local LLM server used by the AI-assisted steps.

## Requirements

- Docker and Docker Compose.
- Enough disk space for videos, render outputs, Whisper models, and Ollama models.
- Optional: an Ollama model matching `LEAGUECLIPS_OLLAMA_MODEL` from the compose file.

## Quick Start

```powershell
docker compose up --build
```

Then open:

- Main editor: <http://localhost:3000>
- Sitcom editor: <http://localhost:3001>
- T-shirt design queue: <http://localhost:3002>
- Backend health check: <http://localhost:5000/healthz>

Runtime data is written under `./data/` by default. That directory is intentionally ignored by Git.

## Configuration

Most settings are provided through environment variables in `docker-compose.yml`.

Common values to change:

- `LEAGUECLIPS_OLLAMA_MODEL`: Ollama model used for local analysis.
- `LEAGUECLIPS_AI_WHISPER_MODEL`: Whisper model size.
- `LEAGUECLIPS_AI_DEVICE`: `cpu` or a supported accelerator setup.
- `OLLAMA_HOST_PORT`: host port for the Ollama service.
- `TSHIRT_IMAGE_PROVIDER`: `local_diffusion` by default, which uses Ollama for a design brief and the local `image_generator` container for the raster image. Use `pollinations` only if you provide an API key, `ollama_svg` for simpler fully local SVG output, or `prompt_card` for local generated PNG cards.
- `TSHIRT_IMAGE_SIZE`: final raster export size, default `8000`; PNG outputs are kept at least this large on both axes.
- `TSHIRT_GENERATION_ENABLED`: initial generation setting, default `true`. The **Image creation On/Off** switch in the T-shirt UI saves your choice across restarts and takes precedence over this initial default. Off blocks both scheduled and manual generation; a design already in progress finishes normally.
- `TSHIRT_LOCAL_IMAGE_SIZE`: image size sent to the local image generator before export upscaling, default `1536`.
- `TSHIRT_LOCAL_IMAGE_FALLBACK_HOSTS`: comma-separated fallback image API hosts if Docker DNS cannot resolve `image_generator`, default `http://clips_image_generator:7860,http://host.docker.internal:7860`.
- `TSHIRT_LOCAL_IMAGE_STEPS`: inference steps sent to the local image generator, default `28`. Higher can improve quality and takes longer.
- `TSHIRT_LOCAL_IMAGE_GUIDANCE_SCALE`: prompt guidance for the local image generator, default `7.0`.
- `IMAGEGEN_MODEL`: local image model, default `stabilityai/stable-diffusion-xl-base-1.0`.
- `IMAGEGEN_DEVICE`: `auto`, `cpu`, `cuda`, or `mps`, default `auto`.
- `TSHIRT_POLLINATIONS_API_KEY`: required when `TSHIRT_IMAGE_PROVIDER=pollinations`; Pollinations now requires an API key with available Pollen credits.
- `TSHIRT_POLLINATIONS_MODEL`: image model for Pollinations, default `flux`.
- `TSHIRT_POLLINATIONS_IMAGE_SIZE`: Pollinations request size before local export upscaling, default `2048`.
- `TSHIRT_OLLAMA_NUM_PREDICT`: token budget for the design brief, default `6500`.
- `TSHIRT_OLLAMA_TEMPERATURE`: design brief creativity, default `0.85`.
- `TSHIRT_OLLAMA_DESIGN_ATTEMPTS`: number of unique-brief attempts before fallback, default `3`.
- `TSHIRT_RECENT_PROMPT_HISTORY`: number of recent prompts Ollama is told to avoid, default `12`.
- `TSHIRT_GENERATE_INTERVAL_SECONDS`: schedule for the T-shirt generator, default `3600`.
- `TSHIRT_FRONTEND_PORT`: host port for the T-shirt design UI, default `3002`.
- `TSHIRT_FRONTEND_URL`: optional public URL for the main editor's **T-Shirt Designs** link when using a reverse proxy. Otherwise the link uses the current hostname and `TSHIRT_FRONTEND_PORT`.

New PNG downloads are validated at a minimum of **8,000 × 8,000 pixels**, including fallback artwork. Diffusion models generate at their configured native size (default 1,536 × 1,536) and the export is upscaled with Lanczos; this increases pixel dimensions without adding native 8K model detail. Non-square artwork retains its proportions on a transparent square canvas. SVG designs use an 8,000 × 8,000 or larger viewport and remain scalable vectors. The gallery uses separate small previews and shows each new design's export dimensions. Existing saved designs are left as they are.

The T-shirt generator uses `TSHIRT_OLLAMA_MODEL`, default `qwen2.5:7b-instruct`. Pull that model in the Ollama container before expecting AI briefs:

```powershell
docker exec clips_ollama ollama pull qwen2.5:7b-instruct
```

The local image generator downloads its model weights on first use and stores them in the `image-generator` data volume. The default SDXL model is several gigabytes. CPU generation can be very slow; a GPU-capable Docker host is strongly recommended for high-quality hourly generation.

For server/CasaOS-style installs, `compose.casa.yml` defaults runtime data to `/DATA/AppData/ClipsCreator/...` and supports `LEAGUECLIPS_SOURCE_DIR` for pointing builds at a local clone. You can override the data root with `CLIPSCREATOR_APPDATA_DIR`.

## Automatic clip processing

The backend and Celery worker share `REDIS_URL` (default `redis://redis:6379/0`). Start both services: the backend alone cannot render clips. Worker concurrency defaults to `2` through `LEAGUECLIPS_WORKER_CONCURRENCY` to limit simultaneous video/AI workloads.

New jobs report `STARTED`, then `SPLIT_PREP` while reading the source and finding clip boundaries. Feed polling remains every 12 hours by default, while active jobs are checked every `LEAGUECLIPS_AUTO_SYNAPSE_TASK_POLL_SECONDS` (default `30`) seconds. Completed task results are retained for at least seven days.

After `LEAGUECLIPS_AUTO_SYNAPSE_PENDING_TIMEOUT_SECONDS` (default `300`) seconds in `PENDING`, the watcher checks workers and the Redis queue. It keeps jobs that are queued, reserved, running, or awaiting a worker response. If two complete checks find no trace of the job, it revokes the old ID and requeues the already downloaded source. An unavailable worker is shown as `waiting_for_worker`. Tasks without progress for the stale-task timeout are cancelled before their tracking is cleared.

For a stuck deployment, open **Open watcher diagnostics JSON** in the main UI, or inspect the services:

```powershell
docker compose ps
docker compose logs --tail=100 celery backend
```

After updating the source, apply the changes with:

```powershell
docker compose up -d --build
```

For CasaOS, use `docker compose -f compose.casa.yml up -d --build` instead. Existing model files remain in the same host data directory; the Ollama container now accesses that directory through `/data/.ollama` so its non-root user can reach it.

## Tests

```powershell
python -m pip install -r requirements-test.txt
python -m pytest -q tests
```

The regression suite covers generation settings, restart recovery, exported image dimensions, frontend scripts, task preparation, missing workers, and queued-job recovery. External AI providers, video rendering, and Redis/worker inspection are mocked; live Docker/model validation is separate. JavaScript syntax checks use Node when available. CI runs the tests and validates both Compose configurations before building service images.

## Public Repo Hygiene

This repository should only contain source, templates, Docker files, and small bundled assets. Generated videos, screenshots, Redis data, model caches, editor state, `.env` files, and local workspace files are ignored.

Before publishing, run a quick scan:

```powershell
git status --ignored --short
git ls-files
```

Make sure no local media, credentials, personal data, or generated caches are listed as tracked files.

## Notes

The project downloads and edits third-party video content. Make sure you have the rights to use any source videos you process or publish.
