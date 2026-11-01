# Linux recovery runner

The recovery pipeline writes IG5236 firmware and issues NAND/FTL initialization commands. It can permanently brick the SSD and erase its contents. The runner defaults to a read-only preflight; firmware writes require `--execute` and an exact typed confirmation.

## Support and limits

- Targets Rocky Linux 10 or Ubuntu 26.04 on x86_64 with a kernel that exposes VFIO device cdev and IOMMUFD (kernel 6.6 or newer). The Ubuntu path is intended for the Desktop live USB session.
- The original project was tested on a Netac NV7000 2 TB with InnoGrit IG5236 under CachyOS/Linux 7.2.3. The menu and runner have not been hardware-tested on Rocky or Ubuntu.
- The SSD must be the only device in its IOMMU group. Boot the host from a separate USB/SATA drive, not the SSD being recovered.
- The repository does not include firmware images. This branch's recovery command is hard-coded for a YMTC X2 NAND firmware package. Confirm the NAND package marking before proceeding; matching filenames do not prove firmware compatibility.
- The existing `mega4.sh` has a hard-coded PCI address and unloads the system-wide NVMe driver. Do not use it on a machine with NVMe system/storage devices. Use the runner below, which binds only the selected PCI device.

## Prepare

Use either Rocky Linux 10 booted from a separate disk or Ubuntu 26.04 Desktop via **Try Ubuntu** from a bootable USB. Ubuntu's live session avoids installing over Rocky 9.5; store the repository and matching firmware on a second USB or another non-target disk. The USB creation process erases the selected USB stick.

On Rocky, install standard tools and Python 3:

```sh
sudo dnf install python3 pciutils util-linux
```

On Ubuntu Live:

```sh
sudo apt update
sudo apt install python3 pciutils util-linux nvme-cli
```

Place the matching firmware package so these files exist:

```text
~/netac-recovery/m2_0536/loader.fw
~/netac-recovery/m2_0536/programmer+cust+3SJ11+ymtcx2.fw
~/netac-recovery/m2_0536/rdisk+cust+3SJ11+512b+hcrc+semi+l0ps+iecc+semiswitch+logerr+rel+mstripe+raid+a1perf_raid+ymtcx2-cmfg.fw
```

The BDF is the PCI address shown by `lspci -Dnn` for the IG5236 controller, for example `0000:03:00.0`. Confirm the address physically corresponds to the Netac NV7000 2 TB and verify that its NAND package is YMTC X2 before proceeding.

## Menu

From the repository root, start the interactive menu:

```sh
python3 tools/recovery/nv7000_menu.py
```

Use it to select the IG5236 PCI address, set the firmware package directory, run preflight, start recovery, and inspect the SSD afterward. Recovery runs only after preflight, NAND-variant confirmation, and two explicit start/erase confirmations. The menu does not download firmware or install packages automatically.

## Read-only preflight

From the repository root, run the checker without `--execute`:

```sh
python3 tools/recovery/rocky_nv7000_recover.py \
  --bdf 0000:03:00.0 \
  --firmware-dir ~/netac-recovery
```

It checks OS/kernel requirements, controller PCI identity, IOMMU-group isolation, root/boot-medium placement, and firmware files. It does not change device settings or write firmware.

## Firmware recovery

Only after confirming the target and firmware variant, run:

```sh
sudo python3 tools/recovery/rocky_nv7000_recover.py \
  --bdf 0000:03:00.0 \
  --firmware-dir /home/USER/netac-recovery \
  --execute
```

The program asks for the exact phrase `ERASE DATA AND RECOVER NETAC NV7000 2TB`. It then binds that PCI device to VFIO and launches the repository's `megaflash4` pipeline. Do not interrupt it. Afterward, shut the computer fully off before powering it back on; a warm reboot may leave the SSD in programmer mode.

If the pipeline fails or stops partway, do not retry or rebind the device blindly. Capture the console output and get technical review first.
