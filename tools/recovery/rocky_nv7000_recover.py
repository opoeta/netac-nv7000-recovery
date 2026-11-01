#!/usr/bin/env python3
"""Fail-closed Rocky Linux runner for the Netac NV7000 2 TB IG5236 pipeline.

Default mode is read-only preflight. Firmware writes require --execute and an
exact interactive confirmation. This utility has not been hardware-tested on
Rocky Linux or Ubuntu; see docs/rocky-linux.md before use.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "tools" / "recovery" / "netac_pipeline.py"
EXPECTED_FW = (
    "loader.fw",
    "programmer+cust+3SJ11+ymtcx2.fw",
    "rdisk+cust+3SJ11+512b+hcrc+semi+l0ps+iecc+semiswitch+logerr+rel+mstripe+raid+a1perf_raid+ymtcx2-cmfg.fw",
)
SUPPORTED_VENDORS = {"0x1f40", "0x1dbe"}
CONFIRMATION = "ERASE DATA AND RECOVER NETAC NV7000 2TB"


class PreflightError(RuntimeError):
    pass


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PreflightError(f"Cannot read {path}: {exc}") from exc


def command_output(*args: str) -> str:
    try:
        result = subprocess.run(args, check=True, text=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(f"Command failed: {' '.join(args)}: {exc}") from exc
    return result.stdout.strip()


def parse_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def check_kernel() -> str:
    release = platform.release()
    match = re.match(r"^(\d+)\.(\d+)", release)
    if not match or tuple(map(int, match.groups())) < (6, 6):
        raise PreflightError(
            f"Kernel {release} is below 6.6; this pipeline requires VFIO device cdev + IOMMUFD. "
            "Use Ubuntu 26.04 live media or Rocky Linux 10; do not install a third-party kernel for this recovery."
        )
    return release


def check_target_not_mounted(device: Path) -> None:
    nvme_root = device / "nvme"
    for controller in nvme_root.glob("nvme*"):
        for namespace in controller.glob("nvme*n*"):
            if not re.fullmatch(r"nvme\d+n\d+", namespace.name):
                continue
            node = f"/dev/{namespace.name}"
            try:
                tree = json.loads(
                    command_output("lsblk", "--json", "--paths", "--output", "NAME,MOUNTPOINTS", node)
                )
            except PreflightError as exc:
                raise PreflightError(f"Cannot verify whether target namespace {node} is in use: {exc}") from exc

            def has_mount(entry: dict) -> bool:
                mounts = entry.get("mountpoints") or []
                return any(mount for mount in mounts) or any(
                    has_mount(child) for child in entry.get("children", [])
                )

            if any(has_mount(entry) for entry in tree.get("blockdevices", [])):
                raise PreflightError(
                    f"Target namespace {node} or one of its partitions is mounted or used as swap. "
                    "Unmount/deactivate it before recovery."
                )


def check_target(bdf: str) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", bdf):
        raise PreflightError("BDF must look like 0000:03:00.0.")
    device = Path("/sys/bus/pci/devices") / bdf.lower()
    if not device.is_dir():
        raise PreflightError(f"PCI device {bdf} is not present.")
    pci_class = read(device / "class").lower()
    if pci_class != "0x010802":
        raise PreflightError(f"{bdf} is class {pci_class}, not an NVMe controller (0x010802).")
    vendor = read(device / "vendor").lower()
    product = read(device / "device").lower()
    if product != "0x5236" or vendor not in SUPPORTED_VENDORS:
        raise PreflightError(
            f"{bdf} is {vendor}:{product}; expected IG5236 1f40:5236 (ROM) or "
            "1dbe:5236 (runtime). Refusing to touch another device."
        )
    group_link = device / "iommu_group" / "devices"
    if not group_link.is_dir():
        raise PreflightError(f"{bdf} has no IOMMU group; VFIO recovery is unavailable.")
    members = sorted(p.name for p in group_link.iterdir())
    if members != [bdf.lower()]:
        raise PreflightError(
            f"IOMMU group for {bdf} also contains {', '.join(members)}. "
            "Isolate the SSD in a group by itself before recovery."
        )
    check_target_not_mounted(device)
    return device


def check_root_not_on_nvme() -> str:
    source = command_output("findmnt", "-nro", "SOURCE", "/")
    # Ubuntu live desktop boots with overlay root; resolve its USB boot medium.
    block_source = source
    if source.startswith("overlay"):
        block_source = command_output("findmnt", "-nro", "SOURCE", "/cdrom")
        if not block_source:
            raise PreflightError("Live root is overlay but /cdrom has no identifiable boot medium.")
    tree = command_output("lsblk", "-s", "-nr", "-o", "NAME", block_source)
    if re.search(r"\bnvme\d+n\d+\b", tree):
        raise PreflightError(
            "The running OS or its boot medium is backed by NVMe. Boot from a separate USB/SATA medium "
            "before recovery; this tool must not risk the running system disk."
        )
    return f"{source} (backing device: {block_source})" if source.startswith("overlay") else source

def firmware_directory(value: str) -> Path:
    directory = Path(value).expanduser().resolve() / "m2_0536"
    if not directory.is_dir():
        raise PreflightError(f"Firmware directory not found: {directory}")
    missing = [name for name in EXPECTED_FW if not (directory / name).is_file()]
    if missing:
        raise PreflightError(
            "Missing exact firmware files required by this pipeline: " + ", ".join(missing)
        )
    return directory


def preflight(args: argparse.Namespace) -> tuple[Path, Path]:
    os_info = parse_os_release()
    supported = (
        os_info.get("ID") == "rocky" and os_info.get("VERSION_ID", "").startswith("10.")
    ) or (
        os_info.get("ID") == "ubuntu" and os_info.get("VERSION_ID", "").startswith("26.04")
    )
    if not supported:
        raise PreflightError(
            f"Detected {os_info.get('PRETTY_NAME', 'unknown OS')}; supported systems are Rocky Linux 10 and Ubuntu 26.04."
        )
    if platform.machine() != "x86_64":
        raise PreflightError("This runner only supports x86_64 systems.")
    kernel = check_kernel()
    if not PIPELINE.is_file():
        raise PreflightError(f"Recovery pipeline not found: {PIPELINE}")
    device = check_target(args.bdf)
    root_source = check_root_not_on_nvme()
    fw = firmware_directory(args.firmware_dir)
    print("Read-only preflight passed.")
    print(f"OS: {os_info.get('PRETTY_NAME')}")
    print(f"Kernel: {kernel}")
    print(f"Root filesystem source: {root_source}")
    print(f"Target: {args.bdf} ({read(device / 'vendor')}:{read(device / 'device')})")
    print(f"Firmware: {fw}")
    print("The upstream pipeline was tested by its author on a different Linux distribution.")
    return device, fw


def bind_vfio(device: Path) -> str:
    if os.geteuid() != 0:
        raise PreflightError("Run --execute with sudo after reviewing the preflight output.")
    subprocess.run(["modprobe", "iommufd"], check=True)
    subprocess.run(["modprobe", "vfio-pci"], check=True)
    if not Path("/dev/iommu").exists():
        raise PreflightError("/dev/iommu is unavailable after loading iommufd.")
    idle_d3 = Path("/sys/module/vfio_pci/parameters/disable_idle_d3")
    if not idle_d3.exists():
        raise PreflightError("vfio-pci does not expose disable_idle_d3 on this kernel.")
    idle_d3.write_text("Y", encoding="ascii")

    override = device / "driver_override"
    previous_override = override.read_text(encoding="ascii").strip() if override.exists() else ""
    driver_link = device / "driver"
    previous_driver = driver_link.resolve().name if driver_link.exists() else ""
    if previous_driver and previous_driver != "vfio-pci":
        (Path("/sys/bus/pci/drivers") / previous_driver / "unbind").write_text(
            device.name, encoding="ascii"
        )
    override.write_text("vfio-pci", encoding="ascii")
    try:
        (Path("/sys/bus/pci/drivers/vfio-pci") / "bind").write_text(
            device.name, encoding="ascii"
        )
        vfio_nodes = sorted((device / "vfio-dev").glob("vfio*"))
        if len(vfio_nodes) != 1:
            raise PreflightError(f"Expected one VFIO cdev for {device.name}; found {len(vfio_nodes)}.")
        return f"/dev/vfio/devices/{vfio_nodes[0].name}"
    except Exception:
        override.write_text(previous_override, encoding="ascii")
        if previous_driver and previous_driver != "vfio-pci":
            (Path("/sys/bus/pci/drivers") / previous_driver / "bind").write_text(
                device.name, encoding="ascii"
            )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bdf", required=True, help="Exact SSD PCI address, for example 0000:03:00.0")
    parser.add_argument(
        "--firmware-dir",
        default="~/netac-recovery",
        help="Directory containing m2_0536/ and the matching Netac firmware package",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Bind the selected controller to VFIO and run the destructive megaflash4 pipeline",
    )
    args = parser.parse_args()

    try:
        device, fw = preflight(args)
        if not args.execute:
            print("No device settings or firmware were changed.")
            print("After reviewing docs/rocky-linux.md, add --execute to start the write process.")
            return 0
        print()
        print("WARNING: this writes controller firmware and initializes NAND/FTL state.")
        print("It can permanently brick the SSD and destroy all data on it.")
        if input(f"Type exactly '{CONFIRMATION}' to continue: ").strip() != CONFIRMATION:
            print("Confirmation did not match; no firmware was written.")
            return 2

        cdev = bind_vfio(device)
        env = os.environ.copy()
        env["NETAC_CDEV"] = cdev
        env["NETAC_FW_DIR"] = str(fw.parent)
        env["NETAC_QSIZE"] = "2"
        print(f"Starting pipeline on {device.name} through {cdev}.")
        result = subprocess.run([sys.executable, str(PIPELINE), "megaflash4"], env=env, cwd=ROOT)
        print(f"Pipeline exit code: {result.returncode}")
        print("Power the computer fully off before rebooting; a warm restart is insufficient.")
        print("Do not unbind/rebind the controller or run the pipeline again after a partial failure.")
        return result.returncode
    except (PreflightError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
