"""The ssh command line for a host: the console's own key, pinned host keys, no prompts."""

import shlex

from app.config import Host


def ssh_argv(host: Host, argv: list[str], key: str, known_hosts: str) -> list[str]:
    return [
        "ssh",
        "-i", key,
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
