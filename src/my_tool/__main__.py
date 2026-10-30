"""`python -m my_tool` 的入口。

存在的理由很实际：没装成命令行工具时（比如刚 clone 下来、只想跑一次看效果），
`python -m my_tool build` 比先 `pip install -e .` 再 `payroll-check build` 少一步。
"""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
