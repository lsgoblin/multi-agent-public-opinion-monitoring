from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from riskshield.api import create_app


def test_ui_import_snapshot_and_evaluation_toggle(tmp_path, monkeypatch):
    """Exercise the real UI script against an isolated real API/store, not canned data."""
    with TestClient(create_app(tmp_path / "ui.db")) as api:
        def local_request(method, url, **kwargs):
            path = url.removeprefix("http://127.0.0.1:8000")
            kwargs.pop("timeout", None)
            return api.request(method, path, **kwargs)

        monkeypatch.setenv("RISKSHIELD_API_URL", "http://127.0.0.1:8000")
        monkeypatch.setattr(httpx, "request", local_request)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "ui/app.py"))
        app.run(timeout=20)
        assert not app.exception
        assert app.metric[1].value == "0"
        next(button for button in app.button if button.label == "导入随附公开案例").click()
        app.run(timeout=20)
        assert not app.exception
        assert app.metric[1].value == "1"
        assert app.checkbox[0].value is False
        before = " ".join(item.value for item in app.markdown)
        assert "营销骚扰相关沟通的公开报道" in before
        assert "次日媒体跟进报道" not in before
        app.checkbox[0].check().run(timeout=20)
        assert not app.exception
        assert "次日媒体跟进报道" in " ".join(item.value for item in app.markdown)
        next(button for button in app.button if button.label == "导入随附公开案例").click()
        app.run(timeout=20)
        assert app.metric[1].value == "1"
        assert any("未重复入库" in message.value for message in app.success)
