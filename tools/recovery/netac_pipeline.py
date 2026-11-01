#!/usr/bin/env python3
"""InnoGrit IG5236 recovery pipeline over VFIO cdev + iommufd.

Safety rules learned the hard way:
  - never touch parent port SBR, never remove/rescan PCI while device is idle
  - enable vfio-pci disable_idle_d3=Y before binding
  - keep this process alive; all stages in one run when possible
"""
import ctypes
import fcntl
import mmap
import os
import struct
import sys
import time
import zlib

CDEV = os.environ.get("NETAC_CDEV", "/dev/vfio/devices/vfio0")
IOVA_BASE = 0x10000000
MEM_SIZE = 0x10000
QSIZE = int(os.environ.get("NETAC_QSIZE", "2"))
# Directory with vendor firmware files (loader.fw, programmer*.fw, rdisk*.fw)
FW_DIR = os.environ.get("NETAC_FW_DIR", os.path.expanduser("~/netac-recovery"))
FW_M2 = os.path.join(FW_DIR, "m2_0536")

IOMMU_IOAS_ALLOC = 0x3B81
IOMMU_IOAS_MAP = 0x3B85
VFIO_DEVICE_BIND_IOMMUFD = 0x3B76
VFIO_DEVICE_ATTACH_IOMMUFD_PT = 0x3B77
VFIO_DEVICE_GET_INFO = 0x3B6B
VFIO_DEVICE_GET_REGION_INFO = 0x3B6C

REG_CC = 0x14
REG_CSTS = 0x1C
REG_AQA = 0x24
REG_ASQ = 0x28
REG_ACQ = 0x30
SQ0TDBL = 0x1000

MAGIC_GRIT = 0x54495247
MAGIC_INNO = 0x4F4E4E49

ASQ_OFF = 0x0000
ACQ_OFF = 0x1000
BUF_OFF = 0x2000
BUF2_OFF = 0x3000


class VfioCdev:
    def __init__(self, cdev):
        self.iommufd = os.open("/dev/iommu", os.O_RDWR)
        buf = bytearray(12)
        struct.pack_into("<III", buf, 0, 12, 0, 0)
        fcntl.ioctl(self.iommufd, IOMMU_IOAS_ALLOC, buf, True)
        self.ioas_id = struct.unpack_from("<I", buf, 8)[0]
        self.devfd = os.open(cdev, os.O_RDWR)
        b = bytearray(24)
        struct.pack_into("<IIiI", b, 0, 24, 0, self.iommufd, 0)
        fcntl.ioctl(self.devfd, VFIO_DEVICE_BIND_IOMMUFD, b, True)
        self.devid = struct.unpack_from("<I", b, 12)[0]
        a = bytearray(16)
        struct.pack_into("<IIII", a, 0, 16, 0, self.ioas_id, 0)
        fcntl.ioctl(self.devfd, VFIO_DEVICE_ATTACH_IOMMUFD_PT, a, True)
        info = bytearray(16)
        struct.pack_into("<I", info, 0, 16)
        fcntl.ioctl(self.devfd, VFIO_DEVICE_GET_INFO, info, True)
        self.num_regions, self.num_irqs = struct.unpack_from("<II", info, 4)
        chosen = None
        for idx in range(self.num_regions):
            rinfo = bytearray(32)
            struct.pack_into("<I", rinfo, 0, 32)
            struct.pack_into("<I", rinfo, 8, idx)
            fcntl.ioctl(self.devfd, VFIO_DEVICE_GET_REGION_INFO, rinfo, True)
            rflags, rindex, rcap, rsize, roff = struct.unpack_from("<IIIQQ", rinfo, 4)
            if rsize >= 0x4000 and (rflags & 4) and chosen is None:
                chosen = (rflags, rsize, roff)
        if chosen is None:
            raise RuntimeError("BAR region not found")
        self.bar_flags, self.bar_size, self.bar_offset = chosen
        self.bar = mmap.mmap(self.devfd, self.bar_size, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE, offset=self.bar_offset)
        self.mem = mmap.mmap(-1, MEM_SIZE)
        self.mem_host = ctypes.addressof(ctypes.c_char.from_buffer(self.mem))
        m = bytearray(40)
        struct.pack_into("<IIIIQQQ", m, 0, 40, 7, self.ioas_id, 0,
                         self.mem_host, MEM_SIZE, IOVA_BASE)
        fcntl.ioctl(self.iommufd, IOMMU_IOAS_MAP, m, True)

    def rd32(self, off):
        return struct.unpack_from("<I", self.bar, off)[0]

    def rd64(self, off):
        return struct.unpack_from("<Q", self.bar, off)[0]

    def wr32(self, off, v):
        struct.pack_into("<I", self.bar, off, v)

    def wr64(self, off, v):
        struct.pack_into("<Q", self.bar, off, v)

    def dev_addr(self, off):
        return IOVA_BASE + off

    def write_mem(self, off, data):
        ctypes.memmove(ctypes.c_void_p(self.mem_host + off), data, len(data))

    def read_mem(self, off, n):
        return ctypes.string_at(self.mem_host + off, n)

    def zero_mem(self, off, n):
        ctypes.memset(ctypes.c_void_p(self.mem_host + off), 0, n)


