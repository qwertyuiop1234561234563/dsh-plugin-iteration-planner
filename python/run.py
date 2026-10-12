"""Python 服务启动入口。

用法：
    python -m run              # 在 python/ 目录下执行，读取 .env
    uvicorn app.main:app --host 0.0.0.0 --port 8080

多进程：设置 ``API_WORKERS>1`` 时启用多 worker（进程数）。注意此时：
- Agent 会话需共享存储（``SESSION_STORE=redis`` + ``REDIS_URL``），否则各 worker 各存一份；
- WebSocket 需在网关层做粘性会话（同一客户端固定落到同一 worker）。
详见 README「企业级能力」小节。
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings
from app.logging_config import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, force=True)
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level,
        workers=settings.api_workers,
        # 关闭 uvicorn 自带日志配置，统一走我们的 JSON 处理器
        log_config=None,
    )


if __name__ == "__main__":
    main()