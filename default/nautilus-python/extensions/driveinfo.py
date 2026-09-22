import os
import re
import shlex
import subprocess
import sys
import time
import weakref

from gi import require_version

require_version("Nautilus", "4.1")

from gi.repository import GObject, Gio, GLib, Nautilus


LSBLK_TIMEOUT = 2
MAX_THROUGHPUT_TICKS = 600
SECTOR_BYTES = 512

USB_SPEED_LABELS = {
    "1.5": "USB 1.0 (Low Speed)",
    "12": "USB 1.1 (Full Speed)",
    "480": "USB 2.0 (Hi-Speed)",
    "5000": "USB 3.0 (SuperSpeed, 5 Gbit/s)",
    "10000": "USB 3.1 Gen 2 (SuperSpeed+, 10 Gbit/s)",
    "20000": "USB 3.2 Gen 2x2 (20 Gbit/s)",
}

SATA_SPEED_LABELS = {
    "1.5 Gbps": "SATA I (1.5 Gbps)",
    "3.0 Gbps": "SATA II (3.0 Gbps)",
    "6.0 Gbps": "SATA III (6.0 Gbps)",
}


def _lsblk_pairs(args):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=LSBLK_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    pairs = {}
    for token in shlex.split(result.stdout):
        key, _, value = token.partition("=")
        pairs[key] = value
    return pairs


def resolve_disk_and_tran(device):
    # --nodeps is load-bearing: without it, lsblk walks the device's whole
    # dependency tree (partitions, dm holders) and prints one line per node,
    # which breaks single-value parsing for whole-disk targets.
    pairs = _lsblk_pairs(["lsblk", "-no", "PKNAME,TRAN", "--nodeps", "-P", device])
    if pairs is None:
        return None, None
    pkname = pairs.get("PKNAME", "")
    tran = pairs.get("TRAN", "")
    if pkname:
        disk = pkname
        if not tran:
            disk_pairs = _lsblk_pairs(
                ["lsblk", "-no", "TRAN", "--nodeps", "-P", f"/dev/{pkname}"]
            )
            tran = (disk_pairs or {}).get("TRAN", "")
    else:
        disk = os.path.basename(device)
    return disk, tran


def usb_speed_label(disk):
    real = os.path.realpath(f"/sys/class/block/{disk}")
    # The device's own sysfs node is a USB address segment (e.g. "1-1",
    # or "2-1.4" behind a hub). Each hub hop adds one more ".N" and nests
    # one level deeper in the path, so the LAST such segment is the actual
    # device; the first would be the hub it's plugged into. Segments with a
    # ":<config>.<interface>" suffix (e.g. "2-1.4:1.0") are interface nodes,
    # not device addresses, and are excluded by the exact-match anchor.
    address = None
    for part in real.split("/"):
        if re.match(r"^[0-9]+-[0-9.]+$", part):
            address = part
    if not address:
        return "USB"
    try:
        with open(f"/sys/bus/usb/devices/{address}/speed") as handle:
            speed = handle.read().strip()
    except (FileNotFoundError, OSError):
        return "USB"
    label = USB_SPEED_LABELS.get(speed)
    if label:
        return label
    return f"USB ({speed} Mbit/s)"


def nvme_speed_label(disk):
    match = re.match(r"(nvme[0-9]+)", disk)
    if not match:
        return "NVMe"
    controller = match.group(1)
    try:
        with open(f"/sys/class/nvme/{controller}/device/current_link_speed") as handle:
            speed = handle.read().strip()
        with open(f"/sys/class/nvme/{controller}/device/current_link_width") as handle:
            width = handle.read().strip()
    except (FileNotFoundError, OSError):
        return "NVMe"
    return f"NVMe (PCIe {speed.replace(' PCIe', '')}, x{width})"


def sata_speed_label(disk):
    real = os.path.realpath(f"/sys/class/block/{disk}/device")
    match = re.search(r"/ata([0-9]+)/", real)
    if not match:
        return "SATA"
    try:
        with open(f"/sys/class/ata_link/link{match.group(1)}/sata_spd") as handle:
            speed = handle.read().strip()
    except (FileNotFoundError, OSError):
        return "SATA"
    if speed == "<unknown>":
        return "SATA"
    return SATA_SPEED_LABELS.get(speed, f"SATA ({speed})")


