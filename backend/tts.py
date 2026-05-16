"""
tts.py – Outbound text-to-speech via Google Home (Chromecast).

Connects to a Google Home device by IP and casts a Google TTS audio URL so
the device speaks the supplied message aloud.  Requires pychromecast.

If the device IP is empty or the cast fails, the error is logged and the
caller is not interrupted — TTS is always best-effort.
"""

from __future__ import annotations

import logging
import time
import urllib.parse

logger = logging.getLogger(__name__)

_TTS_URL_TEMPLATE = (
    "https://translate.google.com/translate_tts"
    "?ie=UTF-8&client=tw-ob&tl={lang}&q={text}"
)


def _build_tts_url(message: str, language: str = "en") -> str:
    """Return a Google Translate TTS URL for the given message."""
    return _TTS_URL_TEMPLATE.format(
        lang=urllib.parse.quote(language),
        text=urllib.parse.quote(message),
    )


def speak_on_google_home(
    message: str,
    device_ip: str,
    language: str = "en",
    wait_seconds: int = 20,
) -> bool:
    """
    Cast a spoken TTS message to a Google Home / Chromecast device.

    :param message:      Text to speak.
    :param device_ip:    IP address of the target device.
    :param language:     BCP-47 language tag (default ``"en"``).
    :param wait_seconds: How long to wait for playback before disconnecting.
    :returns:            ``True`` if the cast was initiated successfully,
                         ``False`` otherwise.
    """
    if not device_ip:
        logger.debug("TTS skipped: no device_ip configured.")
        return False

    if not message:
        logger.debug("TTS skipped: empty message.")
        return False

    try:
        import pychromecast  # deferred so the app starts without pychromecast installed
    except ImportError:
        logger.warning(
            "pychromecast is not installed. "
            "Run `pip install pychromecast` to enable Google Home TTS."
        )
        return False

    tts_url = _build_tts_url(message, language)
    cast = None

    try:
        cast = pychromecast.Chromecast(device_ip)
        cast.wait()

        mc = cast.media_controller
        mc.play_media(tts_url, "audio/mpeg")
        mc.block_until_active()

        # Give the device time to finish speaking before we disconnect.
        time.sleep(wait_seconds)

        cast.quit_app()
        logger.info("TTS cast to %s: %r", device_ip, message)
        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning("TTS cast to %s failed: %s", device_ip, exc)
        return False

    finally:
        if cast is not None:
            try:
                cast.disconnect()
            except Exception:  # noqa: BLE001
                pass
