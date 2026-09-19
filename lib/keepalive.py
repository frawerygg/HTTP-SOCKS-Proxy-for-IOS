"""Optional Pythonista background audio lifecycle for Stable Mode."""

import os
import struct
import tempfile
import threading
import wave


class KeepaliveManager:
    def __init__(self, enabled=True, event_callback=None):
        self.enabled = enabled
        self._event = event_callback or (lambda *args: None)
        self._player = None
        self._audio_session = None
        self._active = False
        self._lock = threading.RLock()
        self._supervisor_thread = None
        self._supervisor_stop = threading.Event()
        self._supervisor_interval = 1.0

    @property
    def active(self):
        """Return whether the keepalive is actually still playing when known.

        Revised 4.2 tracked only whether playback had been started once. iOS can
        interrupt/deactivate an audio session later, leaving that flag stale.
        """
        with self._lock:
            if not self._active or self._player is None:
                return False
            playing = self._player_is_playing_unlocked()
            return self._active if playing is None else bool(playing)

    def _player_is_playing_unlocked(self):
        player = self._player
        if player is None:
            return False
        try:
            # objc_util AVAudioPlayer proxy
            method = getattr(player, "isPlaying", None)
            if callable(method):
                return bool(method())
        except Exception:
            pass
        try:
            # Pythonista sound.Player
            value = getattr(player, "playing")
            return bool(value() if callable(value) else value)
        except Exception:
            return None

    def ensure_active(self):
        """Best-effort repair if iOS interrupted the silent audio keepalive."""
        with self._lock:
            if not self.enabled:
                return False
            if self._active and self._player is not None:
                playing = self._player_is_playing_unlocked()
                if playing is not False:
                    return True
                try:
                    if self._audio_session is not None:
                        self._audio_session.setActive_error_(True, None)
                    if hasattr(self._player, "setNumberOfLoops_"):
                        self._player.setNumberOfLoops_(-1)
                    elif hasattr(self._player, "number_of_loops"):
                        self._player.number_of_loops = -1
                    if hasattr(self._player, "setVolume_"):
                        self._player.setVolume_(0.0)
                    elif hasattr(self._player, "volume"):
                        self._player.volume = 0.0
                    result = self._player.play()
                    self._active = result is not False
                    if self._active:
                        self._event("keepalive", "info", "background audio restarted")
                        return True
                except Exception as exc:
                    self._event(
                        "keepalive", "warning",
                        "background audio restart failed: %s" % exc,
                    )
                    self._active = False
            # Recreate the session/player if it disappeared entirely. RLock
            # permits start() to re-enter safely.
            return self.start()


    def start_supervisor(self, interval=1.0):
        """Continuously repair the audio anchor from a dedicated thread.

        Stable Mode previously relied on its asyncio scheduler task to call
        ``ensure_active``.  If that loop is delayed while Pythonista moves to
        the background, it is exactly the wrong place to make keepalive repair
        depend on.  This tiny thread has no network work and only checks the
        AVAudioPlayer state.
        """
        with self._lock:
            self._supervisor_interval = max(0.25, float(interval))
            thread = self._supervisor_thread
            if thread is not None and thread.is_alive():
                return True
            self._supervisor_stop.clear()
            self._supervisor_thread = threading.Thread(
                target=self._supervisor_main,
                name="proxy-keepalive",
                daemon=True,
            )
            self._supervisor_thread.start()
            return True

    def _supervisor_main(self):
        last_ok = None
        while not self._supervisor_stop.wait(self._supervisor_interval):
            try:
                ok = bool(self.ensure_active())
            except Exception as exc:
                ok = False
                if last_ok is not False:
                    self._event(
                        "keepalive", "warning",
                        "background keepalive supervisor error: %s" % exc,
                    )
            if last_ok is True and not ok:
                self._event(
                    "keepalive", "warning",
                    "background audio keepalive became unavailable",
                )
            elif last_ok is False and ok:
                self._event(
                    "keepalive", "info",
                    "background audio keepalive supervisor recovered playback",
                )
            last_ok = ok

    def stop_supervisor(self):
        with self._lock:
            thread = self._supervisor_thread
            self._supervisor_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        with self._lock:
            if self._supervisor_thread is thread:
                self._supervisor_thread = None

    def start(self):
        with self._lock:
            if self._active:
                return True
            if not self.enabled:
                self._event("keepalive", "info", "background keepalive disabled")
                return False
            try:
                path = self._ensure_silence_file()
                try:
                    from objc_util import ObjCClass

                    AVAudioSession = ObjCClass("AVAudioSession")
                    AVAudioPlayer = ObjCClass("AVAudioPlayer")
                    NSURL = ObjCClass("NSURL")
                    self._audio_session = AVAudioSession.sharedInstance()
                    self._audio_session.setCategory_withOptions_error_(
                        "AVAudioSessionCategoryPlayback", 1, None
                    )
                    self._audio_session.setActive_error_(True, None)
                    url = NSURL.fileURLWithPath_(path)
                    self._player = AVAudioPlayer.alloc().initWithContentsOfURL_error_(
                        url, None
                    )
                    self._player.setNumberOfLoops_(-1)
                    self._player.setVolume_(0.0)
                    self._player.play()
                except ImportError:
                    import sound

                    self._player = sound.Player(path)
                    self._player.number_of_loops = -1
                    self._player.volume = 0.0
                    self._player.play()
                self._active = True
                self._event("keepalive", "info", "background audio active")
                return True
            except Exception as exc:
                self._active = False
                self._event(
                    "keepalive", "warning", "background audio unavailable: %s" % exc
                )
                return False

    def stop(self):
        self.stop_supervisor()
        with self._lock:
            if self._player is not None:
                try:
                    self._player.stop()
                except Exception:
                    try:
                        self._player.pause()
                    except Exception:
                        pass
            self._player = None
            if self._audio_session is not None:
                try:
                    self._audio_session.setActive_error_(False, None)
                except Exception:
                    pass
            self._audio_session = None
            self._active = False
            self._event("keepalive", "info", "background audio stopped")

    @staticmethod
    def _ensure_silence_file():
        path = os.path.join(tempfile.gettempdir(), "ios_proxy_stable_silence.wav")
        if os.path.exists(path) and os.path.getsize(path) > 44:
            return path
        with wave.open(path, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(struct.pack("<h", 0) * 8000)
        return path