def connection_label(disk, tran):
    if tran == "usb":
        return usb_speed_label(disk)
    if tran == "nvme":
        return nvme_speed_label(disk)
    if tran == "sata":
        return sata_speed_label(disk)
    if tran:
        return tran.capitalize()
    return None


def read_disk_stat(disk):
    with open(f"/sys/class/block/{disk}/stat") as handle:
        fields = handle.read().split()
    return int(fields[2]), int(fields[6])


def throughput_label(prev, cur, elapsed):
    if elapsed <= 0:
        elapsed = 1.0
    read_mbps = (cur[0] - prev[0]) * SECTOR_BYTES / 1_000_000 / elapsed
    write_mbps = (cur[1] - prev[1]) * SECTOR_BYTES / 1_000_000 / elapsed
    return f"R: {read_mbps:.1f} MB/s  W: {write_mbps:.1f} MB/s"


class DriveInfoModelProvider(GObject.GObject, Nautilus.PropertiesModelProvider):
    def get_models(self, files):
        try:
            return self._get_models(files)
        except Exception as error:
            print(f"driveinfo: {error}", file=sys.stderr)
            return []

    def _get_models(self, files):
        if len(files) != 1:
            return []

        location = files[0].get_location()
        if not location:
            return []

        mount = self._find_mount(location.get_uri())
        if not mount:
            return []

        volume = mount.get_volume()
        if not volume:
            return []

        device = volume.get_identifier(Gio.VOLUME_IDENTIFIER_KIND_UNIX_DEVICE)
        if not device:
            return []

        disk, tran = resolve_disk_and_tran(device)
        if not disk:
            return []

        store = Gio.ListStore.new(Nautilus.PropertiesItem)

        label = connection_label(disk, tran)
        if label:
            store.append(Nautilus.PropertiesItem.new(name="Connection", value=label))

        self._add_throughput(store, disk)

        if store.get_n_items() == 0:
            return []

        return [Nautilus.PropertiesModel.new(title="Drive", model=store)]

    def _find_mount(self, uri):
        # Gio.VolumeMonitor has no get_default(); the singleton accessor
        # bound here is .get(), wrapping g_volume_monitor_get().
        for mount in Gio.VolumeMonitor.get().get_mounts():
            root = mount.get_root()
            if root and root.get_uri() == uri:
                return mount
        return None

    def _add_throughput(self, store, disk):
        try:
            prev = read_disk_stat(disk)
        except (OSError, IndexError, ValueError):
            return

        index = store.get_n_items()
        store.append(
            Nautilus.PropertiesItem.new(name="Throughput", value="Measuring…")
        )
        state = {"prev": prev, "time": time.monotonic(), "ticks": 0}
        store_ref = weakref.ref(store)

        def tick():
            store = store_ref()
            if store is None:
                # Nautilus dropped its last reference to the store (the
                # popover was closed); this closure was the only thing left
                # keeping the timer alive, so stop it now instead of ticking
                # for the full MAX_THROUGHPUT_TICKS with no listener.
                return GLib.SOURCE_REMOVE
            try:
                cur = read_disk_stat(disk)
                now = time.monotonic()
                value = throughput_label(state["prev"], cur, now - state["time"])
                # PropertiesItem.value is construct-only (verified: set_property
                # raises TypeError); splice() replaces the row and fires
                # items-changed, which is how a live GListModel row updates.
                store.splice(
                    index, 1, [Nautilus.PropertiesItem.new(name="Throughput", value=value)]
                )
                state["prev"] = cur
                state["time"] = now
                state["ticks"] += 1
            except Exception as error:
                print(f"driveinfo: {error}", file=sys.stderr)
                return GLib.SOURCE_REMOVE
            if state["ticks"] >= MAX_THROUGHPUT_TICKS:
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add_seconds(1, tick)
