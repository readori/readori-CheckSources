#!/usr/bin/env python3
"""Console entry point for the standalone Readori source validator.

Without a sub-command this is the batch validator (every batch flag starts with ``--``).  The first argument
``debug`` / ``test-rule`` / ``fix`` / ``validate-one`` / ``export`` / ``rpc`` selects the R146 book-source workbench
(``validator/workbench_cli.py``); ``rpc`` is the JSON-lines service the desktop workbench talks to.
"""

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

WORKBENCH_COMMANDS = ("debug", "test-rule", "fix", "validate-one", "export", "rpc")


def dispatch(argv: list[str]) -> int:
    """Workbench sub-command when ``argv[0]`` names one, otherwise the unchanged batch ``main()``."""
    if argv and argv[0] in WORKBENCH_COMMANDS:
        from validator.workbench_cli import main as workbench_main

        return workbench_main(argv)
    return main()


if __name__ == "__main__":
    raise SystemExit(dispatch(sys.argv[1:]))
