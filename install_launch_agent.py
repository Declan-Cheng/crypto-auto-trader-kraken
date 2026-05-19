#!/usr/bin/env python3
from __future__ import annotations

import argparse
import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LABEL = "com.chengziyou.kraken-auto-trader"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def write_plist() -> None:
    ROOT.joinpath("logs").mkdir(exist_ok=True)
    ROOT.joinpath("run_live.sh").chmod(0o755)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / "run_live.sh")],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(ROOT),
        "StandardOutPath": str(ROOT / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(ROOT / "logs" / "launchd.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as fh:
        plistlib.dump(payload, fh)


def run(args: list[str]) -> None:
    subprocess.run(args, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or remove the Kraken auto-trader LaunchAgent")
    parser.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart"])
    args = parser.parse_args()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    service = f"gui/{uid}/{LABEL}"

    if args.action == "install":
        write_plist()
        print(f"installed {PLIST}")
        return 0
    if args.action == "uninstall":
        run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)])
        if PLIST.exists():
            PLIST.unlink()
        print(f"uninstalled {PLIST}")
        return 0
    if args.action == "start":
        write_plist()
        run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)])
        run(["launchctl", "enable", service])
        run(["launchctl", "kickstart", "-k", service])
        print(f"started {LABEL}")
        return 0
    if args.action == "stop":
        run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)])
        print(f"stopped {LABEL}")
        return 0
    if args.action == "restart":
        run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)])
        write_plist()
        run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)])
        run(["launchctl", "kickstart", "-k", service])
        print(f"restarted {LABEL}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
