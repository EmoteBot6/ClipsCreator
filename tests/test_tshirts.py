import importlib
import io
import json
import threading
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest
from PIL import Image

from frontend_tshirts import index as shirts


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(shirts, "DATA_DIR", tmp_path)
    monkeypatch.setattr(shirts, "DESIGNS_DIR", tmp_path / "designs")
    monkeypatch.setattr(shirts, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(shirts, "CATALOG_PATH", tmp_path / "designs.json")
    monkeypatch.setattr(shirts, "generation_lock", threading.Lock())
    monkeypatch.setattr(shirts, "scheduler_wakeup", threading.Event())
    return shirts.app.test_client()


def test_toggle_persists_and_blocks_manual_and_scheduled_generation(service, monkeypatch):
    generate = Mock()
    monkeypatch.setattr(shirts, "ask_ollama_for_design", generate)
    assert service.get("/api/status").json["generation_enabled"] is True
    response = service.post("/api/settings", json={"generation_enabled": False})
    assert response.status_code == 200
    assert response.json["next_attempt_at_utc"] is None
    assert json.loads(shirts.STATUS_PATH.read_text())["generation_enabled"] is False
    assert shirts.should_generate(shirts.load_status()) is False
    assert shirts.generate_design() == {"status": "disabled"}
    assert service.post("/api/generate-now").status_code == 409
    generate.assert_not_called()
    assert not shirts.generation_lock.locked()
    assert service.post("/api/settings", json={"generation_enabled": True}).status_code == 200
    assert shirts.should_generate(shirts.load_status()) is True
    assert shirts.scheduler_wakeup.is_set()


@pytest.mark.parametrize("payload", [{}, {"generation_enabled": "false"}, {"generation_enabled": 0}, [], None])
def test_settings_requires_boolean(service, payload):
    assert service.post("/api/settings", json=payload).status_code == 400
    assert service.get("/api/status").json["generation_enabled"] is True


def test_restart_clears_running_flag_but_preserves_toggle(service, monkeypatch):
    shirts.save_status(is_running=True, generation_enabled=False)
    thread = Mock()
    monkeypatch.setattr(shirts.threading, "Thread", Mock(return_value=thread))
    monkeypatch.setattr(shirts, "scheduler_thread", None)
    shirts.start_scheduler()
    assert shirts.load_status()["is_running"] is False
    assert shirts.load_status()["generation_enabled"] is False
    thread.start.assert_called_once()


def test_manual_requests_reserve_generation_before_thread_starts(service, monkeypatch):
    thread = Mock()
    monkeypatch.setattr(shirts.threading, "Thread", Mock(return_value=thread))
    assert service.post("/api/generate-now").status_code == 202
    assert service.post("/api/generate-now").status_code == 409
    thread.start.assert_called_once()


def test_failed_thread_start_releases_lock(service, monkeypatch):
    monkeypatch.setattr(shirts.threading, "Thread", Mock(side_effect=RuntimeError("thread unavailable")))
    with pytest.raises(RuntimeError):
        shirts.start_manual_generation()
    assert not shirts.generation_lock.locked()


def test_export_enforces_minimum_without_stretching_and_uses_small_preview(service, monkeypatch):
    monkeypatch.setattr(shirts, "IMAGE_SIZE", 1000)
    path = shirts.DATA_DIR / "design.png"
    Image.new("RGB", (80, 40), "red").save(path, "JPEG")
    dimensions = shirts.image_export_metadata(path)
    assert dimensions == {"width": 8000, "height": 8000}
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.size == (8000, 8000)
        assert image.getpixel((4000, 0))[3] == 0
        assert image.getpixel((4000, 4000))[3] == 255
    with Image.open(path.with_name("preview.png")) as image:
        assert image.size == (600, 600)


def test_svg_has_minimum_export_viewport(service):
    path = shirts.DATA_DIR / "design.svg"
    shirts.create_svg_file({"svg": '<svg width="512" height="512" viewBox="0 0 512 512"><rect width="512" height="512" fill="red"/></svg>'}, path)
    root = ET.fromstring(path.read_text())
    assert root.attrib["width"] == root.attrib["height"] == "8000"
    assert root.attrib["viewBox"] == "0 0 512 512"


@pytest.mark.parametrize("provider,fail", [("local_diffusion", False), ("pollinations", False), ("prompt_card", False), ("local_diffusion", True)])
def test_every_raster_generation_path_passes_export_validation(service, monkeypatch, provider, fail):
    monkeypatch.setattr(shirts, "IMAGE_PROVIDER", provider)
    monkeypatch.setattr(shirts, "ask_ollama_for_design", Mock(return_value={"title": "Test", "prompt": "Test illustration"}))

    def render(brief, path):
        Image.new("RGB", (32, 32), "red").save(path)

    monkeypatch.setattr(shirts, "generate_local_diffusion_image", Mock(side_effect=RuntimeError("offline")) if fail else render)
    monkeypatch.setattr(shirts, "generate_pollinations_image", render)
    monkeypatch.setattr(shirts, "render_prompt_card", render)
    exporter = Mock(return_value={"width": 8000, "height": 8000})
    monkeypatch.setattr(shirts, "image_export_metadata", exporter)
    result = shirts.generate_design("manual")
    assert result["status"] == ("fallback" if fail else "generated")
    assert result["width"] == result["height"] == 8000
    assert result["preview_url"].endswith("/preview.png")
    exporter.assert_called_once()
    assert shirts.load_status()["is_running"] is False


@pytest.mark.parametrize("design_id", [".", "..", "../designs", "a/b"])
def test_rejects_catalog_root_as_design_id(service, design_id):
    with pytest.raises(ValueError):
        shirts.safe_id(design_id)


def test_frontend_has_toggle_and_small_preview(service):
    page = service.get("/")
    assert page.status_code == 200
    assert b'role="switch"' in page.data
    assert b"design.preview_url || design.url" in page.data


def test_generated_download_has_8000_pixels_and_preview_is_separate(service, monkeypatch):
    monkeypatch.setattr(shirts, "IMAGE_PROVIDER", "local_diffusion")
    monkeypatch.setattr(shirts, "ask_ollama_for_design", Mock(return_value={"title": "Artwork", "prompt": "Botanical pattern"}))
    content = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(content, "PNG")
    response = Mock(status_code=200, content=content.getvalue(), headers={"Content-Type": "image/png"})
    monkeypatch.setattr(shirts, "post_local_image", Mock(return_value=response))
    result = shirts.generate_design("manual")
    assert result["status"] == "generated"
    with Image.open(io.BytesIO(service.get(result["download_url"]).data)) as image:
        assert image.size == (8000, 8000)
    with Image.open(io.BytesIO(service.get(result["preview_url"]).data)) as image:
        assert image.size == (600, 600)
    assert service.get("/api/designs").json[0]["width"] == 8000


def test_toggle_off_during_generation_remains_off_at_completion(service, monkeypatch):
    monkeypatch.setattr(shirts, "IMAGE_PROVIDER", "ollama_svg")

    def brief(*args, **kwargs):
        assert service.post("/api/settings", json={"generation_enabled": False}).status_code == 200
        return {"title": "In flight", "prompt": "Print graphic"}

    monkeypatch.setattr(shirts, "ask_ollama_for_design", brief)
    assert shirts.generate_design()["status"] == "generated"
    assert service.get("/api/status").json["generation_enabled"] is False
    assert shirts.should_generate(shirts.load_status()) is False


def test_env_cannot_lower_minimum_export_size(monkeypatch):
    # Reload without starting a scheduler or writing into the runtime data folder.
    monkeypatch.setenv("TSHIRT_IMAGE_SIZE", "512")
    importlib.reload(shirts)
    assert shirts.IMAGE_SIZE == 8000
    assert shirts.scheduler_thread is None
