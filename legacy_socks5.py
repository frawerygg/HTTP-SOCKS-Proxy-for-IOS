#!python3
"""Direct launcher for the preserved original UI and server manager."""

from socks5 import IS_PYTHONISTA, run_console, run_with_ui


if __name__ == "__main__":
    if IS_PYTHONISTA:
        run_with_ui(mode="legacy")
    else:
        run_console()
