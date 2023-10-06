#!/usr/bin/env python
"""
Blunt mypy: try scanning files with dmypy as well as mypy in parallel.
Return results from whichever completes first.

Sometimes dmypy hangs, crashes, gives false positives/negatives, etc.
Try to detect these weird states and kill it. Better luck next time!
"""
import asyncio
import logging
import re
import sys
from asyncio import FIRST_COMPLETED, create_subprocess_exec, create_task, gather, wait, wait_for
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from logging import Logger, getLogger
from pathlib import Path
from typing import Literal, TypeAlias

logger: Logger = getLogger(__name__)


ProcType: TypeAlias = Literal["mypy", "dmypy", "kill"]


@dataclass
class ProcResult:
    type: ProcType
    stdout: str
    stderr: str
    exit_code: int


def script_path(name: str) -> str:
    return str(Path(sys.executable).parent / name)


async def wait_process_results(type: ProcType, cmd: list[str]) -> ProcResult:
    proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    assert proc.stdout
    assert proc.stderr
    stdout, stderr = await gather(proc.stdout.read(), proc.stderr.read())
    code = await proc.wait()
    return ProcResult(type=type, stdout=stdout.decode(), stderr=stderr.decode(), exit_code=code)


# https://stackoverflow.com/a/71016398/177663
async def run_mypy(args: list[str]) -> ProcResult:
    logger.debug("Launching mypy")
    return await wait_process_results("mypy", [script_path("mypy"), *args])


async def run_dmypy(args: list[str]) -> ProcResult:
    logger.debug("Launching dmypy")
    return await wait_process_results("dmypy", [script_path("dmypy"), "run", "--", *args])


async def kill_mypy() -> None:
    result = await wait_process_results("kill", [script_path("dmypy"), "kill"])
    if result.exit_code != 0 or result.stderr:
        logger.warning(f"Failed to kill dmypy (exit {result.exit_code}): {result.stderr.strip()}")
    else:
        logger.info("dmypy killed")


NO_ERRORS_RE = re.compile(r"^Success: no issues found in [0-9]+ source file(s)?\n$")
WITH_ERRORS_RE = re.compile(r"\nFound [0-9]+ error(s)? in [0-9]+ file(s)? \(checked [0-9]+ source files\)\n$")


def is_successful_result(result: ProcResult) -> bool:
    if result.exit_code not in (0, 1):
        return False
    if result.stderr:
        return False
    if NO_ERRORS_RE.search(result.stdout) or WITH_ERRORS_RE.search(result.stdout):
        return True
    return False


async def inner_main() -> None:
    args: list[str] = sys.argv[1:]
    if not args:
        print("No args!")
        sys.exit(1)

    daemon_task = create_task(run_dmypy(args))
    sync_task = create_task(run_mypy(args))

    # https://stackoverflow.com/a/74696461/177663
    # task = next(as_completed([daemon_task, sync_task]))
    done, pending = await wait([daemon_task, sync_task], return_when=FIRST_COMPLETED)
    if daemon_task in done:
        result = daemon_task.result()
        if is_successful_result(result):
            logger.debug(f"dmypy finished successfully (code {result.exit_code})")
            sync_task.cancel()
        else:
            logger.warning(
                f"dmypy exited with {result.exit_code}. "
                f"stderr: {result.stderr.strip() or '(empty)'} "
                f"stdout: {result.stdout.strip() or '(empty)'}"
            )
            await kill_mypy()
            result = await wait_for(sync_task, timeout=None)

    elif sync_task in done:
        result = sync_task.result()
        logger.debug("mypy finished first. waiting for dmypy to catch up...")
        try:
            await wait_for(daemon_task, timeout=1)
        except TimeoutError:
            logger.debug("timeout waiting for dmypy")
            daemon_task.cancel()

    else:
        raise AssertionError(f"Completed task {done} is something unexpected!")

    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write("STDERR:\n")
        sys.stderr.write(result.stderr)
    sys.exit(result.exit_code)


def main() -> None:
    try:
        logging.basicConfig(level=logging.DEBUG)
        asyncio.run(inner_main())
    except KeyboardInterrupt:
        logger.error("Keyboard interrupt")
        sys.exit(1)


if __name__ == "__main__":
    main()
