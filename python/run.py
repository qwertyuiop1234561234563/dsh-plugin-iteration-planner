"""Python 服务启动入口。

用法：
    python -m run              # 在 python/ 目录下执行，读取 .env
    uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-proxy-headers

本网关端口由 ``GATEWAY_PORT`` 决定（不是 ``API_PORT``）：后者属于 TS 算法引擎，两者共用
仓库根同一份 ``.env``，同名会让引擎与网关在本地联调时抢同一个端口。

多进程：设置 ``API_WORKERS>1`` 时启用多 worker（进程数）。注意此时：
- Agent 会话需共享存储（``SESSION_STORE=redis`` + ``REDIS_URL``），否则各 worker 各存一份；
- WebSocket 需在网关层做粘性会话（同一客户端固定落到同一 worker）。
详见 README「企业级能力」小节。

**客户端身份必须是单一事实来源**：uvicorn 默认开启 ``--proxy-headers``，并把
``127.0.0.1`` 当作可信代理，会用 ``X-Forwarded-For`` **改写** ``request.client``——
这会让应用层基于直连对端的信任判断（``API_TRUSTED_PROXIES``）被绕过（同机来源的
客户端即可伪造 IP 绕过限流）。因此这里显式关闭 uvicorn 的转发头改写，改由应用层
按 ``API_TRUSTED_PROXIES`` 决定是否采信 XFF。命令行启动时请同样附加
``--no-proxy-headers``。
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
        port=settings.gateway_port,
        log_level=settings.log_level,
        workers=settings.api_workers,
        # 关闭 uvicorn 自带日志配置，统一走我们的 JSON 处理器
        log_config=None,
        # 关闭转发头改写：客户端 IP 只由应用层按 API_TRUSTED_PROXIES 判定，
        # 避免 uvicorn 用 XFF 覆盖 request.client 从而绕过限流分桶
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()