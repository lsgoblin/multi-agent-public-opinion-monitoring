"""Loopback-only Docker ingress relay with no container egress route."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import socket
import struct
from functools import partial

_RTM_DELROUTE = 25
_NLM_F_REQUEST = 1
_NLM_F_ACK = 4
_RTA_OIF = 4
_RT_TABLE_MAIN = 254
_RTPROT_BOOT = 3
_RT_SCOPE_UNIVERSE = 0
_RTN_UNICAST = 1
_PR_CAPBSET_DROP = 24
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_UNPRIVILEGED_UID = 10001
_UNPRIVILEGED_GID = 10001


def _delete_default_route(interface: str) -> None:
    """Delete the IPv4 default route through one interface using rtnetlink."""
    interface_index = socket.if_nametoindex(interface)
    route = struct.pack(
        "=BBBBBBBBI",
        socket.AF_INET,
        0,  # destination prefix length
        0,
        0,
        _RT_TABLE_MAIN,
        _RTPROT_BOOT,
        _RT_SCOPE_UNIVERSE,
        _RTN_UNICAST,
        0,
    )
    output_interface = struct.pack("=HHI", 8, _RTA_OIF, interface_index)
    sequence = 1
    header = struct.pack(
        "=IHHII",
        16 + len(route) + len(output_interface),
        _RTM_DELROUTE,
        _NLM_F_REQUEST | _NLM_F_ACK,
        sequence,
        0,
    )

    with socket.socket(
        socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE
    ) as netlink:
        netlink.bind((0, 0))
        netlink.send(header + route + output_interface)
        response = netlink.recv(4096)

    if len(response) < 20:
        raise RuntimeError("short rtnetlink response while removing default route")
    message_type = struct.unpack_from("=H", response, 4)[0]
    error = struct.unpack_from("=i", response, 16)[0]
    if message_type != 2:  # NLMSG_ERROR; error=0 is the ACK success response.
        raise RuntimeError(f"unexpected rtnetlink response type {message_type}")
    if error:
        code = abs(error)
        raise OSError(code, os.strerror(code), interface)


def _remove_default_routes() -> None:
    routes = []
    with open("/proc/net/route", encoding="ascii") as route_file:
        next(route_file)
        for line in route_file:
            fields = line.split()
            if (
                len(fields) >= 4
                and fields[1] == "00000000"
                and int(fields[3], 16) & 1
            ):
                routes.append(fields[0])

    for interface in routes:
        _delete_default_route(interface)

    with open("/proc/net/route", encoding="ascii") as route_file:
        remaining_defaults = [
            line.split()[0]
            for line in list(route_file)[1:]
            if len(line.split()) >= 4
            and line.split()[1] == "00000000"
            and int(line.split()[3], 16) & 1
        ]
    if remaining_defaults:
        raise RuntimeError(f"default IPv4 routes remain: {remaining_defaults}")

    with open("/proc/net/ipv6_route", encoding="ascii") as route_file:
        for line in route_file:
            fields = line.split()
            if (
                len(fields) >= 10
                and fields[0] == "0" * 32
                and fields[1] == "00"
                and int(fields[8], 16) & 1
            ):
                raise RuntimeError("an active IPv6 default route remains")


def _drop_privileges() -> None:
    """Drop every capability and switch to an unprivileged identity."""
    if os.geteuid() != 0:
        raise RuntimeError("gateway must start as root to remove its default route")
    if not os.path.exists("/.dockerenv"):
        raise RuntimeError("refusing to change routes outside a Docker container")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    with open("/proc/sys/kernel/cap_last_cap", encoding="ascii") as cap_file:
        last_capability = int(cap_file.read().strip())
    for capability in range(last_capability + 1):
        if libc.prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), f"drop capability {capability}")

    if libc.prctl(
        _PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0
    ) != 0 and ctypes.get_errno() != errno.EINVAL:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "clear ambient capabilities")

    os.setgroups([])
    os.setgid(_UNPRIVILEGED_GID)
    os.setuid(_UNPRIVILEGED_UID)

    status = {}
    with open("/proc/self/status", encoding="ascii") as status_file:
        for line in status_file:
            if line.startswith(
                ("Uid:", "Gid:", "CapEff:", "CapPrm:", "CapBnd:", "CapAmb:")
            ):
                key, value = line.split(":", maxsplit=1)
                status[key] = value.strip().split()
    if os.geteuid() != _UNPRIVILEGED_UID or os.getegid() != _UNPRIVILEGED_GID:
        raise RuntimeError("gateway did not switch to its unprivileged identity")
    retained_capabilities = (
        "CapEff",
        "CapPrm",
        "CapBnd",
        "CapAmb",
    )
    if any(
        int(status.get(capability, ["0"])[0], 16)
        for capability in retained_capabilities
    ):
        raise RuntimeError("gateway retained Linux capabilities after privilege drop")


async def _pipe(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
    while chunk := await source.read(65536):
        destination.write(chunk)
        await destination.drain()
    if destination.can_write_eof():
        destination.write_eof()


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target: tuple[str, int],
) -> None:
    upstream = None
    try:
        upstream_reader, upstream = await asyncio.open_connection(*target)
        await asyncio.gather(
            _pipe(reader, upstream),
            _pipe(upstream_reader, writer),
        )
    except Exception as exc:
        print(f"Local gateway {target}: {exc}", flush=True)
    finally:
        writer.close()
        if upstream is not None:
            upstream.close()


async def _serve() -> None:
    api = await asyncio.start_server(
        partial(_relay, target=("api", 8000)), "0.0.0.0", 8000
    )
    web = await asyncio.start_server(
        partial(_relay, target=("web", 8501)), "0.0.0.0", 8501
    )
    async with api, web:
        await asyncio.gather(api.serve_forever(), web.serve_forever())


def main() -> None:
    if not os.path.exists("/.dockerenv"):
        raise RuntimeError("refusing to change routes outside a Docker container")
    _remove_default_routes()
    _drop_privileges()
    print(
        "Local gateway started as "
        f"uid={os.getuid()} gid={os.getgid()} without capabilities "
        "or a default IPv4 route",
        flush=True,
    )
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
