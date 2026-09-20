import re
import shutil
import subprocess

import pytest

from frontend.index import app
from frontend_tshirts.index import app as shirt_app


def test_shirt_navigation_uses_configured_port(monkeypatch):
    monkeypatch.delenv("TSHIRT_FRONTEND_URL", raising=False)
    monkeypatch.setenv("TSHIRT_FRONTEND_PORT", "4002")
    response = app.test_client().get("/tshirts", base_url="http://192.0.2.1:3000")
    assert response.location == "http://192.0.2.1:4002/"
    assert b'href="/tshirts"' in app.test_client().get("/").data


def test_shirt_navigation_supports_reverse_proxy_url(monkeypatch):
    monkeypatch.setenv("TSHIRT_FRONTEND_URL", "https://designs.example.com/")
    assert app.test_client().get("/tshirts").location == "https://designs.example.com/"


@pytest.mark.parametrize("frontend", [app, shirt_app])
def test_rendered_javascript_parses(frontend):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to check browser JavaScript syntax")
    response = frontend.test_client().get("/")
    assert response.status_code == 200
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", response.get_data(as_text=True), flags=re.DOTALL)
    assert scripts
    result = subprocess.run([node, "--check"], input="\n".join(scripts), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
