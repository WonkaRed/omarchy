import os
import re
import subprocess
import sys
import time
from unittest import mock

# This test lives one level above extensions/ so it stays outside the
# default/nautilus-python/extensions/*.py skel-install glob (see
# docs/file-layout.md); point sys.path at extensions/ to import the module.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "extensions"))

import driveinfo


def lsblk_field(field, device):
    result = subprocess.run(
        ["lsblk", "-no", field, "--nodeps", device],
        capture_output=True, text=True, timeout=2,
    )
    return result.stdout.strip()


def check_device(device):
    print(f"--- {device} ---")
    disk, tran = driveinfo.resolve_disk_and_tran(device)
    print(f"disk={disk!r} tran={tran!r}")
    expected_tran = lsblk_field("TRAN", device) or lsblk_field(
        "TRAN", f"/dev/{lsblk_field('PKNAME', device)}"
    )
    assert tran == expected_tran, f"tran mismatch: got {tran!r} expected {expected_tran!r}"

    label = driveinfo.connection_label(disk, tran)
    print(f"connection_label={label!r}")
    assert label, "expected a non-empty connection label"

    try:
        prev = driveinfo.read_disk_stat(disk)
        time.sleep(1)
        cur = driveinfo.read_disk_stat(disk)
        print(f"throughput_label={driveinfo.throughput_label(prev, cur, 1.0)!r}")
    except (OSError, IndexError, ValueError) as error:
        print(f"read_disk_stat failed (unexpected for a real disk): {error}")
        raise


def _usb_address_for(real_path):
    """Mirror of the address-extraction loop in driveinfo.usb_speed_label,
    used to assert on the intermediate result instead of just the final
    (network-dependent) speed label."""
    address = None
    for part in real_path.split("/"):
        if re.match(r"^[0-9]+-[0-9.]+$", part):
            address = part
    return address


def check_usb_direct_attach_address(device, expected_prefix):
    print(f"--- usb address extraction, direct-attach ({device}) ---")
    disk, tran = driveinfo.resolve_disk_and_tran(device)
    if tran != "usb":
        print(f"skipping: {device} is not USB-attached (tran={tran!r})")
        return
    real = os.path.realpath(f"/sys/class/block/{disk}")
    address = _usb_address_for(real)
    print(f"disk={disk!r} real={real!r} address={address!r}")
    assert address and address.startswith(expected_prefix), (
        f"expected a root-port address like {expected_prefix!r}, got {address!r}"
    )
    label = driveinfo.usb_speed_label(disk)
    print(f"usb_speed_label={label!r}")
    assert label and label != "USB", f"expected a real speed label for {disk}, got {label!r}"


def check_usb_hub_address_parsing():
    print("--- usb_speed_label hub-path parsing (simulated, no real hub needed) ---")
    # 2-1 is the hub plugged into the root port; 2-1.4 is the actual device
    # plugged into port 4 of that hub. The regression this guards against:
    # matching the FIRST usbN/ segment (the hub, "2-1") instead of the LAST
    # USB-address segment in the path (the real device, "2-1.4").
    fake_real = (
        "/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1.4/2-1.4:1.0/"
        "host5/target5:0:0/5:0:0:0/block/sdx"
    )
    address = _usb_address_for(fake_real)
    print(f"parsed address={address!r}")
    assert address == "2-1.4", (
        f"hub misattribution regression: expected downstream device '2-1.4', got {address!r} "
        "(if this is '2-1', the code is reporting the HUB's speed as the drive's)"
    )

    opened_paths = []

    def fake_open(path, *args, **kwargs):
        opened_paths.append(path)
        raise FileNotFoundError(path)

    with mock.patch("driveinfo.os.path.realpath", return_value=fake_real), \
         mock.patch("builtins.open", side_effect=fake_open):
        label = driveinfo.usb_speed_label("sdx")

    assert opened_paths == ["/sys/bus/usb/devices/2-1.4/speed"], (
        f"usb_speed_label probed the wrong sysfs node: {opened_paths!r}"
    )
    assert label == "USB", f"expected graceful 'USB' fallback (no such fake device), got {label!r}"
    print("usb_speed_label probed '2-1.4' (the downstream device), not '2-1' (the hub): OK")


def check_weakref_on_liststore():
    print("--- weakref.ref() on Gio.ListStore (empirical PyGObject check) ---")
    import gc
    import weakref
    from gi.repository import Gio, Nautilus as Nautilus_

    store = Gio.ListStore.new(Nautilus_.PropertiesItem)
    ref = weakref.ref(store)
    assert ref() is store, "weakref should resolve to the live store"
    del store
    gc.collect()
    assert ref() is None, (
        "weakref.ref() did not clear after the only strong ref was dropped; "
        "PyGObject may not support this cleanly in this environment"
    )
    print("weakref.ref(Gio.ListStore) resolves to None after the strong ref is dropped: OK")


def check_regular_file(path):
    print(f"--- {path} (expect graceful no-op) ---")
    disk, tran = driveinfo.resolve_disk_and_tran(path)
    print(f"resolve_disk_and_tran on a non-block path: disk={disk!r} tran={tran!r}")

    provider = driveinfo.DriveInfoModelProvider()

    class FakeLocation:
        def get_uri(self):
            return f"file://{path}"

    class FakeFile:
        def get_location(self):
            return FakeLocation()

    models = provider.get_models([FakeFile()])
    print(f"get_models(regular file)={models!r}")
    assert models == [], "expected an empty model list for a non-mount file"


def check_never_raises():
    provider = driveinfo.DriveInfoModelProvider()
    assert provider.get_models([]) == []
    assert provider.get_models([object(), object()]) == []

    class ExplodingFile:
        def get_location(self):
            raise RuntimeError("simulated failure")

    result = provider.get_models([ExplodingFile()])
    assert result == [], f"expected [] even on internal failure, got {result!r}"
    print("get_models never raises: OK (empty-file-list, multi-file, and internal-exception cases)")


if __name__ == "__main__":
    for dev in ("/dev/sdb", "/dev/sdd", "/dev/nvme0n1", "/dev/sda"):
        check_device(dev)

    check_usb_direct_attach_address("/dev/sdb", "1-")
    check_usb_direct_attach_address("/dev/sdd", "1-")
    check_usb_hub_address_parsing()
    check_weakref_on_liststore()
    check_regular_file("/etc/hostname")
    check_never_raises()
    print("\nAll checks passed.")
