"""CWFLY FSD 服务器核心包。

模块布局：
- protocol / config / certificate / auth / db   基础设施
- client_handler / server / server_link / management  协议与服务
- weather / whazzup                             周边数据
- main                                          入口逻辑（python main.py / python -m fsd）
"""
