from __future__ import annotations

import argparse
import json
import multiprocessing
import sys

from . import __version__
from .protocol import capacity_json, health_json, serve


def _configure_stdio() -> None:
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(stdin_reconfigure):
        stdin_reconfigure(encoding="utf-8-sig", errors="strict")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mathagent-sidecar")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--version", action="store_true")
    mode.add_argument("--health", action="store_true")
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--capacity", action="store_true")
    mode.add_argument("--platform-node", action="store_true")
    mode.add_argument("--configure-platform-node", action="store_true")
    mode.add_argument("--clear-platform-node", action="store_true")
    mode.add_argument("--platform-node-status", action="store_true")
    mode.add_argument(
        "--configure-provider-profile",
        choices=("primary", "failover", "accelerator", "retry"),
        metavar="PROFILE",
    )
    mode.add_argument(
        "--clear-provider-profile",
        choices=("primary", "failover", "accelerator", "retry"),
        metavar="PROFILE",
    )
    mode.add_argument("--provider-profile-status", action="store_true")
    parser.add_argument(
        "--resource-mode",
        choices=("quiet", "balanced", "fast"),
        default="balanced",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    _configure_stdio()
    args = build_parser().parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.health:
        print(health_json())
        return 0
    if args.capacity:
        print(capacity_json(args.resource_mode))
        return 0
    if args.configure_platform_node:
        from .platform_node import PlatformNodeError, configure_platform_credential

        try:
            payload = configure_platform_credential(sys.stdin)
        except PlatformNodeError as exc:
            payload = {"status": "error", "errorCode": exc.code}
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            return 2
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return 0
    if args.clear_platform_node:
        from .platform_node import PlatformNodeError, delete_platform_credential

        try:
            delete_platform_credential()
        except PlatformNodeError as exc:
            payload = {"status": "error", "errorCode": exc.code}
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            return 2
        print('{"status":"ok","configured":false,"accessScope":"platform"}')
        return 0
    if args.platform_node_status:
        from .platform_node import read_platform_status

        payload = read_platform_status()
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return 0 if payload.get("status") == "ok" else 2
    if args.configure_provider_profile:
        from .platform_node import PlatformNodeError, configure_provider_profile

        try:
            payload = configure_provider_profile(
                args.configure_provider_profile,
                sys.stdin,
            )
        except PlatformNodeError as exc:
            payload = {"status": "error", "errorCode": exc.code}
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            return 2
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return 0
    if args.clear_provider_profile:
        from .platform_node import PlatformNodeError, delete_provider_profile

        try:
            delete_provider_profile(args.clear_provider_profile)
        except PlatformNodeError as exc:
            payload = {"status": "error", "errorCode": exc.code}
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            return 2
        print(json.dumps({
            "status": "ok",
            "configured": False,
            "providerProfile": args.clear_provider_profile,
        }, ensure_ascii=True, separators=(",", ":")))
        return 0
    if args.provider_profile_status:
        from .platform_node import provider_profile_status

        payload = provider_profile_status()
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return 0 if payload.get("status") == "ok" else 2
    if args.platform_node:
        from .platform_node import run_platform_node

        return run_platform_node(args.resource_mode)
    return serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
