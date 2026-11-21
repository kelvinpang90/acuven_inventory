from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVENTORY_")

    # 镜像构建时由 Dockerfile 的 GIT_SHA 构建参数写进 INVENTORY_GIT_SHA；健康检查原样返回。
    git_sha: str = "unknown"
    # 可以不设：未设置时 /readyz 回答 503（数据库未配置），/healthz 照常。
    database_url: str = ""
