#!/usr/bin/env python3
"""Interactive menu for Netac NV7000 2 TB IG5236 recovery."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools" / "recovery" / "rocky_nv7000_recover.py"
SYSFS = Path("/sys/bus/pci/devices")
FW_DEFAULT = Path("~/netac-recovery").expanduser()


def controllers() -> list[tuple[str, str, str]]:
    found = []
    for device in sorted(SYSFS.iterdir()):
        try:
            if (device / "class").read_text().strip().lower() != "0x010802":
                continue
            vendor = (device / "vendor").read_text().strip().lower()
            product = (device / "device").read_text().strip().lower()
            if product == "0x5236" and vendor in {"0x1f40", "0x1dbe"}:
                found.append((device.name, vendor, product))
        except OSError:
            continue
    return found


def run_runner(bdf: str, firmware_root: Path, execute: bool = False) -> int:
    command = [
        sys.executable,
        str(RUNNER),
        "--bdf",
        bdf,
        "--firmware-dir",
        str(firmware_root),
    ]
    if execute:
        command.append("--execute")
        if os.geteuid() != 0:
            command.insert(0, "sudo")
    return subprocess.run(command, cwd=ROOT).returncode


def show_post_recovery() -> None:
    print("\nRead-only NVMe inventory:")
    nvme_cli = subprocess.run(["sh", "-c", "command -v nvme"], text=True, capture_output=True)
    if nvme_cli.returncode != 0:
        print("nvme-cli is not installed. On Ubuntu Live, install it with: sudo apt install nvme-cli")
        return
    subprocess.run(["nvme", "list"], check=False)
    print("\nExpected: Netac NV7000 namespace close to 2 TB.")
    print("For SMART details, run: sudo nvme smart-log /dev/nvmeX (replace X using the list above).")


def main() -> int:
    if not RUNNER.is_file():
        print(f"Recovery runner not found: {RUNNER}", file=sys.stderr)
        return 1

    selected_bdf = ""
    firmware_root = FW_DEFAULT
    preflight_passed = False

    while True:
        print("\n=== Netac NV7000 2 TB recovery ===")
        print(f"Target PCI address: {selected_bdf or '(not selected)'}")
        print(f"Firmware package root: {firmware_root}")
        print("1. Detect/select IG5236 controller")
        print("2. Set firmware package directory")
        print("3. Run read-only preflight")
        print("4. Start firmware recovery")
        print("5. Check recovered SSD")
        print("0. Exit")
        choice = input("Select: ").strip()

        if choice == "0":
            return 0
        if choice == "1":
            devices = controllers()
            if not devices:
                print("No supported IG5236 controller found in PCI sysfs.")
                continue
            for index, (bdf, vendor, product) in enumerate(devices, start=1):
                print(f"{index}. {bdf} {vendor}:{product}")
            answer = input("Choose the controller number (Enter cancels): ").strip()
            if not answer:
                continue
            try:
                index = int(answer) - 1
                selected_bdf = devices[index][0]
                preflight_passed = False
            except (ValueError, IndexError):
                print("Invalid selection.")
            continue
        if choice == "2":
            answer = input("Firmware package root (contains m2_0536/): ").strip()
            if answer:
                firmware_root = Path(answer).expanduser().resolve()
                preflight_passed = False
            continue
        if choice == "3":
            if not selected_bdf:
                print("Select the controller first.")
                continue
            preflight_passed = run_runner(selected_bdf, firmware_root) == 0
            continue
        if choice == "4":
            if not selected_bdf:
                print("Select the controller first.")
                continue
            if not preflight_passed:
                print("Running preflight before any firmware write...")
                preflight_passed = run_runner(selected_bdf, firmware_root) == 0
            if not preflight_passed:
                print("Preflight failed. No firmware was written.")
                continue
            print("\nThis operation may permanently brick the SSD and erase its data.")
            answer = input("Type START to open the runner's final erase confirmation: ").strip()
            if answer == "START":
                result = run_runner(selected_bdf, firmware_root, execute=True)
                preflight_passed = False
                print(f"Recovery process ended with exit code {result}.")
            else:
                print("Cancelled; no firmware was written.")
            continue
        if choice == "5":
            show_post_recovery()
            continue
        print("Unknown option.")


if __name__ == "__main__":
    raise SystemExit(main())
