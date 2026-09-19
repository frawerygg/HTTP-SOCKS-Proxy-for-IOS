import sys

from lib.interfaces import get_labeled_interfaces, get_default_selections

IS_PYTHONISTA = "Pythonista" in sys.executable

if IS_PYTHONISTA:
    import ui
    import dialogs
    import console
    from objc_util import on_main_thread, ObjCInstance


def _make_label(text, frame, font_size=14, alignment=None, text_color="#ffffff"):
    lbl = ui.Label()
    lbl.text = text
    lbl.frame = frame
    lbl.font = ("<system>", font_size)
    lbl.text_color = text_color
    if alignment is not None:
        lbl.alignment = alignment
    lbl.number_of_lines = 0
    return lbl


def _make_button(title, frame, action):
    btn = ui.Button(type="system")
    btn.title = title
    btn.frame = frame
    btn.action = action
    return btn


class ProxyUIView(ui.View):
    def __init__(
        self,
        stats,
        start_server_cb,
        stop_server_cb,
        socks_port=9876,
        http_port=9877,
        wpad_port=8088,
    ):
        super().__init__()
        self.name = "SOCKS5 Proxy"
        self.background_color = "#000000"
        self.stats = stats
        self._start_server_cb = start_server_cb
        self._stop_server_cb = stop_server_cb
        self._socks_port = socks_port
        self._http_port = http_port
        self._wpad_port = wpad_port
        self._running = False

        # Current selections
        self._proxy_iface = None
        self._connect_iface = None

        # Auto-hide UI
        self._idle_seconds = 0
        self._ui_visible = True

        # Detect interfaces and pick defaults
        self._refresh_and_set_defaults()

        # Keep screen on
        on_main_thread(console.set_idle_timer_disabled)(True)

        self.touch_enabled = True
        self._build_ui()
        self._all_subviews = list(self.subviews)
        self.update_interval = 1.0

    def _refresh_and_set_defaults(self):
        labeled = get_labeled_interfaces()
        proxy_idx, connect_idx = get_default_selections(labeled)
        if labeled:
            if proxy_idx is not None:
                self._proxy_iface = labeled[proxy_idx]
            else:
                self._proxy_iface = labeled[0]
            if connect_idx is not None:
                self._connect_iface = labeled[connect_idx]
            elif len(labeled) > 1:
                self._connect_iface = labeled[1]
            else:
                self._connect_iface = labeled[0]

    def _build_ui(self):
        # --- Config card (proxy + connect) ---
        self._config_card = ui.View()
        self._config_card.background_color = "#1c1c1e"
        self._config_card.corner_radius = 12
        self._config_card.border_width = 0.5
        self._config_card.border_color = "#2c2c2e"
        self.add_subview(self._config_card)

        self._config_header = _make_label(
            "CONFIGURATION", (0, 0, 100, 16), font_size=11, text_color="#8e8e93"
        )
        self._config_card.add_subview(self._config_header)

        self._proxy_label = _make_label("", (0, 0, 100, 28), font_size=15)
        self._update_proxy_label()
        self._config_card.add_subview(self._proxy_label)

        self._proxy_btn = _make_button(
            "Change", (0, 0, 60, 28), self._change_proxy
        )
        self._config_card.add_subview(self._proxy_btn)

        # Divider
        self._config_div = ui.View()
        self._config_div.background_color = "#2c2c2e"
        self._config_card.add_subview(self._config_div)

        self._connect_label = _make_label("", (0, 0, 100, 28), font_size=15)
        self._update_connect_label()
        self._config_card.add_subview(self._connect_label)

        self._connect_btn = _make_button(
            "Change", (0, 0, 60, 28), self._change_connect
        )
        self._config_card.add_subview(self._connect_btn)

        # --- Status label ---
        self._status_label = _make_label(
            "○ Stopped", (0, 0, 100, 22), font_size=14, text_color="#8e8e93"
        )
        self.add_subview(self._status_label)

        # --- Stats card ---
        self._stats_card = ui.View()
        self._stats_card.background_color = "#1c1c1e"
        self._stats_card.corner_radius = 12
        self._stats_card.border_width = 0.5
        self._stats_card.border_color = "#2c2c2e"
        self.add_subview(self._stats_card)

        self._stats_header = _make_label(
            "STATISTICS", (0, 0, 100, 16), font_size=11, text_color="#8e8e93"
        )
        self._stats_card.add_subview(self._stats_header)

        self._stats_text = ui.TextView()
        self._stats_text.editable = False
        self._stats_text.font = ("Menlo", 12)
        self._stats_text.background_color = "#1c1c1e"
        self._stats_text.text_color = "#ffffff"
        self._stats_card.add_subview(self._stats_text)

        # --- Log card ---
        self._log_card = ui.View()
        self._log_card.background_color = "#1c1c1e"
        self._log_card.corner_radius = 12
        self._log_card.border_width = 0.5
        self._log_card.border_color = "#2c2c2e"
        self.add_subview(self._log_card)

        self._log_header = _make_label(
            "LOG", (0, 0, 100, 16), font_size=11, text_color="#8e8e93"
        )
        self._log_card.add_subview(self._log_header)

        self._log_label = _make_label("", (0, 0, 100, 100), font_size=11)
        self._log_card.add_subview(self._log_label)

        # --- Start/Stop button ---
        self._action_btn = _make_button(
            "Start", (0, 0, 100, 48), self._toggle_server
        )
        self._action_btn.background_color = "#007AFF"
        self._action_btn.tint_color = "white"
        self._action_btn.corner_radius = 12
        self._action_btn.font = ("<system-bold>", 17)
        self.add_subview(self._action_btn)

        # --- Exit button ---
        self._exit_btn = _make_button("Exit", (0, 0, 100, 44), self._exit_app)
        self._exit_btn.background_color = "#1c1c1e"
        self._exit_btn.tint_color = "#ff3b30"
        self._exit_btn.corner_radius = 12
        self.add_subview(self._exit_btn)

    def _safe_area_top(self):
        """Get the top safe area inset for status bar + Dynamic Island."""
        try:
            # Try runtime detection first
            insets = ObjCInstance(self).safeAreaInsets()
            if insets.top > 0:
                return insets.top + 8
        except Exception:
            pass
        # Fallback: generous padding for Dynamic Island
        return 60

    def layout(self):
        w = self.width
        pad = 12
        y = self._safe_area_top() + 12

        # --- Config card ---
        card_w = w - 2 * pad
        inner_pad = 14
        config_h = 130
        self._config_card.frame = (pad, y, card_w, config_h)

        iy = inner_pad
        self._config_header.frame = (inner_pad, iy, card_w - 2 * inner_pad, 16)
        iy += 20
        self._proxy_label.frame = (inner_pad, iy, card_w - 2 * inner_pad - 66, 28)
        self._proxy_btn.frame = (card_w - inner_pad - 60, iy, 60, 28)
        iy += 32
        self._config_div.frame = (inner_pad, iy, card_w - 2 * inner_pad, 0.5)
        iy += 8
        self._connect_label.frame = (inner_pad, iy, card_w - 2 * inner_pad - 66, 28)
        self._connect_btn.frame = (card_w - inner_pad - 60, iy, 60, 28)

        y += config_h + 12

        # --- Status label ---
        self._status_label.frame = (pad + 4, y, card_w - 8, 22)
        y += 28

        # --- Stats card ---
        stats_h = 230
        self._stats_card.frame = (pad, y, card_w, stats_h)
        self._stats_header.frame = (inner_pad, inner_pad, card_w - 2 * inner_pad, 16)
        self._stats_text.frame = (
            inner_pad,
            inner_pad + 22,
            card_w - 2 * inner_pad,
            stats_h - inner_pad - 28,
        )

        y += stats_h + 12

        # --- Log card ---
        log_h = 110
        self._log_card.frame = (pad, y, card_w, log_h)
        self._log_header.frame = (inner_pad, inner_pad, card_w - 2 * inner_pad, 16)
        self._log_label.frame = (
            inner_pad,
            inner_pad + 22,
            card_w - 2 * inner_pad,
            log_h - inner_pad - 28,
        )

        y += log_h + 16

        # --- Buttons ---
        self._action_btn.frame = (pad, y, card_w, 48)
        y += 56
        self._exit_btn.frame = (pad, y, card_w, 44)

    def touch_began(self, touch):
        """Reset idle timer and show UI on any touch."""
        self._idle_seconds = 0
        if not self._ui_visible:
            self._show_ui()

    def _hide_ui(self):
        """Hide all subviews — pure black screen. Tap anywhere to wake."""
        for v in self.subviews:
            v.hidden = True
        self._ui_visible = False

    def _show_ui(self):
        """Show all UI elements again."""
        for v in self.subviews:
            v.hidden = False
        self._ui_visible = True

    def _update_proxy_label(self):
        display = self._proxy_iface[0] if self._proxy_iface else "None"
        self._proxy_label.text = f"Proxy: {display}"

    def _update_connect_label(self):
        display = self._connect_iface[0] if self._connect_iface else "None"
        self._connect_label.text = f"Connect: {display}"

    def _change_proxy(self, sender):
        labeled = get_labeled_interfaces()
        items = [d for d, _ in labeled]
        choice = dialogs.list_dialog("Proxy Access Interface", items)
        if choice is not None:
            idx = items.index(choice)
            self._proxy_iface = labeled[idx]
            self._update_proxy_label()
            if self._running:
                self._restart_server()

    def _change_connect(self, sender):
        labeled = get_labeled_interfaces()
        items = [d for d, _ in labeled]
        choice = dialogs.list_dialog("Internet Connection Interface", items)
        if choice is not None:
            idx = items.index(choice)
            self._connect_iface = labeled[idx]
            self._update_connect_label()
            if self._running:
                self._restart_server()

    def _toggle_server(self, sender):
        if self._running:
            self._stop_server_cb()
            self._running = False
            self._status_label.text = "○ Stopped"
            self._status_label.text_color = "#8e8e93"
            self._action_btn.title = "Start"
        else:
            if self._proxy_iface is None or self._connect_iface is None:
                dialogs.alert("Error", "Select both interfaces first.")
                return
            proxy_addr = self._proxy_iface[1].addr.address
            connect_addr = self._connect_iface[1].addr.address
            connect_name = self._connect_iface[1].name
            try:
                self._start_server_cb(proxy_addr, connect_addr, connect_name)
            except Exception as exc:
                dialogs.alert("Proxy failed to start", str(exc))
                self._running = False
                self._status_label.text = "● Error: %s" % exc
                self._status_label.text_color = "#ff3b30"
                self._action_btn.title = "Start"
                return
            self._running = True
            self._status_label.text = "● Running"
            self._status_label.text_color = "#30d158"
            self._action_btn.title = "Stop"

    def _restart_server(self):
        self._stop_server_cb()
        proxy_addr = self._proxy_iface[1].addr.address
        connect_addr = self._connect_iface[1].addr.address
        connect_name = self._connect_iface[1].name
        try:
            self._start_server_cb(proxy_addr, connect_addr, connect_name)
        except Exception as exc:
            self._running = False
            self._status_label.text = "● Error: %s" % exc
            self._status_label.text_color = "#ff3b30"
            self._action_btn.title = "Start"
            dialogs.alert("Proxy failed to restart", str(exc))
            return
        self._status_label.text = "● Running"
        self._status_label.text_color = "#30d158"

    def _exit_app(self, sender):
        """Stop the server and close the Pythonista view."""
        if self._running:
            self._stop_server_cb()
            self._running = False
        self.close()

    def update(self):
        """Called by Pythonista UI at update_interval frequency."""
        # Auto-hide: count idle seconds
        if self._running and self._ui_visible:
            self._idle_seconds += 1
            if self._idle_seconds >= 30:
                self._hide_ui()

        if not self._running:
            return

        snap = self.stats.get_snapshot()
        megabit = 1024 * 1024 / 8
        megabyte = 1024 * 1024

        proxy_addr = self._proxy_iface[1].addr.address if self._proxy_iface else "?"
        lines = [
            f"In:    {snap['inbound_speed'] / megabit:.2f} Mbps",
            f"Out:   {snap['outbound_speed'] / megabit:.2f} Mbps",
            f"",
            f"Connections: {snap['connections']}",
            f"Total In:    {snap['inbound_total'] / megabyte:.2f} MB",
            f"Total Out:   {snap['outbound_total'] / megabyte:.2f} MB",
            f"Total:       {(snap['inbound_total'] + snap['outbound_total']) / megabyte:.2f} MB",
            f"",
            f"Listening: all IPv4 interfaces",
            f"PAC URL: http://{proxy_addr}:{self._wpad_port}/wpad.dat",
            f"SOCKS:   {proxy_addr}:{self._socks_port}",
            f"HTTP:    {proxy_addr}:{self._http_port}",
        ]
        self._stats_text.text = "\n".join(lines)

        if snap["messages"]:
            self._log_label.text = "\n".join(snap["messages"][-5:])
        if snap["errors"]:
            self._log_label.text += f"\nErrors: {snap['errors']}"
