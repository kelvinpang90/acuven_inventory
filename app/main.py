from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db import make_engine
from app.domain.health import health_payload, is_callback_platform


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url) if settings.database_url else None
    app = FastAPI(title="Acuven Inventory", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活检查：不碰数据库，返回线上 commit。"""
        return health_payload(settings.git_sha)

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """就绪检查：数据库能执行一条查询才算就绪。"""
        if engine is None:
            return JSONResponse({"status": "unavailable", "reason": "database_not_configured"}, 503)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable", "reason": "database_unreachable"}, 503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/v1/integrations/{platform}/oauth/callback")
    def oauth_callback(platform: str) -> JSONResponse:
        """平台授权回调的固定地址（设计第 8 节）。

        在平台后台建应用时要填这个地址，所以先让它上线。授权还没实现：不读、不存、不回显任何
        查询参数（授权码只经过这里一次），一律回答 503。state 校验与换令牌由之后的编号任务实现。
        """
        if not is_callback_platform(platform):
            return JSONResponse({"status": "not_found"}, 404)
        return JSONResponse({"status": "not_enabled"}, 503)

    return app
