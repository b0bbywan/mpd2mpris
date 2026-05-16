"""Desktop notifications via dbus-fast.

Talks to ``org.freedesktop.Notifications`` directly — no PyGObject /
gi.repository.Notify. The wrapper remembers the last notification id
so subsequent calls *replace* the existing bubble instead of stacking
new ones (matches the behaviour of the original libnotify-based
``NotifyWrapper``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from gettext import gettext as _
from typing import Any

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

logger = logging.getLogger(__name__)

NOTIFICATIONS_BUS = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_IFACE = "org.freedesktop.Notifications"


@dataclass(frozen=True)
class NotifyTemplates:
    """User-supplied format strings for the notification body and
    summary, per playback state. Empty string means "use the built-in
    default"."""
    summary: str = ""
    body: str = ""
    paused_summary: str = ""
    paused_body: str = ""


def _format_duration(secs: float) -> str:
    """Mirror the original ``convert_timestamp``: ``M:SS`` for tracks
    under an hour, ``H:MM:SS`` otherwise."""
    if secs <= 0:
        return "0:00"
    s = int(secs % 60)
    m = int((secs / 60) % 60)
    h = int(secs / 3600)
    if h == 0:
        return f"{m}:{s:02d}"
    return f"{h}:{m:02d}:{s:02d}"


def _variant_value(v: Any) -> Any:
    """Unwrap a ``dbus_fast.Variant`` if needed. ``Any`` so call sites
    can ``int()`` / iterate without intermediate casts — MPRIS Metadata
    values are deliberately polymorphic."""
    return getattr(v, "value", v)


def format_template(
    template: str, meta: dict, *, position_us: int = 0,
) -> str:
    """Expand ``%placeholder%`` tokens against an MPRIS Metadata dict.

    Mirrors the original mpDris2 placeholder set so existing
    configurations keep working. Unknown placeholders are left
    untouched (rather than raising) — friendlier when users typo.
    """
    length_us = _variant_value(meta.get("mpris:length", 0)) or 0
    trackid = str(_variant_value(meta.get("mpris:trackid", "")) or "")
    url = str(_variant_value(meta.get("xesam:url", "")) or "")
    artist = _variant_value(meta.get("xesam:artist", [])) or []
    albumartist = _variant_value(meta.get("xesam:albumArtist", [])) or []
    genre = _variant_value(meta.get("xesam:genre", [])) or []

    values: dict[str, str] = {
        "album": str(_variant_value(meta.get("xesam:album", _("Unknown album")))),
        "title": str(_variant_value(meta.get("xesam:title", _("Unknown title")))),
        "id": trackid.split("/")[-1],
        "time": _format_duration(int(length_us) / 1_000_000),
        "timeposition": _format_duration(position_us / 1_000_000),
        "date": str(_variant_value(meta.get("xesam:contentCreated", ""))),
        "track": str(_variant_value(meta.get("xesam:trackNumber", ""))),
        "disc": str(_variant_value(meta.get("xesam:discNumber", ""))),
        "artist": ", ".join(str(a) for a in artist) or _("Unknown artist"),
        "albumartist": ", ".join(str(a) for a in albumartist),
        "composer": str(_variant_value(meta.get("xesam:composer", ""))),
        "genre": ", ".join(str(g) for g in genre),
        "file": url.split("/")[-1],
    }
    return re.sub(
        r"%([a-z]+)%",
        lambda m: values.get(m.group(1), m.group(0)),
        template,
    )


@dataclass(frozen=True)
class NotifierConfig:
    """Display tuning for the libnotify bubble.

    ``urgency`` maps to the freedesktop Notifications hint (0 low,
    1 normal, 2 critical). ``timeout`` is in milliseconds; ``-1``
    asks the server to apply its default, ``0`` means "never expire".
    """
    urgency: int = 1
    timeout: int = -1


class Notifier:
    def __init__(
        self, bus: MessageBus, app_name: str = "mpDris2",
        config: NotifierConfig | None = None,
    ) -> None:
        self._bus = bus
        self._app_name = app_name
        self._config = config or NotifierConfig()
        self._last_id: int = 0

    async def notify(
        self, summary: str, body: str = "", icon: str = "",
    ) -> None:
        """Fire (or replace) a notification. Failures are logged at
        debug level — no notification daemon is a common, non-fatal
        configuration (headless, ssh sessions, …)."""
        msg = Message(
            destination=NOTIFICATIONS_BUS,
            path=NOTIFICATIONS_PATH,
            interface=NOTIFICATIONS_IFACE,
            member="Notify",
            signature="susssasa{sv}i",
            body=[
                self._app_name,
                self._last_id,    # replaces_id (0 = new bubble)
                icon,
                summary,
                body,
                [],               # actions
                {"urgency": Variant("y", self._config.urgency)},
                self._config.timeout,
            ],
        )
        try:
            reply = await self._bus.call(msg)
        except Exception as e:
            logger.debug("notify call failed: %r", e)
            return
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return
        try:
            self._last_id = int(reply.body[0])
        except (IndexError, TypeError, ValueError):
            self._last_id = 0
