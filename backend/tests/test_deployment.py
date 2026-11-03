"""Deployment: the shared password and the dashboard served by the API.

Each test builds its own small FastAPI app rather than reloading the real one,
so the behaviour is exercised exactly as ``main.py`` wires it without
disturbing the app the rest of the suite shares.
"""
from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.frontend import mount_frontend, resolve_static_file
from app.config.settings import settings
from app.core.access import install_access_control, password_from_header


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setattr(settings, "access_password", SecretStr("open sesame"))
    app = FastAPI()

    @app.get(f"{settings.api_prefix}/health")
    def health():
        return {"ok": True}

    @app.get(f"{settings.api_prefix}/documents")
    def documents():
        return {"items": []}

    assert install_access_control(app) is True
    return TestClient(app)


class TestSharedPassword:
    def test_no_password_means_no_login(self, monkeypatch):
        monkeypatch.setattr(settings, "access_password", None)
        assert install_access_control(FastAPI()) is False

    def test_an_empty_password_is_not_a_password(self, monkeypatch):
        monkeypatch.setattr(settings, "access_password", SecretStr(""))
        assert install_access_control(FastAPI()) is False

    def test_a_request_without_it_is_refused_and_challenged(self, protected):
        r = protected.get(f"{settings.api_prefix}/documents")
        assert r.status_code == 401
        # the challenge is what makes the browser show its login box
        assert r.headers["WWW-Authenticate"].startswith("Basic")

    def test_the_wrong_password_is_refused(self, protected):
        r = protected.get(f"{settings.api_prefix}/documents",
                          headers=_basic("x", "open says me"))
        assert r.status_code == 401

    def test_the_right_password_with_any_username_is_let_in(self, protected):
        for user in ("", "prof", "anyone"):
            r = protected.get(f"{settings.api_prefix}/documents",
                              headers=_basic(user, "open sesame"))
            assert r.status_code == 200

    def test_the_health_check_stays_open_for_the_host(self, protected):
        assert protected.get(f"{settings.api_prefix}/health").status_code == 200

    def test_a_cors_preflight_is_not_challenged(self, protected):
        r = protected.options(f"{settings.api_prefix}/documents")
        assert r.status_code != 401

    @pytest.mark.parametrize("header", [None, "", "Bearer abc", "Basic !!!notbase64",
                                        "Basic " + base64.b64encode(b"nocolon").decode()])
    def test_malformed_headers_carry_no_password(self, header):
        assert password_from_header(header) is None

    def test_a_password_may_itself_contain_a_colon(self):
        token = base64.b64encode(b"user:pa:ss").decode()
        assert password_from_header(f"Basic {token}") == "pa:ss"


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
    (root / "assets" / "app-1a2b.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the build", encoding="utf-8")
    return root


@pytest.fixture
def served(dist):
    app = FastAPI()

    @app.get(f"{settings.api_prefix}/documents")
    def documents():
        return {"items": []}

    mount_frontend(app, dist)
    return TestClient(app)


class TestDashboardServing:
    def test_the_root_is_the_dashboard(self, served):
        r = served.get("/")
        assert r.status_code == 200 and "dashboard" in r.text

    def test_a_client_side_route_falls_back_to_the_dashboard(self, served):
        r = served.get("/documents/4e44571fa4314809b208c8d1")
        assert r.status_code == 200 and "dashboard" in r.text
        assert r.headers["cache-control"] == "no-cache"

    def test_built_files_are_served_as_themselves(self, served):
        assert served.get("/assets/app-1a2b.js").text == "console.log(1)"
        assert served.get("/favicon.svg").text == "<svg/>"

    def test_api_routes_are_not_shadowed(self, served):
        assert served.get(f"{settings.api_prefix}/documents").json() == {"items": []}

    def test_an_unknown_api_path_is_a_404_not_a_page_of_html(self, served):
        r = served.get(f"{settings.api_prefix}/no-such-thing")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_a_path_cannot_climb_out_of_the_build(self, dist):
        assert resolve_static_file(dist.resolve(), "../secret.txt") is None
        assert resolve_static_file(dist.resolve(), "favicon.svg") is not None

    def test_a_missing_build_is_refused_at_startup(self, tmp_path):
        with pytest.raises(RuntimeError, match="index.html"):
            mount_frontend(FastAPI(), tmp_path)
