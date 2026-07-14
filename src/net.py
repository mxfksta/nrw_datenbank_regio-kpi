"""HTTP-Layer für alle Konnektoren.

Verhalten (bewusst "höflicher" Batch-Client, kein aggressiver Scraper):

- realistischer, identifizierender User-Agent (konfigurierbar)
- respektiert robots.txt je Host (urllib.robotparser, gecacht)
- Rate-Limit: kleine Pause zwischen Requests an denselben Host
- Retries mit exponentiellem Backoff (tenacity) bei Netz-/5xx-/429-Fehlern
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlsplit

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Settings

log = logging.getLogger(__name__)

#: robots.txt-Parser je Host; None = robots.txt nicht abrufbar → als erlaubt behandeln
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
#: Zeitstempel des letzten Requests je Host (für Rate-Limit)
_last_request_at: dict[str, float] = {}


class RobotsDisallowedError(RuntimeError):
    """robots.txt des Hosts untersagt den Abruf dieser URL."""


class RetryableHTTPError(RuntimeError):
    """HTTP-Status, bei dem ein Retry sinnvoll ist (5xx, 429)."""


def _robots_for_host(base_url: str, settings: Settings) -> urllib.robotparser.RobotFileParser | None:
    host = urlsplit(base_url).netloc
    if host in _robots_cache:
        return _robots_cache[host]
    robots_url = f"{urlsplit(base_url).scheme}://{host}/robots.txt"
    parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
    try:
        resp = requests.get(
            robots_url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.http_timeout_seconds,
        )
        if resp.status_code >= 400:
            # Kein robots.txt → Abruf gilt als erlaubt (Konvention)
            parser = None
        else:
            parser.parse(resp.text.splitlines())
    except requests.RequestException:
        log.warning("robots.txt nicht abrufbar, Abruf wird als erlaubt behandelt", extra={"host": host})
        parser = None
    _robots_cache[host] = parser
    return parser


def _respect_rate_limit(url: str, settings: Settings) -> None:
    host = urlsplit(url).netloc
    last = _last_request_at.get(host)
    if last is not None:
        wait_for = settings.rate_limit_seconds - (time.monotonic() - last)
        if wait_for > 0:
            time.sleep(wait_for)
    _last_request_at[host] = time.monotonic()


def http_get(url: str, settings: Settings, params: dict | None = None) -> requests.Response:
    """GET mit robots.txt-Check, Rate-Limit und Retry/Backoff.

    Wirft ``RobotsDisallowedError`` bzw. ``requests.HTTPError`` (4xx) /
    ``RetryableHTTPError`` (nach ausgeschöpften Retries bei 5xx/429).
    """
    robots = _robots_for_host(url, settings)
    if robots is not None and not robots.can_fetch(settings.user_agent, url):
        raise RobotsDisallowedError(f"robots.txt untersagt Abruf: {url}")

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, RetryableHTTPError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _do_get() -> requests.Response:
        _respect_rate_limit(url, settings)
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.http_timeout_seconds,
        )
        if resp.status_code in (429,) or resp.status_code >= 500:
            raise RetryableHTTPError(f"HTTP {resp.status_code} für {url}")
        resp.raise_for_status()
        return resp

    return _do_get()


def http_post(
    url: str,
    settings: Settings,
    *,
    data: dict | None = None,
) -> requests.Response:
    """POST mit Rate-Limit und Retry/Backoff — für authentifizierte Web-APIs.

    Anders als ``http_get`` OHNE robots.txt-Prüfung: Dies ist für Aufrufe
    programmatischer APIs gedacht (z. B. GENESIS/Landesdatenbank), bei denen
    Zugangsdaten im FORM-BODY statt in der URL übergeben werden. So landen die
    Credentials weder in der URL noch in Exceptions/Logs (die nur die URL
    enthalten). robots.txt regelt Crawler und ist hier nicht einschlägig.
    """

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, RetryableHTTPError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _do_post() -> requests.Response:
        _respect_rate_limit(url, settings)
        resp = requests.post(
            url,
            data=data,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.http_timeout_seconds,
        )
        if resp.status_code in (429,) or resp.status_code >= 500:
            raise RetryableHTTPError(f"HTTP {resp.status_code} für {url}")
        resp.raise_for_status()
        return resp

    return _do_post()


def reset_caches() -> None:
    """Setzt robots.txt-/Rate-Limit-Caches zurück (für Tests)."""
    _robots_cache.clear()
    _last_request_at.clear()
