from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anyio import Path


async def run_process(*command: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    """Run a subprocess and capture its output.

    Parameters
    ----------
    *command:
        The executable and its arguments.
    cwd:
        The working directory to run the process from.

    Returns
    -------
    tuple[int, str, str]
        The return code, decoded stdout, and decoded stderr.
    """

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    return (
        process.returncode or 0,
        stdout_bytes.decode(errors='replace'),
        stderr_bytes.decode(errors='replace'),
    )


async def run_git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    """Run a git command and capture its output.

    Parameters
    ----------
    *args:
        Arguments passed to the git executable.
    cwd:
        The working directory to run git from.

    Returns
    -------
    tuple[int, str, str]
        The return code, decoded stdout, and decoded stderr.
    """

    return await run_process('git', *args, cwd=cwd)
