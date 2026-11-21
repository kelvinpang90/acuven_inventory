"""健康检查的响应体与授权回调的平台名。"""

# 授权回调路径里接受的平台名（设计 V1_MINIMAL_DESIGN 第 8 节）。V1 只有两个自营店铺的平台。
CALLBACK_PLATFORMS = ("shopee", "tiktok-shop")


def health_payload(commit: str) -> dict[str, str]:
    """部署工作流靠 commit 确认线上就是本次提交（部署观察契约 D3）。"""
    return {"status": "ok", "commit": commit or "unknown"}


def is_callback_platform(name: str) -> bool:
    """只认精确的小写平台名：路径写错时回答 404，而不是猜一个平台。"""
    return name in CALLBACK_PLATFORMS