class Nvme:
    def __init__(self, backend):
        self.b = backend
        self.cap = self.b.rd64(0)
        self.dstrd = (self.cap >> 32) & 0xF
        self.qsize = QSIZE
        self.phase = 1
        self.sq_tail = 0
        self.cq_head = 0
        self.cid = 1
        self.cq_db_off = SQ0TDBL + 4 * (2 ** self.dstrd)

    def regs(self):
        return (f"VS=0x{self.b.rd32(8):08x} CC=0x{self.b.rd32(REG_CC):08x} "
                f"CSTS=0x{self.b.rd32(REG_CSTS):08x} AQA=0x{self.b.rd32(REG_AQA):08x}")

    def disable(self, timeout=5.0):
        cc = self.b.rd32(REG_CC)
        if cc & 1:
            self.b.wr32(REG_CC, cc & ~1)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not (self.b.rd32(REG_CSTS) & 1):
                return True
            time.sleep(0.02)
        return not (self.b.rd32(REG_CSTS) & 1)

    def enable(self, timeout=30.0):
        self.disable()
        self.b.wr32(REG_AQA, (self.qsize - 1) | ((self.qsize - 1) << 16))
        self.b.wr64(REG_ASQ, self.b.dev_addr(ASQ_OFF))
        self.b.wr64(REG_ACQ, self.b.dev_addr(ACQ_OFF))
        self.phase = 1
        self.sq_tail = 0
        self.cq_head = 0
        self.b.zero_mem(0, MEM_SIZE)
        self.b.wr32(REG_CC, (6 << 16) | (4 << 20) | 1)
        t0 = time.time()
        while time.time() - t0 < timeout:
            csts = self.b.rd32(REG_CSTS)
            if csts & 2:
                return ("fatal", csts, time.time() - t0)
            if csts & 1:
                return ("ready", csts, time.time() - t0)
            time.sleep(0.5)
        return ("timeout", self.b.rd32(REG_CSTS), timeout)

    def submit(self, dwords, timeout=10.0):
        cid = self.cid
        self.cid = (self.cid % 0xFFFE) + 1
        dwords[0] = (dwords[0] & 0xFFFF) | (cid << 16)
        sqe = struct.pack("<16I", *dwords)
        slot = self.sq_tail
        self.b.write_mem(ASQ_OFF + slot * 64, sqe)
        self.sq_tail = (self.sq_tail + 1) % self.qsize
        self.b.wr32(SQ0TDBL, self.sq_tail)
        t0 = time.time()
        while time.time() - t0 < timeout:
            dw3 = struct.unpack_from("<I", self.b.mem, ACQ_OFF + self.cq_head * 16 + 12)[0]
            if ((dw3 >> 16) & 1) == self.phase:
                dw0 = struct.unpack_from("<I", self.b.mem, ACQ_OFF + self.cq_head * 16)[0]
                status = (dw3 >> 17) & 0x7FFF
                self.cq_head = (self.cq_head + 1) % self.qsize
                self.b.wr32(self.cq_db_off, self.cq_head)
                if self.cq_head == 0:
                    self.phase ^= 1
                return cid, dw0, status
            time.sleep(0.002)
        return None, None, None

    def admin(self, opcode, cdw10=0, cdw11=0, cdw12=0, cdw13=0, cdw14=0, cdw15=0,
              nsid=0xFFFFFFFF, prp1=None, prp2=None, timeout=10.0, verbose=True):
        dwords = [0] * 16
        dwords[0] = opcode
        dwords[1] = nsid
        if prp1 is not None:
            dwords[6] = prp1 & 0xFFFFFFFF
            dwords[7] = (prp1 >> 32) & 0xFFFFFFFF
        if prp2 is not None:
            dwords[8] = prp2 & 0xFFFFFFFF
            dwords[9] = (prp2 >> 32) & 0xFFFFFFFF
        dwords[10] = cdw10
        dwords[11] = cdw11
        dwords[12] = cdw12
        dwords[13] = cdw13
        dwords[14] = cdw14
        dwords[15] = cdw15
        c, dw0, status = self.submit(dwords, timeout=timeout)
        if verbose:
            if c is None:
                print(f"  opcode 0x{opcode:02x} -> TIMEOUT")
            else:
                print(f"  opcode 0x{opcode:02x} -> status 0x{status:04x} dw0=0x{dw0:08x}")
        return status

    def identify_controller(self, verbose=True):
        st = self.admin(0x06, cdw10=1, nsid=0, prp1=self.b.dev_addr(BUF_OFF), verbose=verbose)
        d = self.b.read_mem(BUF_OFF, 4096)
        if st == 0 and verbose:
            sn = d[4:24].decode("latin1").strip("\x00 ")
            mn = d[24:64].decode("latin1").strip("\x00 ")
            fr = d[64:72].decode("latin1").strip("\x00 ")
            print(f"    SN={sn!r} MN={mn!r} FR={fr!r}")
        return d

    def get_fwconfig(self, verbose=True, save=None):
        self.b.zero_mem(BUF_OFF, 4096)
        st = self.admin(0xE6, cdw10=0x400, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO,
                        prp1=self.b.dev_addr(BUF_OFF), verbose=verbose)
        d = self.b.read_mem(BUF_OFF, 4096)
        if st == 0 and verbose:
            print(f"    fwconfig[0..64]={d[:64].hex()}")
            print(f"    ascii: {d[:64]!r}")
        if save and st == 0:
            with open(save, "wb") as f:
                f.write(d)
        return d if st == 0 else None

    def get_mpinfo(self, page=0, verbose=True):
        self.b.zero_mem(BUF_OFF, 4096)
        st = self.admin(0xF2, cdw10=0x400, cdw12=page, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO,
                        prp1=self.b.dev_addr(BUF_OFF), verbose=verbose)
        return self.b.read_mem(BUF_OFF, 4096) if st == 0 else None

    def to_recovery(self):
        return self.admin(0xF7, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=15)

    def set_fwconfig(self, page):
        assert len(page) == 4096
        crc = zlib.crc32(page) & 0xFFFFFFFF
        print(f"  SetFWconfig (0xE5) crc32=0x{crc:08x} send=0x{(~crc) & 0xFFFFFFFF:08x}")
        self.b.zero_mem(BUF_OFF, 4096)
        self.b.write_mem(BUF_OFF, page)
        return self.admin(0xE5, cdw10=0x400, cdw12=1, cdw13=0,
                          cdw14=(~crc) & 0xFFFFFFFF, cdw15=0,
                          prp1=self.b.dev_addr(BUF_OFF), timeout=20)

    def fw_download(self, data, chunk=4096, verbose=True, timeout=15.0, gap=0.0):
        n = len(data)
        off = 0
        idx = 0
        t0 = time.time()
        while off < n:
            part = data[off:off + chunk]
            self.b.zero_mem(BUF_OFF, 4096)
            self.b.write_mem(BUF_OFF, part)
            numd = (len(part) - 1) >> 2
            st = self.admin(0x11, cdw10=numd, cdw11=off >> 2, nsid=0xFFFFFFFF,
                            prp1=self.b.dev_addr(BUF_OFF), verbose=False, timeout=timeout)
            if st != 0:
                print(f"    fw chunk #{idx} @0x{off:x} FAILED status={st} (CSTS={self.b.rd32(REG_CSTS):08x})")
                return False
            if verbose:
                print(f"    chunk #{idx} @0x{off:x}/{n} ok ({off*100//n}%)")
            idx += 1
            off += len(part)
            if gap:
                time.sleep(gap)
        print(f"    download complete: {n} bytes in {time.time()-t0:.2f}s")
        return True

    def fw_commit(self, action_slot=0x19, timeout=60):
        print(f"  FW Commit (0x10) cdw10=0x{action_slot:x}")
        return self.admin(0x10, cdw10=action_slot, nsid=0xFFFFFFFF, timeout=timeout)


