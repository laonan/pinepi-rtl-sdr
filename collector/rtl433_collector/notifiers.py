"""Pluggable push-channel adapters.

A *notifier* takes a rendered image (bytes on disk) plus a short caption and
delivers it to some destination. The abstract :class:`Notifier` defines the
contract; :class:`WeComNotifier` is the first concrete channel. Adding another
channel later (Telegram, Slack, e-mail, ...) is a matter of subclassing
:class:`Notifier` and registering it in :func:`build_notifier`.

WeCom group-bot image message format (webhook):

    POST <webhook-url>
    {"msgtype": "image",
     "image": {"base64": "<base64 of the raw image bytes>",
               "md5": "<md5 hex of the raw image bytes>"}}

The ``md5`` is computed over the *raw* image bytes, before base64 encoding.
WeCom limits bot images to 2 MB (base64) — see the official docs:
https://developer.work.weixin.qq.com/document/path/99110
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

log = logging.getLogger("rtl433_collector.notifiers")

# WeCom hard limit for group-bot images (of the base64-encoded payload).
WECOM_MAX_BASE64_BYTES = 2 * 1024 * 1024


class NotifyError(RuntimeError):
    """Raised when a channel fails to deliver."""


class Notifier(ABC):
    """Abstract push channel.

    Subclasses implement :meth:`send_image`. Callers use :meth:`send_image_file`
    which handles reading the file and computing the digest, so each channel
    only deals with bytes.
    """

    #: short channel name, for logging / config selection
    name: str = "notifier"

    @abstractmethod
    def send_image(self, image_bytes: bytes, *, caption: str = "") -> None:
        """Deliver raw image bytes. Raise :class:`NotifyError` on failure."""

    def send_image_file(self, path: Path | str, *, caption: str = "") -> None:
        data = Path(path).read_bytes()
        self.send_image(data, caption=caption)


class WeComNotifier(Notifier):
    """WeCom (企业微信) group-bot webhook channel.

    :param webhook_url: the full ``.../cgi-bin/webhook/send?key=...`` URL.
    :param timeout: HTTP timeout in seconds.
    """

    name = "wecom"

    def __init__(self, webhook_url: str, *, timeout: float = 10.0) -> None:
        if not webhook_url:
            raise ValueError("WeComNotifier requires a webhook_url")
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send_image(self, image_bytes: bytes, *, caption: str = "") -> None:
        b64 = base64.b64encode(image_bytes)
        if len(b64) > WECOM_MAX_BASE64_BYTES:
            raise NotifyError(
                "image too large for WeCom: base64 is "
                f"{len(b64)} bytes (limit {WECOM_MAX_BASE64_BYTES})"
            )
        md5 = hashlib.md5(image_bytes).hexdigest()
        payload = {
            "msgtype": "image",
            "image": {"base64": b64.decode("ascii"), "md5": md5},
        }
        self._post(payload)

        # WeCom bot images carry no caption, so send the caption as a short
        # follow-up text message when provided.
        if caption:
            try:
                self._post({"msgtype": "text", "text": {"content": caption}})
            except NotifyError:
                # A missing caption is non-fatal; the image already went out.
                log.warning("WeCom caption message failed (image delivered)")

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except HTTPError as exc:  # noqa: PERF203
            raise NotifyError(f"WeCom HTTP {exc.code}: {exc.reason}") from exc
        except URLError as exc:
            raise NotifyError(f"WeCom connection failed: {exc.reason}") from exc

        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise NotifyError(f"WeCom returned non-JSON: {raw[:200]}") from exc

        # WeCom always returns errcode 0 on success.
        if data.get("errcode") not in (0, None):
            raise NotifyError(
                f"WeCom errcode {data.get('errcode')}: {data.get('errmsg')}"
            )


class LogNotifier(Notifier):
    """Fallback channel that just logs. Useful for dry-runs and tests."""

    name = "log"

    def send_image(self, image_bytes: bytes, *, caption: str = "") -> None:
        log.info(
            "[log-notifier] would send %d-byte image; caption=%r",
            len(image_bytes),
            caption,
        )


def build_notifier(channel: str, config: dict) -> Notifier:
    """Factory: construct a notifier for ``channel`` from a config mapping.

    :param channel: one of ``"wecom"``, ``"log"``.
    :param config:  keys depend on the channel, e.g. ``WECOM_WEBHOOK_URL``.

    Add new channels here as they are implemented.
    """
    channel = (channel or "").strip().lower()
    if channel == "wecom":
        return WeComNotifier(
            config.get("WECOM_WEBHOOK_URL", ""),
            timeout=float(config.get("PUSH_TIMEOUT", 10)),
        )
    if channel in ("log", "none", ""):
        return LogNotifier()
    raise ValueError(f"unknown push channel: {channel!r}")
