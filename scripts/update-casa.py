#!/usr/bin/env python3
"""Update an existing ClipsCreator Compose/CasaOS app using published images.

Requires Python 3 and Docker Compose v2. No Python packages or Git clone needed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


IMAGE_NAMES = {
    "backend": "clipscreator-backend",
    "celery": "clipscreator-backend",
    "frontend": "clipscreator-frontend",
    "frontend_sitcom": "clipscreator-frontend-sitcom",
    "frontend_tshirts": "clipscreator-frontend-tshirts",
    "image_generator": "clipscreator-image-generator",
}
OVERRIDE_NAME = "compose.clipscreator-images.json"
OWNER_KEY = "x-clipscreator-image-updater"


def run(arguments, cwd=None, capture=False):
    result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=capture)
    if result.returncode:
        details = result.stderr.strip() if capture else "See command output above."
        raise RuntimeError(f"{arguments[0]} command failed ({result.returncode}): {details}")
    return result.stdout if capture else ""


def labelled_paths(value):
    return [Path(part).resolve() for part in value.split(",") if part]


def find_deployment(container):
    containers = json.loads(run(["docker", "inspect", container], capture=True))
    labels = containers[0].get("Config", {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    files = labelled_paths(labels.get("com.docker.compose.project.config_files", ""))
    if not project or not files:
        raise RuntimeError("The existing container has no Compose project/file labels. Use the Compose file managed by CasaOS.")
    for path in files:
        if not path.is_file():
            raise RuntimeError(f"The installed Compose file is missing: {path}")
    directory = Path(labels.get("com.docker.compose.project.working_dir") or files[0].parent).resolve()
    if not directory.is_dir():
        raise RuntimeError(f"The installed project directory is missing: {directory}")
    env_files = labelled_paths(labels.get("com.docker.compose.project.environment_file", ""))
    for path in env_files:
        if not path.is_file():
            raise RuntimeError(f"The installed environment file is missing: {path}")
    return project, files, directory, env_files


def compose_command(project, files, directory, env_files):
    command = ["docker", "compose", "--project-name", project, "--project-directory", str(directory)]
    for path in env_files:
        command.extend(["--env-file", str(path)])
    for path in files:
        command.extend(["--file", str(path)])
    return command


def read_config(command, directory):
    return json.loads(run(command + ["config", "--format", "json"], cwd=directory, capture=True))


def update(container="clips_backend", tag=None, dry_run=False):
    if tag is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise ValueError("Invalid image tag. Use latest, a release tag, or sha-<commit>.")
    run(["docker", "compose", "version"], capture=True)
    project, files, directory, env_files = find_deployment(container)
    override = files[0].parent / OVERRIDE_NAME
    if override.exists():
        previous = json.loads(override.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get(OWNER_KEY) != 1:
            raise RuntimeError(f"Refusing to overwrite an unrelated file: {override}")
    # Compose records every -f path in the containers. Replace our prior overlay
    # instead of appending it repeatedly on subsequent updates.
    base_files = [path for path in files if path != override]
    if not base_files:
        raise RuntimeError("The original installed Compose file is missing.")
    command = compose_command(project, base_files, directory, env_files)
    services = read_config(command, directory).get("services", {})
    if not {"backend", "celery", "frontend"}.issubset(services):
        raise RuntimeError("This project does not contain the expected backend, celery and frontend services.")
    selected = [service for service in IMAGE_NAMES if service in services]
    for service in selected:
        for volume in services[service].get("volumes", []):
            target = volume.get("target", "")
            if volume.get("type") == "bind" and (
                target in {"/app", "/workspace"} or target.startswith(("/app/", "/workspace/"))
            ):
                raise RuntimeError(f"{service} mounts source code over the image at {target}. Use compose.casa.yml for image-based deployment.")

    image_tag = tag if tag is not None else "${CLIPS_IMAGE_TAG:-latest}"
    overlay = {
        OWNER_KEY: 1,
        "services": {
            service: {"image": f"ghcr.io/emotebot6/{IMAGE_NAMES[service]}:{image_tag}"}
            for service in selected
        },
    }
    print(f"Updating existing project: {project}", flush=True)
    print(f"Using installed configuration: {', '.join(str(path) for path in base_files)}", flush=True)
    missing = set(IMAGE_NAMES) - set(selected)
    if missing:
        print(
            f"Services absent from this installation: {', '.join(sorted(missing))}. "
            "Import the updated compose.casa.yml into this existing CasaOS app once to add them.",
            flush=True,
        )

    candidate = None
    try:
        # The candidate is used only for validation; running containers must
        # reference the persistent overlay so the next update can find it.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", prefix=".clips-images-", dir=override.parent, delete=False) as handle:
            candidate = Path(handle.name)
            json.dump(overlay, handle, indent=2)
            handle.write("\n")
        check_command = compose_command(project, base_files + [candidate], directory, env_files)
        resolved = read_config(check_command, directory)
        images = list(dict.fromkeys(resolved["services"][service]["image"] for service in selected))
        for image in images:
            print(f"{'Would pull' if dry_run else 'Pulling'} {image}", flush=True)
            if not dry_run:
                # Pull explicitly: Compose can tolerate a failed pull when the
                # installed service still has a legacy build: section.
                run(["docker", "image", "pull", image], cwd=directory)
        if dry_run:
            print("Dry run complete. No images pulled or containers changed.", flush=True)
            return
        os.replace(candidate, override)
        candidate = None
        command = compose_command(project, base_files + [override], directory, env_files)
        run(command + ["up", "-d", "--no-build", "--pull", "never", *selected], cwd=directory)
        run(command + ["ps"], cwd=directory)
        print("Update applied. Check the service status above and open the app.", flush=True)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="clips_backend", help="Existing backend container name")
    parser.add_argument("--tag", help="Published image tag; default: CLIPS_IMAGE_TAG from Compose's environment, or latest")
    parser.add_argument("--dry-run", action="store_true", help="Show the target deployment and images without updating")
    args = parser.parse_args()
    try:
        update(args.container, args.tag, args.dry_run)
    except (OSError, ValueError, KeyError, IndexError, RuntimeError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