def cmfg_conf_page(data):
    if data[:4] != b"CMFG":
        return None
    total = struct.unpack_from("<I", data, 8)[0]
    segs = [struct.unpack_from("<II", data, 0x10 + 8 * i) for i in range(8)]
    for (s, e) in segs:
        if s == 0 and e == 0:
            continue
        page = data[s * 0x1000:(s + 1) * 0x1000]
        if page[:4] == b"CONF":
            return page, (s, e)
    return None, segs


def flash_file(nv, path, with_conf=True):
    with open(path, "rb") as f:
        data = f.read()
    print(f"== flash {os.path.basename(path)}: {len(data)} bytes ({len(data)//4096} pages)")
    conf = None
    if with_conf and data[:4] == b"CMFG":
        conf, info = cmfg_conf_page(data)
        print(f"   CMFG descriptors={info} conf={'yes' if conf else 'no'}")
    ok = nv.fw_download(data)
    if not ok:
        return False
    if conf:
        st = nv.set_fwconfig(conf)
        if st != 0:
            print(f"   set_fwconfig failed status={st}, continuing")
    return nv.fw_commit(0x19) == 0


def stage(nv, name):
    status, csts, dt = nv.enable(timeout=30)
    print(f"[{name}] enable -> {status} CSTS=0x{csts:08x} in {dt:.1f}s")
    if status != "ready":
        return False
    print(f"[{name}] {nv.regs()}")
    print(f"[{name}] fwconfig:")
    nv.get_fwconfig(save=os.path.join(FW_DIR, f"fwconfig_{name}.bin"))
    return True


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    print(f"== Netac IG5236 pipeline: {cmd} ==")
    backend = VfioCdev(CDEV)
    print(f"VFIO ioas={backend.ioas_id} devid={backend.devid} BAR0 size=0x{backend.bar_size:x}")
    nv = Nvme(backend)
    print(f"CAP=0x{nv.cap:016x} VS=0x{backend.rd32(8):08x} {nv.regs()}")

    if cmd == "status":
        if stage(nv, "status"):
            nv.identify_controller()
            d = nv.get_mpinfo(0)
            if d:
                print(f"  mpinfo[0..64]={d[:64].hex()}")
    elif cmd == "recovery":
        if stage(nv, "recovery"):
            st = nv.to_recovery()
            print(f"  ToRecovery 0xF7 status={st}")
            time.sleep(2)
            print(f"  {nv.regs()}")
    elif cmd == "flash":
        path = sys.argv[2]
        if not stage(nv, "pre"):
            sys.exit(2)
        flash_file(nv, path)
        time.sleep(8)
        stage(nv, "post")
        nv.identify_controller()
    elif cmd == "megaflash":
        base = FW_M2 + "/"
        loader = base + "loader.fw"
        programmer = base + "programmer+cust+3SJ11+ymtcx2.fw"
        rdisk = base + "rdisk+cust+3SJ11+512b+hcrc+semi+l0ps+iecc+semiswitch+logerr+rel+mstripe+raid+a1perf_raid+ymtcx2-cmfg.fw"

        def probe_once(timeout=3.0):
            status, csts, dt = nv.enable(timeout=timeout)
            if status != "ready":
                return None
            st = nv.admin(0x06, cdw10=1, nsid=0, prp1=nv.b.dev_addr(BUF_OFF),
                          timeout=1.5, verbose=False)
            if st is None:
                return None
            d = nv.b.read_mem(BUF_OFF, 4096)
            sn = d[4:24].decode("latin1").strip("\x00 ")
            mn = d[24:64].decode("latin1").strip("\x00 ")
            fr = d[64:72].decode("latin1").strip("\x00 ")
            return st, sn, mn, fr

        def wait_ident(seconds, label):
            t0 = time.time()
            while time.time() - t0 < seconds:
                r = probe_once(2.0)
                if r:
                    status, sn, mn, fr = r
                    print(f"[{label}] status={status} SN={sn!r} MN={mn!r} FR={fr!r}")
                    if status == 0 and (sn or mn):
                        return (sn, mn, fr)
                time.sleep(1)
            print(f"[{label}] no valid identity in {seconds}s")
            return None

        print("========== PHASE 0: probe ==========")
        ident = wait_ident(40, "phase0")
        if not ident:
            sys.exit(3)
        sn, mn, fr = ident
        rom_signature = sn == "SSD-5236" and mn == "MN-5236" and fr == "2.0.0.3"
        runtime_signature = (
            mn == "INNOGRIT"
            and sn in {"SSD-DRAM-120216-V", "SSD-DRAM-120216-F"}
            and fr == "3.S.J.11"
        )
        if not (rom_signature or runtime_signature):
            print(
                "Refusing to write: unsupported initial identity. Expected the documented "
                "Netac IG5236 ROM or 3.S.J.11 runtime signature; got "
                f"SN={sn!r} MN={mn!r} FR={fr!r}"
            )
            sys.exit(4)

        if "MN-5236" in mn or "SSD-5236" in sn:
            print("== ROM personality: ToRecovery + loader ==")
            nv.enable(timeout=20)
            print("ToRecovery:", nv.to_recovery())
            time.sleep(2)
            ok = flash_file(nv, loader)
            print("loader flash ok:", ok)
            ident = wait_ident(60, "post-loader")
            if not ident:
                sys.exit(3)
            sn, mn, fr = ident

        if "INNOGRIT" not in mn and "SSD-DRAM" not in sn:
            print("== flashing programmer ==")
            nv.enable(timeout=20)
            ok = flash_file(nv, programmer)
            print("programmer flash ok:", ok)
            ident = wait_ident(120, "post-programmer")
            if not ident:
                sys.exit(3)
            sn, mn, fr = ident

        print("========== PHASE 2: DDR/mpinfo ==========")
        nv.enable(timeout=20)
        print("DDR eye:", nv.admin(0xE4, cdw12=3, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=30))
        time.sleep(5)
        for i in range(12):
            d = nv.get_mpinfo(0, verbose=False)
            print(f"mpinfo {i}: {'ok' if d else 'no'}")
            if d:
                print(f"  mpinfo[0..32]={d[:32].hex()}")
                break
            time.sleep(5)

        print("========== PHASE 3: rdisk ==========")
        nv.enable(timeout=20)
        ok = flash_file(nv, rdisk)
        print("rdisk flash ok:", ok)
        ident = wait_ident(120, "post-rdisk")
        print("post-rdisk:", ident)

        print("========== PHASE 4: finalize ==========")
        nv.enable(timeout=20)
        print("SmartClean:", nv.admin(0xD0, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        print("RemoveCheck:", nv.admin(0xE4, cdw12=5, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        print("BrandNew:", nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        ident = wait_ident(10, "final")
        print("FINAL IDENT:", ident)
    elif cmd == "megaflash3":
        base = FW_M2 + "/"
        loader = base + "loader.fw"
        programmer = base + "programmer+cust+3SJ11+ymtcx2.fw"
        rdt = base + "programmer+cust+3SJ11+rdt+ymtcx2-cmfg.fw"
        rdisk = base + "rdisk+cust+3SJ11+512b+hcrc+semi+l0ps+iecc+semiswitch+logerr+rel+mstripe+raid+a1perf_raid+ymtcx2-cmfg.fw"

        def probe_once(timeout=3.0):
            status, csts, dt = nv.enable(timeout=timeout)
            if status != "ready":
                return None
            st = nv.admin(0x06, cdw10=1, nsid=0, prp1=nv.b.dev_addr(BUF_OFF),
                          timeout=1.5, verbose=False)
            if st is None:
                return None
            d = nv.b.read_mem(BUF_OFF, 4096)
            sn = d[4:24].decode("latin1").strip("\x00 ")
            mn = d[24:64].decode("latin1").strip("\x00 ")
            fr = d[64:72].decode("latin1").strip("\x00 ")
            return st, sn, mn, fr

        def wait_ident(seconds, label):
            t0 = time.time()
            while time.time() - t0 < seconds:
                r = probe_once(3.0)
                if r:
                    status, sn, mn, fr = r
                    print(f"[{label}] status={status} SN={sn!r} MN={mn!r} FR={fr!r}")
                    if status == 0 and (sn or mn):
                        return (sn, mn, fr)
                time.sleep(1)
            print(f"[{label}] no valid identity in {seconds}s")
            return None

        def try_download(data, label):
            variants = [
                ("std", 0xFFFFFFFF, None, 0),
                ("conf_first", "CONF", None, 0),
                ("nsid0", 0, None, 0),
                ("numd_bytes", 0xFFFFFFFF, 4096, 0),
                ("cdw12_1", 0xFFFFFFFF, None, 1),
            ]
            conf, _ = cmfg_conf_page(data) if data[:4] == b"CMFG" else (None, None)
            for name, nsid, numd_override, cdw12 in variants:
                if name == "conf_first":
                    if not conf:
                        continue
                    print(f"  [{label}/conf_first] sending CONF via 0xE5 first")
                    st = nv.set_fwconfig(conf)
                    if st != 0:
                        print(f"  [{label}/conf_first] 0xE5 status={st}, skipping")
                        continue
                    nsid = 0xFFFFFFFF
                off = 0
                ok = True
                while off < len(data):
                    part = data[off:off + 4096]
                    nv.b.zero_mem(BUF_OFF, 4096)
                    nv.b.write_mem(BUF_OFF, part)
                    numd = numd_override if numd_override else ((len(part) - 1) >> 2)
                    st = nv.admin(0x11, cdw10=numd, cdw11=off >> 2, cdw12=cdw12, nsid=nsid,
                                  prp1=nv.b.dev_addr(BUF_OFF), timeout=15, verbose=False)
                    if st != 0:
                        print(f"  [{label}/{name}] chunk #{off // 4096} status={st}")
                        ok = False
                        break
                    off += len(part)
                if ok:
                    print(f"  [{label}/{name}] DOWNLOAD OK")
                    if name != "conf_first" and conf:
                        print(f"  [{label}] sending CONF via 0xE5")
                        print("  0xE5:", nv.set_fwconfig(conf))
                    return True
            return False

        print("========== PHASE 0 ==========")
        ident = wait_ident(40, "phase0")
        if not ident:
            sys.exit(3)
        sn, mn, fr = ident

        if "MN-5236" in mn or "SSD-5236" in sn:
            print("== ROM: ToRecovery + loader ==")
            nv.enable(timeout=20)
            print("ToRecovery:", nv.to_recovery())
            time.sleep(2)
            print("loader:", flash_file(nv, loader))
            ident = wait_ident(60, "post-loader")
            if not ident:
                sys.exit(3)
            sn, mn, fr = ident

        if "INNOGRIT" not in mn and "SSD-DRAM" not in sn:
            print("== programmer ==")
            nv.enable(timeout=20)
            print("programmer:", flash_file(nv, programmer))
            ident = wait_ident(120, "post-programmer")
            if not ident:
                sys.exit(3)
            sn, mn, fr = ident

        print("========== PHASE 2: DDR/mpinfo/prep ==========")
        nv.enable(timeout=20)
        print("DDR eye:", nv.admin(0xE4, cdw12=3, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=30))
        time.sleep(3)
        for i in range(8):
            d = nv.get_mpinfo(0, verbose=False)
            print(f"mpinfo {i}: {'ok' if d else 'no'}")
            if d:
                break
            time.sleep(5)
        print("BrandNew:", nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        print("RemoveCheck:", nv.admin(0xE4, cdw12=5, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        time.sleep(2)

        print("========== PHASE 3: rdt + rdisk ==========")
        for path in (rdt, rdisk):
            with open(path, "rb") as f:
                data = f.read()
            print(f"== {os.path.basename(path)} ({len(data)} bytes)")
            nv.enable(timeout=20)
            if try_download(data, os.path.basename(path)[:16]):
                print("commit 0x19:", nv.fw_commit(0x19, timeout=180))
                ident = wait_ident(120, "post-commit")
                print("  identity:", ident)
            else:
                print("  download failed for", path)

        print("========== PHASE 4: finalize ==========")
        nv.enable(timeout=20)
        print("SmartClean:", nv.admin(0xD0, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        print("BrandNew:", nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        ident = wait_ident(10, "final")
        print("FINAL IDENT:", ident)
    elif cmd == "megaflash4":
        base = FW_M2 + "/"
        loader = base + "loader.fw"
        programmer = base + "programmer+cust+3SJ11+ymtcx2.fw"
        rdisk = base + "rdisk+cust+3SJ11+512b+hcrc+semi+l0ps+iecc+semiswitch+logerr+rel+mstripe+raid+a1perf_raid+ymtcx2-cmfg.fw"

        def probe_once(timeout=3.0):
            status, csts, dt = nv.enable(timeout=timeout)
            if status != "ready":
                return None
            st = nv.admin(0x06, cdw10=1, nsid=0, prp1=nv.b.dev_addr(BUF_OFF),
                          timeout=1.5, verbose=False)
            if st is None:
                return None
            d = nv.b.read_mem(BUF_OFF, 4096)
            sn = d[4:24].decode("latin1").strip("\x00 ")
            mn = d[24:64].decode("latin1").strip("\x00 ")
            fr = d[64:72].decode("latin1").strip("\x00 ")
            return st, sn, mn, fr

        def wait_ident(seconds, label):
            t0 = time.time()
            while time.time() - t0 < seconds:
                r = probe_once(3.0)
                if r:
                    status, sn, mn, fr = r
                    print(f"[{label}] status={status} SN={sn!r} MN={mn!r} FR={fr!r}")
                    if status == 0 and (sn or mn):
                        return (sn, mn, fr)
                time.sleep(1)
            print(f"[{label}] no valid identity in {seconds}s")
            return None

        def download_nsid0(data, label):
            off = 0
            while off < len(data):
                part = data[off:off + 4096]
                nv.b.zero_mem(BUF_OFF, 4096)
                nv.b.write_mem(BUF_OFF, part)
                numd = (len(part) - 1) >> 2
                st = nv.admin(0x11, cdw10=numd, cdw11=off >> 2, nsid=0,
                              prp1=nv.b.dev_addr(BUF_OFF), timeout=15, verbose=False)
                if st != 0:
                    print(f"  [{label}] chunk #{off // 4096} status={st}")
                    return False
                if off % (4096 * 64) == 0:
                    print(f"  [{label}] chunk #{off // 4096} ok")
                off += len(part)
            print(f"  [{label}] DOWNLOAD OK ({len(data)} bytes)")
            return True

        def commit_try(label):
            for cdw10 in (0x19, 0x09, 0x0B, 0x0F, 0x11, 0x01, 0x03):
                st = nv.admin(0x10, cdw10=cdw10, nsid=0, timeout=180, verbose=False)
                print(f"  [{label}] commit cdw10=0x{cdw10:02x} nsid=0 -> {st}")
                if st == 0:
                    return cdw10
                if st is None:
                    time.sleep(3)
            return None

        print("========== PHASE 0 ==========")
        ident = wait_ident(40, "phase0")
        if not ident:
            sys.exit(3)
        sn, mn, fr = ident

        if "MN-5236" in mn or "SSD-5236" in sn:
            print("== ROM: ToRecovery + loader ==")
            nv.enable(timeout=20)
            print("ToRecovery:", nv.to_recovery())
            time.sleep(2)
            print("loader:", flash_file(nv, loader))
            ident = wait_ident(60, "post-loader")
            if not ident:
                sys.exit(3)
            sn, mn, fr = ident

        if "INNOGRIT" not in mn and "SSD-DRAM" not in sn:
            print("== programmer ==")
            nv.enable(timeout=20)
            print("programmer:", flash_file(nv, programmer))
            ident = wait_ident(120, "post-programmer")
            if not ident:
                sys.exit(3)
            sn, mn, fr = ident

        print("========== PHASE 2 ==========")
        nv.enable(timeout=20)
        print("DDR eye:", nv.admin(0xE4, cdw12=3, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=30))
        time.sleep(3)
        for i in range(8):
            d = nv.get_mpinfo(0, verbose=False)
            print(f"mpinfo {i}: {'ok' if d else 'no'}")
            if d:
                break
            time.sleep(5)
        print("BrandNew:", nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        print("RemoveCheck:", nv.admin(0xE4, cdw12=5, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        time.sleep(2)

        print("========== PHASE 3: rdisk (nsid=0) ==========")
        with open(rdisk, "rb") as f:
            data = f.read()
        nv.enable(timeout=20)
        if download_nsid0(data, "rdisk"):
            conf, _ = cmfg_conf_page(data) if data[:4] == b"CMFG" else (None, None)
            if conf:
                print("  CONF 0xE5:", nv.set_fwconfig(conf))
            print("  sleep 3s before commit")
            time.sleep(3)
            print("commit result:", commit_try("rdisk"))
            ident = wait_ident(180, "post-rdisk")
            print("  identity:", ident)
        else:
            print("rdisk download failed")

        print("========== PHASE 4: finalize ==========")
        nv.enable(timeout=20)
        print("SmartClean:", nv.admin(0xD0, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        print("BrandNew:", nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO, timeout=60))
        ident = wait_ident(10, "final")
        print("FINAL IDENT:", ident)
    elif cmd == "bar4":
        for idx in range(nv.b.num_regions):
            rinfo = bytearray(32)
            struct.pack_into("<I", rinfo, 0, 32)
            struct.pack_into("<I", rinfo, 8, idx)
            fcntl.ioctl(nv.b.devfd, VFIO_DEVICE_GET_REGION_INFO, rinfo, True)
            rflags, rindex, rcap, rsize, roff = struct.unpack_from("<IIIQQ", rinfo, 4)
            print(f"region[{idx}] flags=0x{rflags:x} size=0x{rsize:x} off=0x{roff:x}")
            if rsize >= 0x10000 and (rflags & 4):
                m = mmap.mmap(nv.b.devfd, rsize, mmap.MAP_SHARED,
                              mmap.PROT_READ | mmap.PROT_WRITE, offset=roff)
                for off in range(0, 0x800, 16):
                    row = m[off:off + 16]
                    print(f"  {off:04x}: {row.hex()}  {''.join(chr(b) if 32 <= b < 127 else '.' for b in row)}")
                break
    elif cmd == "spam":
        nv.b.wr32(REG_AQA, (nv.qsize - 1) | ((nv.qsize - 1) << 16))
        nv.b.wr64(REG_ASQ, nv.b.dev_addr(ASQ_OFF))
        nv.b.wr64(REG_ACQ, nv.b.dev_addr(ACQ_OFF))
        nv.b.wr32(REG_CC, (6 << 16) | (4 << 20) | 1)
        cid = 0
        hits = 0
        t0 = time.time()
        while time.time() - t0 < 20:
            csts = nv.b.rd32(REG_CSTS)
            if not (csts & 1):
                time.sleep(0.005)
                continue
            cid += 1
            for op, c10 in ((0xF7, 0), (0x06, 1)):
                dwords = [0] * 16
                dwords[0] = op | ((cid & 0xFFFF) << 16)
                dwords[1] = 0
                dwords[6] = nv.b.dev_addr(BUF_OFF) & 0xFFFFFFFF
                dwords[7] = nv.b.dev_addr(BUF_OFF) >> 32
                dwords[10] = c10
                dwords[12] = 1 if op == 0xF7 else 0
                dwords[14] = MAGIC_GRIT
                dwords[15] = MAGIC_INNO
                sqe = struct.pack("<16I", *dwords)
                slot = nv.sq_tail
                nv.b.write_mem(ASQ_OFF + slot * 64, sqe)
                nv.sq_tail = (nv.sq_tail + 1) % nv.qsize
                nv.b.wr32(SQ0TDBL, nv.sq_tail)
                time.sleep(0.002)
                dw3 = struct.unpack_from("<I", nv.b.mem, ACQ_OFF + nv.cq_head * 16 + 12)[0]
                if ((dw3 >> 16) & 1) == nv.phase:
                    status = (dw3 >> 17) & 0x7FFF
                    print(f"HIT cid={cid} op=0x{op:02x} status={status} t={time.time()-t0:.3f}")
                    hits += 1
                    nv.cq_head = (nv.cq_head + 1) % nv.qsize
                    nv.b.wr32(nv.cq_db_off, nv.cq_head)
                    if nv.cq_head == 0:
                        nv.phase ^= 1
        print("hits:", hits)
    elif cmd == "kick":
        print("=== try CC.EN toggle + NSSR ===")
        nv.b.wr32(REG_CC, (1 << 18) | 1)
        time.sleep(3)
        print("after NSSR:", nv.regs())
        s, c, _ = nv.enable(timeout=15)
        print("enable:", s, f"CSTS=0x{c:08x}")
        st = nv.admin(0x06, cdw10=1, nsid=0, prp1=nv.b.dev_addr(BUF_OFF), timeout=3, verbose=False)
        print("identify after NSSR:", st)
        if st == 0:
            d = nv.b.read_mem(BUF_OFF, 4096)
            print("  SN=", d[4:24], " MN=", d[24:64])
            sys.exit(0)
        print("=== doorbell sweep ===")
        for qs in (2, 4, 32):
            nv.qsize = qs
            for off in (0x1004, 0x1008, 0x1010, 0x1020, 0x1040, 0x1080):
                nv.cq_db_off = off
                nv.enable(timeout=10)
                st = nv.admin(0x06, cdw10=1, nsid=0, prp1=nv.b.dev_addr(BUF_OFF),
                              timeout=3, verbose=False)
                print(f"  qsize={qs} cqdb=0x{off:x} -> {st}")
                if st == 0:
                    d = nv.b.read_mem(BUF_OFF, 4096)
                    print("  WORKS: SN=", d[4:24], " MN=", d[24:64])
                    sys.exit(0)
        print("no luck")
    elif cmd == "waitflash":
        path = sys.argv[2]
        ready = False
        for attempt in range(40):
            status, csts, dt = nv.enable(timeout=20)
            if status == "ready":
                st = nv.admin(0x06, cdw10=1, nsid=0, prp1=nv.b.dev_addr(BUF_OFF),
                              timeout=5, verbose=False)
                if st == 0:
                    d = nv.b.read_mem(BUF_OFF, 4096)
                    sn = d[4:24].decode("latin1").strip("\x00 ")
                    mn = d[24:64].decode("latin1").strip("\x00 ")
                    print(f"[waitflash] responsive after {attempt} tries: SN={sn!r} MN={mn!r}")
                    ready = True
                    break
            if attempt % 4 == 0:
                print(f"[waitflash] attempt {attempt}: no response (CSTS=0x{csts:08x})")
            time.sleep(5)
        if not ready:
            print("[waitflash] controller never became responsive")
            sys.exit(3)
        print(f"===== flashing {os.path.basename(path)} =====")
        ok = flash_file(nv, path)
        print("flash ok:", ok)
        time.sleep(8)
        stage(nv, "post")
        nv.identify_controller()
    elif cmd == "multiflash":
        if not stage(nv, "pre"):
            sys.exit(2)
        for path in sys.argv[2:]:
            print(f"===== {os.path.basename(path)} =====")
            try:
                ok = flash_file(nv, path)
                print("flash ok:", ok)
            except Exception as e:
                print("flash exception:", e)
            time.sleep(8)
            print("regs:", nv.regs())
            stage(nv, "post")
            nv.identify_controller()
            time.sleep(3)
    elif cmd == "finalize":
        if not stage(nv, "finalize"):
            sys.exit(2)
        print("SmartClean:", nv.admin(0xD0, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO))
        print("RemoveCheckFtlBuild:", nv.admin(0xE4, cdw12=5, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO))
        print("BrandNew:", nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO))
        time.sleep(5)
        stage(nv, "final")
        nv.identify_controller()
    elif cmd == "full":
        base = FW_M2 + "/"
        rdt = base + "programmer+cust+3SJ11+rdt+ymtcx2-cmfg.fw"
        rdisk = base + "rdisk+cust+3SJ11+512b+hcrc+semi+l0ps+iecc+semiswitch+logerr+rel+mstripe+raid+a1perf_raid+ymtcx2-cmfg.fw"
        if not stage(nv, "pre"):
            sys.exit(2)
        for path in (rdt, rdisk):
            print(f"===== {os.path.basename(path)} =====")
            try:
                ok = flash_file(nv, path)
                print("flash ok:", ok)
            except Exception as e:
                print("flash exception:", e)
            time.sleep(10)
            print("regs:", nv.regs())
            stage(nv, "post")
            nv.identify_controller()
        print("===== finalize =====")
        nv.admin(0xD0, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO)
        nv.admin(0xE4, cdw12=5, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO)
        nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO)
        time.sleep(5)
        stage(nv, "final")
        nv.identify_controller()
    elif cmd == "fwtest":
        if not stage(nv, "fwtest"):
            sys.exit(2)
        path = os.path.join(FW_M2, "programmer+cust+3SJ11+ymtcx2.fw")
        with open(path, "rb") as f:
            data = f.read(0x4000)
        variants = [
            ("nsid=ffffffff numd=1023", 0xFFFFFFFF, 1023, 4096, None),
            ("nsid=0 numd=1023", 0, 1023, 4096, None),
            ("nsid=1 numd=1023", 1, 1023, 4096, None),
            ("nsid=ffffffff numd=1024", 0xFFFFFFFF, 1024, 4096, None),
            ("nsid=ffffffff numd=2047 8K", 0xFFFFFFFF, 2047, 8192, "prp2"),
            ("nsid=ffffffff numd=0 (4b)", 0xFFFFFFFF, 0, 4, None),
            ("nsid=ffffffff numd=1023 cdw12=1", 0xFFFFFFFF, 1023, 4096, "cdw12"),
        ]
        for name, nsid, numd, length, extra in variants:
            nv.b.zero_mem(BUF_OFF, 0x3000)
            nv.b.write_mem(BUF_OFF, data[:length])
            prp2 = nv.b.dev_addr(BUF_OFF + 0x1000) if extra == "prp2" else None
            st = nv.admin(0x11, cdw10=numd, cdw11=0, cdw12=(1 if extra == "cdw12" else 0),
                          nsid=nsid, prp1=nv.b.dev_addr(BUF_OFF), prp2=prp2,
                          timeout=10, verbose=False)
            print(f"  {name}: status={st} CSTS=0x{nv.b.rd32(REG_CSTS):08x}")
    elif cmd == "ns":
        if stage(nv, "ns"):
            st = nv.admin(0x06, cdw10=0, nsid=1, prp1=nv.b.dev_addr(BUF_OFF))
            d = nv.b.read_mem(BUF_OFF, 4096)
            if st == 0:
                nsze, ncap, nuse = struct.unpack_from("<QQQ", d, 0)
                flbas = d[26]
                lbads = d[128 + (flbas & 0x0F) * 4 + 2]
                print(f"  NSZE={nsze} NCAP={ncap} NUSE={nuse} FLBAS=0x{flbas:02x} LBADS=2^{lbads}")
                print(f"  nsid1[0..64]={d[:64].hex()}")
    elif cmd == "loader":
        path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(FW_M2, "loader.fw")
        with open(path, "rb") as f:
            data = f.read()
        print(f"== loader stage {path}: {len(data)} bytes")
        if not stage(nv, "pre"):
            sys.exit(2)
        print("ToRecovery (0xF7) status:", nv.to_recovery())
        time.sleep(1)
        print(" ", nv.regs())
        ok = nv.fw_download(data, timeout=30.0, gap=0.2)
        print("download ok:", ok)
        if ok:
            if data[:4] == b"CMFG":
                conf, info = cmfg_conf_page(data)
                print(f"CMFG descriptors={info} conf={'yes' if conf else 'no'}")
                if conf:
                    st = nv.set_fwconfig(conf)
                    print("set_fwconfig status:", st)
            print("commit 0x19 status:", nv.fw_commit(0x19, timeout=180))
            time.sleep(8)
            stage(nv, "post")
            nv.identify_controller()
    elif cmd == "flash_no_reinit":
        path = sys.argv[2]
        if not stage(nv, "pre"):
            sys.exit(2)
        flash_file(nv, path)
    elif cmd == "vendor":
        if not stage(nv, "vendor"):
            sys.exit(2)
        sub = sys.argv[2] if len(sys.argv) > 2 else "brandnew"
        if sub == "brandnew":
            nv.admin(0xE4, cdw12=1, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO)
        elif sub == "smartclean":
            nv.admin(0xD0, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO)
        elif sub == "removecheck":
            nv.admin(0xE4, cdw12=5, cdw14=MAGIC_GRIT, cdw15=MAGIC_INNO)
        elif sub == "setthin":
            blocks = int(sys.argv[3]) if len(sys.argv) > 3 else (2 * 1024**4) // 512
            nv.admin(0xD1, cdw12=blocks & 0xFFFFFFFF, cdw13=blocks >> 32, nsid=1)
    else:
        print("unknown command")
        sys.exit(1)


if __name__ == "__main__":
    main()
