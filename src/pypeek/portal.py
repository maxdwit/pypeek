"""Minimal xdg-desktop-portal ScreenCast client (Wayland).

Unlike pipewire-capture's PortalCapture it can
  * remember the user's choice (persist_mode + restore_token), so the
    "Bildschirmübertragung" dialog only appears the first time,
  * ask for the mouse cursor to be embedded in the frames.
The resulting fd/node_id are handed to pipewire_capture.CaptureStream.
"""
import os, time, itertools
from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import Proxy, open_dbus_connection

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
_counter = itertools.count(1)


class PortalError(Exception):
    pass


class ScreenCastSession:
    def __init__(self, token_file, source_types=1, cursor_mode=2):
        self.token_file = token_file
        self.source_types = source_types  # 1 monitor, 2 window, 3 both
        self.cursor_mode = cursor_mode    # 1 hidden, 2 embedded, 4 metadata
        self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)
        self.sender = self.conn.unique_name[1:].replace(".", "_")
        self.session_handle = None
        self.fd = -1
        self.node_id = None
        self.streams = []
        self.width = 0
        self.height = 0
        self._tag = f"pypeek{os.getpid()}"

    def _addr(self, interface="org.freedesktop.portal.ScreenCast", path=PATH):
        return DBusAddress(path, bus_name=BUS, interface=interface)

    def _token(self):
        return f"{self._tag}_{next(_counter)}"

    def _request(self, method, signature, make_body, timeout=120):
        """Call a portal method that answers through a Request/Response signal."""
        token = self._token()
        request_path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=request_path)
        Proxy(message_bus, self.conn).AddMatch(rule)
        with self.conn.filter(rule) as queue:
            msg = new_method_call(self._addr(), method, signature, make_body(token))
            self.conn.send_and_get_reply(msg, timeout=10)
            reply = self.conn.recv_until_filtered(queue, timeout=timeout)
        code, results = reply.body
        if code != 0:
            raise PortalError(f"{method} was cancelled or denied (code {code})")
        return results

    def _load_token(self):
        try:
            with open(self.token_file) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def open(self):
        results = self._request("CreateSession", "a{sv}", lambda t: ({
            "handle_token": ("s", t), "session_handle_token": ("s", self._token())},))
        self.session_handle = results["session_handle"][1]

        opts = lambda t: (self.session_handle, {
            "handle_token": ("s", t), "types": ("u", self.source_types),
            "multiple": ("b", False), "cursor_mode": ("u", self.cursor_mode),
            "persist_mode": ("u", 2)} | ({"restore_token": ("s", tok)} if (tok := self._load_token()) else {}))
        self._request("SelectSources", "oa{sv}", opts)

        results = self._request("Start", "osa{sv}", lambda t: (self.session_handle, "", {"handle_token": ("s", t)}))
        streams = results["streams"][1]
        if not streams:
            raise PortalError("portal returned no streams")
        # every approved screen: (node_id, width, height, x, y)
        self.streams = []
        for node, props in streams:
            w, h = props["size"][1] if "size" in props else (0, 0)
            x, y = props["position"][1] if "position" in props else (None, None)
            self.streams.append((node, w, h, x, y))
        self.node_id, self.width, self.height = self.streams[0][:3]
        token = results.get("restore_token")
        if token:
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            with open(self.token_file, "w") as f:
                f.write(token[1])

        msg = new_method_call(self._addr(), "OpenPipeWireRemote", "oa{sv}", (self.session_handle, {}))
        reply = self.conn.send_and_get_reply(msg, timeout=10)
        self.fd = reply.body[0].to_raw_fd()
        return self

    @property
    def is_open(self):
        return self.session_handle is not None and self.fd >= 0

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self.session_handle:
            try:
                msg = new_method_call(self._addr("org.freedesktop.portal.Session", self.session_handle), "Close")
                self.conn.send_and_get_reply(msg, timeout=3)
            except Exception:
                pass
            self.session_handle = None
        try:
            self.conn.close()
        except Exception:
            pass
