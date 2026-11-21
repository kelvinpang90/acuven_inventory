import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

# 只指向 CI 的一次性 MySQL 服务容器；本地没设时跳过需要数据库的那条。
TEST_DATABASE_URL = os.environ.get("INVENTORY_TEST_DATABASE_URL", "")
UNREACHABLE = "mysql+pymysql://nobody:x@127.0.0.1:1/none"


def client(**overrides: str) -> TestClient:
    values = {"git_sha": "b" * 40, "database_url": ""} | overrides
    return TestClient(create_app(Settings(**values)))


def test_healthz_reports_the_commit_without_touching_the_database():
    response = client(database_url=UNREACHABLE).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "commit": "b" * 40}


def test_readyz_is_unavailable_when_no_database_is_configured():
    response = client().get("/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_not_configured"


def test_readyz_is_unavailable_when_the_database_cannot_be_reached():
    response = client(database_url=UNREACHABLE).get("/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "database_unreachable"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="INVENTORY_TEST_DATABASE_URL not set (CI only)")
def test_readyz_is_ok_against_a_real_database():
    response = client(database_url=TEST_DATABASE_URL).get("/readyz")
    assert response.status_code == 200


@pytest.mark.parametrize("platform", ["shopee", "tiktok-shop"])
def test_oauth_callback_exists_but_is_not_enabled_and_echoes_nothing(platform):
    # 地址先上线、授权未实现：不能把授权码或 state 原样回显到响应里。
    response = client().get(
        f"/api/v1/integrations/{platform}/oauth/callback",
        params={"code": "secret-code-123", "state": "secret-state-456"},
    )
    assert response.status_code == 503
    assert response.json() == {"status": "not_enabled"}
    assert "secret-code-123" not in response.text
    assert "secret-state-456" not in response.text


def test_oauth_callback_for_an_unknown_platform_is_not_found():
    response = client().get("/api/v1/integrations/lazada/oauth/callback", params={"code": "x"})
    assert response.status_code == 404
    assert response.json() == {"status": "not_found"}
