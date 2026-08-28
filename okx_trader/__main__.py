# -*- coding: utf-8 -*-
"""``python -m okx_trader`` 的入口：全部子命令见 cli.py。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
