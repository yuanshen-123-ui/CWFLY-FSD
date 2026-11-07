"""支持 `python -m fsd` 启动（等价于 `python main.py`）。"""

import asyncio

from .main import main

if __name__ == "__main__":
    asyncio.run(main())
