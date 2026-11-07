#!/usr/bin/env python3
"""兼容入口：`python main.py`（等价于 `python -m fsd`）。"""

import asyncio

from fsd.main import main

if __name__ == "__main__":
    asyncio.run(main())
