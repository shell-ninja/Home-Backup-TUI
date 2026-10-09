"""portal.py - Native XDG Desktop Portal directory picker for Linux/Wayland/X11."""

import os
import sys
import uuid
import urllib.parse
from pathlib import Path
from typing import Optional


def pick_directory_portal(
    title: str = "Select Backup Directory",
    current_folder: Optional[str] = None,
    timeout_sec: float = 120.0,
) -> Optional[str]:
    """Open native XDG Desktop Portal directory chooser dialog.
    
    Returns the selected folder path, or None if cancelled or unavailable.
    """
    try:
        from gi.repository import Gio, GLib  # type: ignore
    except ImportError:
        return None

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:
        return None

    loop = GLib.MainLoop()
    result_path: Optional[str] = None

    token = f"hb_{uuid.uuid4().hex[:8]}"
    sender = bus.get_unique_name()[1:].replace(".", "_")
    handle_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def on_response(connection, sender_name, object_path, interface_name, signal_name, parameters, user_data):
        nonlocal result_path
        try:
            code, results = parameters.unpack()
            if code == 0 and "uris" in results:
                uris = results["uris"]
                if uris:
                    uri = uris[0]
                    if uri.startswith("file://"):
                        result_path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
                    else:
                        result_path = uri
        except Exception:
            pass
        finally:
            loop.quit()

    try:
        sub_id = bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            handle_path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
    except Exception:
        return None

    options = {
        "handle_token": GLib.Variant("s", token),
        "directory": GLib.Variant("b", True),
        "modal": GLib.Variant("b", True),
    }

    if current_folder and os.path.exists(current_folder):
        folder_bytes = os.path.abspath(current_folder).encode("utf-8") + b"\0"
        options["current_folder"] = GLib.Variant("ay", [b for b in folder_bytes])

    timer_id = None
    if timeout_sec > 0:
        timer_id = GLib.timeout_add_seconds(int(timeout_sec), lambda: loop.quit())

    try:
        bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.FileChooser",
            "OpenFile",
            GLib.Variant("(ssa{sv})", ("", title, options)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        loop.run()
    except Exception as e:
        sys.stderr.write(f"Portal call failed: {e}\n")
    finally:
        try:
            if timer_id:
                GLib.source_remove(timer_id)
            bus.signal_unsubscribe(sub_id)
        except Exception:
            pass

    return result_path


def is_portal_available() -> bool:
    """Check if org.freedesktop.portal.Desktop is responsive on session bus."""
    try:
        from gi.repository import Gio  # type: ignore
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.DBus.Peer",
            None,
        )
        proxy.Ping()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        sys.exit(0 if is_portal_available() else 1)
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    res = pick_directory_portal(current_folder=folder)
    if res:
        print(res)
