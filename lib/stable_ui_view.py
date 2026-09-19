"""Pythonista UI for Stable Mode. Legacy UI remains in ``ui_view.py``."""

import threading

import console
import dialogs
import ui
from objc_util import ObjCInstance, on_main_thread

from .interface_manager import AUTO


def _label(text="", size=14, color="#ffffff"):
    view = ui.Label()
    view.text = text
    view.font = ("<system>", size)
    view.text_color = color
    view.number_of_lines = 0
    return view


def _button(title, action, color="#007aff"):
    view = ui.Button(type="system")
    view.title = title
    view.action = action
    view.background_color = color
    view.tint_color = "white"
    view.corner_radius = 10
    view.font = ("<system-bold>", 15)
    return view


class StableProxyUIView(ui.View):
    def __init__(self, manager, stats):
        super().__init__()
        self.name = "SOCKS5 Proxy — Stable Mode"
        self.background_color = "#000000"
        self.manager = manager
        self.stats = stats
        self.proxy_selection = manager.proxy_selection or AUTO
        self.outbound_selection = manager.outbound_selection or AUTO
        self._busy = False
        self._refreshing = True
        self.scroll = ui.ScrollView()
        self.scroll.always_bounce_vertical = True
        self.add_subview(self.scroll)
        self._build()
        self.refresh_button.enabled = False
        self.refresh_button.title = "Refreshing…"
        on_main_thread(console.set_idle_timer_disabled)(True)
        self.update_interval = 1.0
        self._run_background(self.manager.refresh_interfaces, self._interfaces_refreshed)

    def _card(self):
        view = ui.View()
        view.background_color = "#1c1c1e"
        view.corner_radius = 12
        view.border_width = 0.5
        view.border_color = "#38383a"
        self.scroll.add_subview(view)
        return view

    def _build(self):
        self.config_card = self._card()
        self.config_header = _label("STABLE MODE • INTERFACES", 11, "#8e8e93")
        self.config_card.add_subview(self.config_header)
        self.proxy_label = _label("Inbound / endpoint hint: Auto", 14)
        self.config_card.add_subview(self.proxy_label)
        self.proxy_button = _button("Change", self._choose_proxy, "#2c2c2e")
        self.config_card.add_subview(self.proxy_button)
        self.outbound_label = _label("Internet / Outbound: Auto", 14)
        self.config_card.add_subview(self.outbound_label)
        self.outbound_button = _button("Change", self._choose_outbound, "#2c2c2e")
        self.config_card.add_subview(self.outbound_button)
        self.refresh_button = _button("Refresh Interfaces", self._refresh, "#2c2c2e")
        self.config_card.add_subview(self.refresh_button)

        self.status_label = _label("○ Stopped", 15, "#8e8e93")
        self.scroll.add_subview(self.status_label)

        self.stats_card = self._card()
        self.stats_header = _label("TRAFFIC • CURRENT RUN", 11, "#8e8e93")
        self.stats_card.add_subview(self.stats_header)
        self.stats_text = ui.TextView()
        self.stats_text.editable = False
        self.stats_text.font = ("Menlo", 12)
        self.stats_text.background_color = "#1c1c1e"
        self.stats_text.text_color = "#ffffff"
        self.stats_card.add_subview(self.stats_text)

        self.health_card = self._card()
        self.health_header = _label("HEALTH", 11, "#8e8e93")
        self.health_card.add_subview(self.health_header)
        self.health_text = ui.TextView()
        self.health_text.editable = False
        self.health_text.font = ("Menlo", 11)
        self.health_text.background_color = "#1c1c1e"
        self.health_text.text_color = "#ffffff"
        self.health_card.add_subview(self.health_text)

        self.log_card = self._card()
        self.log_header = _label("RECENT EVENTS", 11, "#8e8e93")
        self.log_card.add_subview(self.log_header)
        self.log_text = ui.TextView()
        self.log_text.editable = False
        self.log_text.font = ("Menlo", 10)
        self.log_text.background_color = "#1c1c1e"
        self.log_text.text_color = "#ffffff"
        self.log_card.add_subview(self.log_text)

        self.start_button = _button("Start", self._toggle)
        self.scroll.add_subview(self.start_button)
        self.recover_button = _button("Recover", self._recover, "#ff9500")
        self.scroll.add_subview(self.recover_button)
        self.exit_button = _button("Exit", self._exit, "#2c2c2e")
        self.exit_button.tint_color = "#ff453a"
        self.scroll.add_subview(self.exit_button)

    def _safe_top(self):
        try:
            inset = ObjCInstance(self).safeAreaInsets().top
            if inset:
                return inset + 8
        except Exception:
            pass
        return 52

    def layout(self):
        self.scroll.frame = self.bounds
        width = min(700, self.width - 24)
        pad = (self.width - width) / 2
        y = self._safe_top()
        self.config_card.frame = (pad, y, width, 178)
        self.config_header.frame = (14, 10, width - 28, 18)
        self.proxy_label.frame = (14, 33, width - 104, 44)
        self.proxy_button.frame = (width - 88, 33, 74, 44)
        self.outbound_label.frame = (14, 78, width - 104, 44)
        self.outbound_button.frame = (width - 88, 78, 74, 44)
        self.refresh_button.frame = (14, 128, width - 28, 44)
        y += 188
        self.status_label.frame = (pad + 4, y, width - 8, 24)
        y += 30
        stats_h = 112
        self.stats_card.frame = (pad, y, width, stats_h)
        self.stats_header.frame = (14, 10, width - 28, 18)
        self.stats_text.frame = (10, 30, width - 20, stats_h - 38)
        y += stats_h + 10
        health_h = 210
        self.health_card.frame = (pad, y, width, health_h)
        self.health_header.frame = (14, 10, width - 28, 18)
        self.health_text.frame = (10, 30, width - 20, health_h - 38)
        y += health_h + 10
        log_h = 150
        self.log_card.frame = (pad, y, width, log_h)
        self.log_header.frame = (14, 10, width - 28, 18)
        self.log_text.frame = (10, 30, width - 20, log_h - 38)
        y += log_h + 10
        gap = 8
        third = (width - gap * 2) / 3
        self.start_button.frame = (pad, y, third, 48)
        self.recover_button.frame = (pad + third + gap, y, third, 48)
        self.exit_button.frame = (pad + (third + gap) * 2, y, third, 48)
        self.scroll.content_size = (self.width, y + 72)

    def _choose(self, title, current):
        options = self.manager.interface_options(current)
        labels = [label for label, value in options]
        choice = dialogs.list_dialog(title, labels)
        if choice is None:
            return None
        return options[labels.index(choice)][1]

    def _choose_proxy(self, sender):
        selection = self._choose("Inbound / endpoint hint", self.proxy_selection)
        if selection is None:
            return
        self.manager.set_proxy_selection(selection)
        self.proxy_selection = self.manager.proxy_selection
        self._update_selection_labels()
        # Stable Mode listens on 0.0.0.0.  This selection is advisory metadata
        # and must not restart healthy listeners merely because an enX entry
        # appears or disappears from getifaddrs().

    def _choose_outbound(self, sender):
        selection = self._choose(
            "Internet / Outbound Interface", self.outbound_selection
        )
        if selection is None:
            return
        # The manager owns runtime selection state.  It recovers only when a
        # meaningful outbound change actually occurred.
        self.manager.set_outbound_selection(selection, recover_if_running=True)
        self.outbound_selection = self.manager.outbound_selection
        self._update_selection_labels()

    def _update_selection_labels(self):
        self.proxy_label.text = "Inbound / endpoint hint: %s" % self.proxy_selection
        self.outbound_label.text = "Internet / Outbound: %s" % self.outbound_selection

    def _refresh(self, sender):
        if self._busy or self._refreshing:
            return
        self._refreshing = True
        self.refresh_button.enabled = False
        self.refresh_button.title = "Refreshing…"
        self._run_background(self.manager.refresh_interfaces, self._interfaces_refreshed)

    def _interfaces_refreshed(self, result, error):
        self._refreshing = False
        self.refresh_button.enabled = not self._busy
        self.refresh_button.title = "Refresh Interfaces"
        if error:
            dialogs.alert("Refresh failed", str(error))
        else:
            self.proxy_selection = self.manager.proxy_selection
            self.outbound_selection = self.manager.outbound_selection
            self._update_selection_labels()

    def _toggle(self, sender):
        if self._busy:
            return
        if self.manager.running:
            self._set_busy(True, "Stopping…")
            self._run_background(self.manager.stop, self._stopped)
        else:
            self._set_busy(True, "Starting…")
            self._run_background(
                lambda: self.manager.start(
                    self.proxy_selection, self.outbound_selection
                ),
                self._started,
            )

    def _started(self, result, error):
        self._set_busy(False)
        if error:
            dialogs.alert("Stable Mode could not start", str(error))
        self.update()

    def _stopped(self, result, error):
        self._set_busy(False)
        if error or result is False:
            dialogs.alert("Stop failed", str(error or "Proxy shutdown timed out"))
        self.update()

    def _recover(self, sender):
        if not self.manager.running:
            dialogs.alert("Recover", "Start Stable Mode first.")
            return
        try:
            self.manager.recover("Recover button")
            self.status_label.text = "◐ Recovery requested"
            self.status_label.text_color = "#ff9f0a"
        except Exception as exc:
            dialogs.alert("Recover failed", str(exc))

    def _exit(self, sender):
        if self._busy:
            return
        if self.manager.running:
            self._set_busy(True, "Stopping…")
            self._run_background(self.manager.stop, self._exited)
            return
        self.close()

    def _exited(self, result, error):
        if error or result is False:
            self._set_busy(False)
            dialogs.alert("Exit failed", str(error or "Proxy shutdown timed out"))
            self.update()
            return
        self.close()

    def will_close(self):
        # Revised 5: the Stable UI is a control panel, not the owner of the
        # proxy lifetime. Closing/navigating away from the view must not
        # terminate a running proxy. The explicit Exit button still stops it.
        on_main_thread(console.set_idle_timer_disabled)(False)

    def _set_busy(self, busy, title=None):
        self._busy = busy
        for control in (
            self.start_button,
            self.recover_button,
            self.refresh_button,
            self.proxy_button,
            self.outbound_button,
            self.exit_button,
        ):
            control.enabled = not busy
        if not busy and self._refreshing:
            self.refresh_button.enabled = False
        if title:
            self.start_button.title = title

    def _run_background(self, operation, completion):
        def worker():
            result = None
            error = None
            try:
                result = operation()
            except Exception as exc:
                error = exc
            ui.delay(lambda: completion(result, error), 0.0)

        threading.Thread(target=worker, daemon=True).start()

    def update(self):
        snap = self.stats.get_snapshot()
        runtime = snap["runtime"]
        state = runtime.get("state", "Stopped")
        colors = {
            "Running": "#30d158",
            "Recovering": "#ff9f0a",
            "Starting": "#64d2ff",
            "Paused": "#ff453a",
            "Error": "#ff453a",
            "Stopped": "#8e8e93",
        }
        self.status_label.text = "● %s" % state if state != "Stopped" else "○ Stopped"
        self.status_label.text_color = colors.get(state, "#8e8e93")
        self.proxy_selection = self.manager.proxy_selection
        self.outbound_selection = self.manager.outbound_selection
        self._update_selection_labels()
        if self._busy:
            return
        self.start_button.title = "Stop" if self.manager.running else "Start"

        megabyte = 1024.0 * 1024.0
        inbound_total = snap.get("inbound_total", 0)
        outbound_total = snap.get("outbound_total", 0)
        total = inbound_total + outbound_total
        self.stats_text.text = (
            "Connections:       %d\n"
            "Download / In:     %.2f MB\n"
            "Upload / Out:      %.2f MB\n"
            "Total transferred: %.2f MB"
            % (
                snap.get("connections", 0),
                inbound_total / megabyte,
                outbound_total / megabyte,
                total / megabyte,
            )
        )

        started = runtime.get("started_at")
        stopped = runtime.get("stopped_at")
        uptime_end = (
            stopped
            if runtime.get("state") == "Stopped" and stopped
            else __import__("time").time()
        )
        uptime = 0 if not started else max(0, int(uptime_end - started))
        components = runtime.get("components", {})
        lines = [
            "Uptime: %02d:%02d:%02d  Recoveries: %d  Level: %d"
            % (uptime // 3600, (uptime // 60) % 60, uptime % 60,
               runtime.get("recovery_count", 0), runtime.get("recovery_level", 0)),
            "Proxy:    %s" % (runtime.get("proxy_interface") or self.proxy_selection),
            "Inbound local link: %s  (%.0fs without IPv4)" % (
                runtime.get("inbound_recovery_state", "IDLE"),
                runtime.get("inbound_no_ipv4_seconds", 0.0) or 0.0,
            ),
            "Outbound: %s" % (runtime.get("outbound_interface") or self.outbound_selection),
            "Inbound bind: %s" % (runtime.get("listener_bind") or "pending"),
            "Local addresses: %s" % (runtime.get("local_addresses") or "pending"),
            "EN2 diagnostic: %s" % (runtime.get("en2_diagnostic_log") or "off"),
        ]
        if runtime.get("last_recovery_reason"):
            lines.append("Last recovery: %s" % runtime["last_recovery_reason"])
        if runtime.get("last_error"):
            lines.append("Last error: %s" % runtime["last_error"])
        for name in (
            "SOCKS listener", "HTTP listener", "WPAD listener",
            "proxy interface", "outbound interface", "DNS",
            "internet/outbound", "watchdog", "scheduler", "background", "keepalive",
        ):
            lines.append("%-18s %s" % ((name + ":"), components.get(name, "pending")))
        self.health_text.text = "\n".join(lines)

        events = snap.get("events", [])[-5:]
        event_lines = [
            "%s %-10s %s" % (event["timestamp"], event["component"][:10], event["message"])
            for event in events
        ]
        # General log messages include accepted client source/local-destination
        # diagnostics emitted by the shared SOCKS/HTTP listener.
        log_lines = ["listener   %s" % message for message in snap.get("messages", [])[-3:]]
        self.log_text.text = "\n".join(event_lines + log_lines)
