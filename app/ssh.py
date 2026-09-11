"""The ssh command line for a host: the console's own key, pinned host keys, no prompts."""

import asyncio
import shlex

from app.config import Host


def ssh_argv(host: Host, argv: list[str], key: str, known_hosts: str) -> list[str]:
    return [
        "ssh",
        "-i", key,
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "ConnectTimeout=5",
        "-o", "LogLevel=ERROR",
        "-p", str(host.ssh_port),
        "--",
        host.ssh or "",
        shlex.join(argv),
    ]


async def capture(cmd: list[str]) -> tuple[int, bytes, bytes]:
    """Run to completion; a cancelled caller takes the process down with it."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err
