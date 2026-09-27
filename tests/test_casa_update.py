import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("casa_updater", ROOT / "scripts" / "update-casa.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    directory = tmp_path / "Existing Casa App"
    directory.mkdir()
    compose = directory / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    env_file = directory / "server settings.env"
    env_file.write_text("CLIPS_IMAGE_TAG=sha-1234567\n", encoding="utf-8")
    services = {
        service: {
            "build": {"context": str(directory / service)},
            "volumes": [{"type": "bind", "source": "/my/custom/data", "target": "/data"}],
            "environment": {"KEEP_MY_SETTING": "original"},
        }
        for service in updater.IMAGE_NAMES
    }
    services["redis"] = {"image": "redis:7-alpine"}
    services["ollama"] = {"image": "ollama/ollama:latest"}
    labels = {
        "com.docker.compose.project": "my-casa-app",
        "com.docker.compose.project.config_files": str(compose),
        "com.docker.compose.project.working_dir": str(directory),
        "com.docker.compose.project.environment_file": str(env_file),
    }
    calls = []
    configs = []
    fail_pull = []

    def run(arguments, cwd=None, capture=False):
        calls.append((arguments, cwd))
        if arguments[:2] == ["docker", "inspect"]:
            return json.dumps([{"Config": {"Labels": labels}}])
        if arguments[-3:] == ["config", "--format", "json"]:
            config = {"services": copy.deepcopy(services)}
            paths = [Path(arguments[i + 1]) for i, arg in enumerate(arguments) if arg == "--file"]
            for path in paths:
                if path == compose:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                for name, values in payload["services"].items():
                    image = values["image"].replace("${CLIPS_IMAGE_TAG:-latest}", "sha-1234567")
                    config["services"][name]["image"] = image
            configs.append(config)
            return json.dumps(config)
        if arguments[:3] == ["docker", "image", "pull"] and fail_pull:
            raise RuntimeError("Registry unavailable")
        return ""

    monkeypatch.setattr(updater, "run", run)
    return SimpleNamespace(
        directory=directory, compose=compose, env_file=env_file, services=services,
        labels=labels, calls=calls, configs=configs, fail_pull=fail_pull,
        override=directory / updater.OVERRIDE_NAME,
    )


def test_update_reuses_existing_project_files_environment_and_data(deployment):
    original = deployment.compose.read_bytes()
    updater.update()
    assert deployment.compose.read_bytes() == original
    overlay = json.loads(deployment.override.read_text())
    assert overlay[updater.OWNER_KEY] == 1
    assert all(set(value) == {"image"} for value in overlay["services"].values())
    for service in updater.IMAGE_NAMES:
        assert deployment.configs[-1]["services"][service]["volumes"] == deployment.services[service]["volumes"]
        assert deployment.configs[-1]["services"][service]["environment"] == {"KEEP_MY_SETTING": "original"}
    pulls = [args for args, _ in deployment.calls if args[:3] == ["docker", "image", "pull"]]
    assert len(pulls) == 5  # Backend and Celery share one image.
    assert all(args[-1].startswith("ghcr.io/emotebot6/") and args[-1].endswith(":sha-1234567") for args in pulls)
    up, cwd = next((args, cwd) for args, cwd in deployment.calls if "up" in args)
    assert cwd == deployment.directory
    assert up[up.index("--project-name") + 1] == "my-casa-app"
    assert up[up.index("--env-file") + 1] == str(deployment.env_file)
    assert up[up.index("up"):] == ["up", "-d", "--no-build", "--pull", "never", *updater.IMAGE_NAMES]
    assert str(deployment.override) in up
    assert not list(deployment.directory.glob(".clips-images-*"))


def test_pull_failure_does_not_recreate_containers_or_replace_overlay(deployment):
    previous = {updater.OWNER_KEY: 1, "services": {}}
    deployment.override.write_text(json.dumps(previous))
    deployment.fail_pull.append(True)
    with pytest.raises(RuntimeError, match="Registry unavailable"):
        updater.update()
    assert json.loads(deployment.override.read_text()) == previous
    assert not any("up" in args for args, _ in deployment.calls)
    assert not list(deployment.directory.glob(".clips-images-*"))


def test_dry_run_does_not_pull_or_persist_overlay(deployment, capsys):
    updater.update(dry_run=True)
    assert not deployment.override.exists()
    assert not any("pull" in args or "up" in args for args, _ in deployment.calls)
    assert "Would pull ghcr.io/emotebot6/" in capsys.readouterr().out


def test_repeated_update_does_not_duplicate_override_file(deployment):
    updater.update()
    deployment.labels["com.docker.compose.project.config_files"] += "," + str(deployment.override)
    deployment.calls.clear()
    updater.update()
    up = next(args for args, _ in deployment.calls if "up" in args)
    assert up.count(str(deployment.override)) == 1
    assert up.count(str(deployment.compose)) == 1


def test_missing_optional_services_are_not_created_with_guessed_settings(deployment, capsys):
    del deployment.services["frontend_tshirts"]
    del deployment.services["image_generator"]
    updater.update()
    overlay = json.loads(deployment.override.read_text())
    assert "frontend_tshirts" not in overlay["services"]
    assert "image_generator" not in overlay["services"]
    assert "Import the updated compose.casa.yml" in capsys.readouterr().out


def test_explicit_tag_is_applied_to_all_app_images(deployment):
    updater.update(tag="1.2.3")
    images = json.loads(deployment.override.read_text())["services"].values()
    assert all(item["image"].endswith(":1.2.3") for item in images)


@pytest.mark.parametrize("tag", ["bad/tag", "tag;command", "two words", "-latest", ""])
def test_invalid_tag_is_rejected_before_docker_access(deployment, tag):
    with pytest.raises(ValueError, match="Invalid image tag"):
        updater.update(tag=tag)
    assert not deployment.calls


def test_missing_original_compose_file_does_not_start_a_new_project(deployment):
    deployment.labels["com.docker.compose.project.config_files"] = str(deployment.directory / "missing.yml")
    with pytest.raises(RuntimeError, match="Compose file is missing"):
        updater.update()
    assert not any("pull" in args or "up" in args for args, _ in deployment.calls)


def test_existing_unrelated_override_is_not_overwritten(deployment):
    deployment.override.write_text('{"services": {}}')
    with pytest.raises(RuntimeError, match="unrelated file"):
        updater.update()
    assert deployment.override.read_text() == '{"services": {}}'


def test_source_mounts_cannot_silently_hide_the_new_image_code(deployment):
    deployment.services["backend"]["volumes"].append({"type": "bind", "source": "/source", "target": "/workspace"})
    with pytest.raises(RuntimeError, match="mounts source code"):
        updater.update()
    assert not any("pull" in args for args, _ in deployment.calls)


def test_casa_images_match_the_github_build_workflow():
    casa = yaml.safe_load((ROOT / "compose.casa.yml").read_text(encoding="utf-8"))
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    published = {entry["service"]: entry["image"] for entry in workflow["jobs"]["docker"]["strategy"]["matrix"]["include"]}
    for service, image in updater.IMAGE_NAMES.items():
        definition = casa["services"][service]
        assert "build" not in definition
        published_service = "backend" if service == "celery" else service
        assert definition["image"] == published[published_service] + ":${CLIPS_IMAGE_TAG:-latest}"
        assert definition["image"].startswith(f"ghcr.io/emotebot6/{image}:")


def test_real_compose_accepts_image_overlay_and_retains_server_settings(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose is needed to validate overlay merging")
    compose = tmp_path / "compose.yml"
    compose.write_text(json.dumps({"services": {"backend": {
        "build": ".", "environment": {"MY_SETTING": "retained"},
        "volumes": ["custom-data:/data"], "ports": ["5100:5000"],
    }}, "volumes": {"custom-data": {}}}))
    overlay = tmp_path / "images.json"
    overlay.write_text(json.dumps({updater.OWNER_KEY: 1, "services": {"backend": {
        "image": "ghcr.io/emotebot6/clipscreator-backend:latest",
    }}}))
    result = subprocess.run([
        docker, "compose", "-p", "test-casa", "-f", str(compose), "-f", str(overlay), "config", "--format", "json",
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    service = json.loads(result.stdout)["services"]["backend"]
    assert service["image"] == "ghcr.io/emotebot6/clipscreator-backend:latest"
    assert service["environment"]["MY_SETTING"] == "retained"
    assert service["volumes"][0]["source"] == "custom-data"
    assert service["ports"][0]["published"] == "5100"
