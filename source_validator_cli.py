#!/usr/bin/env python3
"""Console entry point for the standalone Readori source validator."""

from __future__ import annotations

import sys

try:
    from validator.validate_source_packages import main
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local install
    missing = exc.name or "a runtime dependency"
    print(
        f"缺少 Python 依赖：{missing}。请先运行 install_dependencies.ps1，"
        "或执行 pip install -r requirements-validate-sources.txt。",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
