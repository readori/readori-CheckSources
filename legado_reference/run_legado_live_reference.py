#!/usr/bin/env python3
"""Run the REAL Legado source check (live network) over a book-source package.

The Kotlin test ``LegadoLiveSourceCheckTest`` (a port of Legado's ``CheckSourceService`` flow) and its Gradle init
script are attached to the checked-out Legado ``:app`` unit tests without modifying that checkout; only ordinary
Gradle build outputs (and, when absent, ``local.properties`` with ``sdk.dir``) are written there.

Requirements: JDK 17 (the Legado build uses ``jvmToolchain(17)`` and AGP 8.13), an Android SDK
(``ANDROID_HOME`` / ``ANDROID_SDK_ROOT``; an existing ``local.properties`` is left untouched and never read) and
network access (Gradle dependencies, Robolectric android-all jar, the book sites).

Exit codes: 0 report written; 1 Gradle failed (a partial report may exist); 2 usage / environment error;
3 the report is missing or not a Legado live report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

HERE = Path(__file__).resolve().parent
INIT_SCRIPT = HERE / "legado-live.init.gradle"
ADAPTER_SOURCE = HERE / "kotlin"
TEST_CLASS = "io.legado.app.readori.LegadoLiveSourceCheckTest"
DEFAULT_VARIANT = "AppRelease"
DEFAULT_GRADLE_JVMARGS = "-Xmx4g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
LOG_PREFIX = "[LEGADO-LIVE-WRAPPER]"


class WrapperError(Exception):
    """A usage or environment problem (exit code 2)."""


def gradle_task(variant: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", variant):
        raise WrapperError(f"invalid --variant {variant!r}; expected e.g. AppRelease or AppDebug")
    return f":app:test{variant}UnitTest"


def gradle_wrapper_command(legado_root: Path, platform: str | None = None) -> list[str]:
    """``gradlew.bat`` on Windows, ``sh gradlew`` elsewhere (works even when the exec bit is missing)."""
    platform = platform or sys.platform
    if platform == "win32":
        return [str(legado_root / "gradlew.bat")]
    return ["sh", str(legado_root / "gradlew")]


def resolve_sdk_dir(env: Mapping[str, str]) -> str | None:
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def plan_local_properties(legado_root: Path, env: Mapping[str, str]) -> tuple[str, str | None]:
    """Return ``(action, sdk_dir)``.

    ``keep``: ``local.properties`` already exists - it is neither read nor changed (it may hold local signing
    secrets). ``write``: it is absent and an SDK path is known. ``missing-sdk``: absent and no SDK path.
    Legado's release signing only activates when the *project property* ``RELEASE_STORE_FILE`` is set
    (``app/build.gradle``), which this run never sets, so no signing values are needed or invented.
    """
    if (legado_root / "local.properties").exists():
        return "keep", None
    sdk_dir = resolve_sdk_dir(env)
    if sdk_dir is None:
        return "missing-sdk", None
    return "write", sdk_dir


def write_local_properties(legado_root: Path, sdk_dir: str) -> Path:
    target = legado_root / "local.properties"
    # Java .properties: forward slashes avoid backslash escapes on Windows.
    value = Path(sdk_dir).as_posix()
    target.write_text(f"sdk.dir={value}\n", encoding="utf-8", newline="\n")
    return target


def git_revision(legado_root: Path) -> str:
    try:
        process = subprocess.run(
            ["git", "-c", f"safe.directory={legado_root.as_posix()}", "-C", str(legado_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return process.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def build_command(
    *,
    legado_root: Path,
    sources: Path,
    output: Path,
    concurrency: int,
    timeout: int | None,
    limit: int,
    offset: int,
    build_root: Path | None,
    variant: str = DEFAULT_VARIANT,
    gradle_jvmargs: str = DEFAULT_GRADLE_JVMARGS,
    flush_every: int | None = None,
    include_resources: bool = True,
    legado_revision: str = "",
    platform: str | None = None,
) -> list[str]:
    command = gradle_wrapper_command(legado_root, platform)
    command += [
        "--no-daemon",
        "--console=plain",
        "--stacktrace",
        f"-Dorg.gradle.jvmargs={gradle_jvmargs}",
        "-I",
        str(INIT_SCRIPT),
        gradle_task(variant),
        "--tests",
        TEST_CLASS,
        f"-PreadoriLiveAdapter={ADAPTER_SOURCE}",
        f"-PreadoriLiveSources={sources}",
        f"-PreadoriLiveOutput={output}",
        f"-PreadoriLiveConcurrency={concurrency}",
        f"-PreadoriLiveLimit={limit}",
        f"-PreadoriLiveOffset={offset}",
    ]
    if timeout is not None and timeout > 0:
        command.append(f"-PreadoriLiveTimeoutSeconds={timeout}")
    if flush_every is not None:
        command.append(f"-PreadoriLiveFlushEvery={flush_every}")
    if build_root is not None:
        command.append(f"-PreadoriLiveBuildRoot={build_root}")
    if not include_resources:
        command.append("-PreadoriLiveIncludeResources=false")
    if legado_revision:
        command.append(f"-PreadoriLiveLegadoRevision={legado_revision}")
    return command


def validate_output(output: Path) -> dict:
    if not output.is_file():
        raise ValueError(f"Legado live run did not create the report: {output}")
    try:
        document = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"report is not valid JSON: {output}: {error}") from error
    if not isinstance(document, dict) or document.get("engine") != "legado-live" or not isinstance(
        document.get("results"), list
    ):
        raise ValueError(f"report is not a legado-live report: {output}")
    return document


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--legado-root",
        type=Path,
        default=Path(os.environ.get("LEGADO_ROOT", "legado")),
        help="checkout of the Legado repository (default: $LEGADO_ROOT or ./legado)",
    )
    parser.add_argument("--sources", type=Path, required=True, help="book-source JSON array (see prepare_sources.py)")
    parser.add_argument("--output", type=Path, required=True, help="live report JSON to write")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=None, help="per-source seconds (default: Legado's 180 s)")
    parser.add_argument("--limit", type=int, default=0, help="0 = all sources")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--build-root",
        type=Path,
        default=None,
        help="optional external Gradle build directory (keeps the checkout free of build outputs)",
    )
    parser.add_argument("--variant", default=DEFAULT_VARIANT, help="Android variant, default AppRelease")
    parser.add_argument("--gradle-jvmargs", default=DEFAULT_GRADLE_JVMARGS)
    parser.add_argument("--flush-every", type=int, default=None, help="snapshot the report every N sources")
    parser.add_argument(
        "--no-android-resources",
        action="store_true",
        help="do not include merged Android resources in the Robolectric run (fallback if app start-up misbehaves)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the Gradle command and exit; nothing is written")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    env = os.environ if env is None else env
    try:
        legado_root = args.legado_root.expanduser().resolve()
        sources = args.sources.expanduser().resolve()
        output = args.output.expanduser().resolve()
        build_root = args.build_root.expanduser().resolve() if args.build_root else None
        if args.concurrency < 1:
            raise WrapperError("--concurrency must be >= 1")
        for required in (INIT_SCRIPT, ADAPTER_SOURCE):
            if not required.exists():
                raise WrapperError(f"required path does not exist: {required}")
        if not args.dry_run:
            if not sources.is_file():
                raise WrapperError(f"sources file does not exist: {sources}")
            wrapper = legado_root / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
            if not wrapper.is_file():
                raise WrapperError(f"Gradle wrapper not found: {wrapper}")
        action, sdk_dir = plan_local_properties(legado_root, env)
        if action == "missing-sdk" and not args.dry_run:
            raise WrapperError(
                "no Android SDK: set ANDROID_HOME or ANDROID_SDK_ROOT (or provide <legado-root>/local.properties)"
            )
        revision = git_revision(legado_root) if legado_root.is_dir() else ""
        command = build_command(
            legado_root=legado_root,
            sources=sources,
            output=output,
            concurrency=args.concurrency,
            timeout=args.timeout,
            limit=args.limit,
            offset=args.offset,
            build_root=build_root,
            variant=args.variant,
            gradle_jvmargs=args.gradle_jvmargs,
            flush_every=args.flush_every,
            include_resources=not args.no_android_resources,
            legado_revision=revision,
        )
    except WrapperError as error:
        print(f"{LOG_PREFIX} ERROR: {error}", file=sys.stderr)
        return 2

    print(f"{LOG_PREFIX} local.properties: {action}" + (" (sdk.dir from environment)" if action == "write" else ""))
    print(f"{LOG_PREFIX} cwd={legado_root}")
    print(f"{LOG_PREFIX} {shlex.join(command)}", flush=True)
    if args.dry_run:
        return 0

    if action == "write" and sdk_dir is not None:
        write_local_properties(legado_root, sdk_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    result = subprocess.run(command, cwd=legado_root)
    gradle_code = result.returncode
    try:
        document = validate_output(output)
    except ValueError as error:
        print(f"{LOG_PREFIX} ERROR: {error}", file=sys.stderr)
        return 1 if gradle_code != 0 else 3
    summary = document.get("summary") if isinstance(document.get("summary"), dict) else {}
    print(
        f"{LOG_PREFIX} report={output} partial={document.get('partial')} "
        f"completed={summary.get('completed')}/{summary.get('total')} passed={summary.get('passed')} "
        f"failed={summary.get('failed')}"
    )
    if gradle_code != 0:
        print(f"{LOG_PREFIX} ERROR: Gradle exited with {gradle_code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
