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
- `TSHIRT_IMAGE_PROVIDER`: `ollama_svg` by default, which asks Ollama for a print-ready SVG. Use `prompt_card` for local generated PNG cards, or `pollinations` for a no-key external raster image attempt.
- `TSHIRT_GENERATE_INTERVAL_SECONDS`: schedule for the T-shirt generator, default `3600`.
- `TSHIRT_FRONTEND_PORT`: host port for the T-shirt design UI, default `3002`.

For server/CasaOS-style installs, `compose.casa.yml` defaults runtime data to `/DATA/AppData/ClipsCreator/...` and supports `LEAGUECLIPS_SOURCE_DIR` for pointing builds at a local clone. You can override the data root with `CLIPSCREATOR_APPDATA_DIR`.

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
