"""Central configuration for the optional Stable Mode runtime.

Legacy Mode intentionally keeps the constants and behaviour in ``socks5.py``.
"""

import logging
from dataclasses import dataclass


@dataclass
class StableConfig:
    listen_host: str = "0.0.0.0"
    socks_port: int = 9876
    http_port: int = 9877
    wpad_port: int = 8088

    connection_timeout: float = 12.0
    handshake_timeout: float = 10.0
    max_client_connections: int = 64
    http_max_header_bytes: int = 65536
    udp_max_mappings: int = 256
    udp_mapping_ttl: float = 60.0
    udp_max_pending_tasks: int = 128

    interface_refresh_interval: float = 5.0
    watchdog_interval: float = 5.0
    missing_interface_scans: int = 3
    missing_interface_grace: float = 20.0
    interface_cache_ttl: float = 180.0

    # Revised 4.2: an en* object can remain present while its client-reachable
    # 169.254/16 local-link IPv4 is absent. Real-device testing showed proxy
    # resets do not create that external link, so Stable Mode now waits
    # passively and resumes as soon as the address reappears.
    inbound_ipv4_warn_after: float = 30.0
    # Deprecated compatibility knobs from Revised 4.1; no longer drive resets.
    inbound_ipv4_deep_reset_after: float = 60.0
    inbound_ipv4_post_reset_timeout: float = 30.0

    health_interval: float = 15.0
    external_health_interval: float = 60.0
    health_failures_before_recovery: int = 2
    health_probe_timeout: float = 2.0
    health_confirmation_attempts: int = 3
    health_confirmation_delay: float = 0.75

    restart_initial_delay: float = 1.0
    restart_backoff: float = 2.0
    restart_max_delay: float = 8.0
    healthy_backoff_reset: float = 60.0
    max_consecutive_restarts: int = 6
    restart_pause: float = 60.0

    diagnostics_enabled: bool = True
    # Temporary, removable real-device diagnostic.  It only observes en2 and
    # nearby runtime/listener state; it never drives recovery decisions.
    # Revised 4.3: the high-frequency en2 trace was useful for diagnosing the
    # disappearing-interface issue, but it performs a getifaddrs scan and a
    # synchronous file open/flush every sample. Keep it available, but off by
    # default now that the wired-link behavior is understood.
    en2_diagnostics_enabled: bool = False
    en2_diagnostics_interval: float = 5.0

    # Lightweight background-runtime diagnostics. These run on the proxy event
    # loop and only emit an event when scheduling falls materially behind.
    scheduler_probe_interval: float = 1.0
    scheduler_lag_warn: float = 0.75
    keepalive_recheck_interval: float = 5.0
    # Revised 5: supervise the audio anchor from a plain Python thread as well
    # as the asyncio loop, so a busy/stalled proxy loop cannot be the only
    # component capable of repairing an interrupted background audio session.
    keepalive_supervisor_interval: float = 1.0
    # When Pythonista is backgrounded, keep forwarding traffic but pause the
    # relatively expensive interface/health probes that can provoke recovery
    # during iOS scheduling pressure. Full checking resumes in foreground.
    background_lite_mode: bool = True
    logging_level: int = logging.INFO
    enable_keepalive: bool = True
    prefer_vpn: bool = True


DEFAULT_STABLE_CONFIG = StableConfig()
