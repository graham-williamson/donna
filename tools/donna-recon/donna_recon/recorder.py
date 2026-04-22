"""Async recorder loop.

Attaches Playwright to Chromium over CDP, wires page + network events,
watches for mark requests, drives captures, and flushes cleanly on stop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import (
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Request,
    Response,
    async_playwright,
)

from donna_recon import paths
from donna_recon.redact import redact_headers, redact_request_body, redact_url
from donna_recon.slug import slug_for_label, slug_for_url
from donna_recon.writer import (
    append_jsonl,
    ensure_dir,
    write_snapshot_html,
    write_snapshot_png,
)

log = logging.getLogger("donna_recon.recorder")


def _load_indicator_js() -> str:
    return (Path(__file__).parent / "indicator.js").read_text(encoding="utf-8")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class Recorder:
    """Owns the async lifecycle of a single recording."""

    def __init__(self, recording_dir: Path, cdp_port: int) -> None:
        self._rec = recording_dir
        self._port = cdp_port
        self._seq = 0
        self._stop = asyncio.Event()
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._attached_pages: set[int] = set()
        self._capture_lock = asyncio.Lock()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        ensure_dir(paths.snapshots_dir(self._rec))
        self._pw = await async_playwright().start()
        try:
            browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self._port}"
            )
            if not browser.contexts:
                raise RuntimeError("attached Chromium has no contexts")
            self._context = browser.contexts[0]

            await self._context.expose_function("__donnaMark", self._on_mark)
            await self._context.add_init_script(_load_indicator_js())

            self._context.on("request", self._on_request_sync)
            self._context.on("response", self._on_response_sync)
            self._context.on(
                "page",
                lambda p: asyncio.create_task(self._attach_page(p)),
            )

            for page in self._context.pages:
                await self._attach_page(page)
                # `add_init_script` only applies to subsequent navigations
                # (new documents loaded after the call). Pages that were
                # already open when we attached — typically the one
                # Chromium was launched with via --url — wouldn't get the
                # indicator + F9 handler otherwise. Evaluate the script
                # directly here; its own idempotency guard
                # (`__donnaReconAttached`) prevents double-registration if
                # it also fires on a later navigation.
                try:
                    await page.evaluate(_load_indicator_js())
                except Exception as e:
                    log.warning(
                        "initial indicator injection failed for %s: %s",
                        page.url, e,
                    )
                if page.url and page.url not in ("about:blank", ""):
                    await self._safe_capture(page, label=None)

            watcher = asyncio.create_task(self._watch_mark_req())
            try:
                await self._stop.wait()
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        finally:
            await self._shutdown()

    async def _attach_page(self, page: Page) -> None:
        if id(page) in self._attached_pages:
            return
        self._attached_pages.add(id(page))

        async def on_load(_: Page) -> None:
            await self._safe_capture(page, label=None)

        def on_framenav(frame: Frame) -> None:
            if frame is page.main_frame:
                return  # main frame covered by 'load'
            append_jsonl(
                paths.trace_path(self._rec),
                {
                    "ts": _iso_now(),
                    "type": "subframe_navigation",
                    "url": redact_url(frame.url),
                },
            )

        def on_close(_: Page) -> None:
            append_jsonl(
                paths.trace_path(self._rec),
                {"ts": _iso_now(), "type": "page_close", "url": redact_url(page.url)},
            )

        page.on("load", on_load)
        page.on("framenavigated", on_framenav)
        page.on("close", on_close)

    async def _on_mark(self, label: str) -> None:
        page = self._most_recent_page()
        if page is None:
            return
        await self._safe_capture(page, label=label or "mark")

    def _most_recent_page(self) -> Page | None:
        if self._context is None or not self._context.pages:
            return None
        return self._context.pages[-1]

    def _on_request_sync(self, request: Request) -> None:
        asyncio.create_task(self._on_request(request))

    def _on_response_sync(self, response: Response) -> None:
        asyncio.create_task(self._on_response(response))

    async def _on_request(self, request: Request) -> None:
        try:
            headers = dict(request.headers)
        except Exception:
            headers = {}
        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
        body: dict[str, Any] | None = None
        raw: bytes | None = None
        try:
            candidate = request.post_data_buffer
            if isinstance(candidate, (bytes, bytearray)):
                raw = bytes(candidate)
        except Exception:
            raw = None
        if raw:
            body = redact_request_body(content_type, raw)
        append_jsonl(
            paths.network_path(self._rec),
            {
                "ts": _iso_now(),
                "type": "request",
                "method": request.method,
                "url": redact_url(request.url),
                "headers": redact_headers(headers),
                "body": body,
            },
        )

    async def _on_response(self, response: Response) -> None:
        try:
            headers = dict(response.headers)
        except Exception:
            headers = {}
        content_length = headers.get("content-length") or headers.get("Content-Length")
        try:
            clen: int | None = int(content_length) if content_length else None
        except ValueError:
            clen = None
        append_jsonl(
            paths.network_path(self._rec),
            {
                "ts": _iso_now(),
                "type": "response",
                "status": response.status,
                "url": redact_url(response.url),
                "content_type": headers.get("content-type")
                or headers.get("Content-Type"),
                "content_length": clen,
            },
        )

    async def _safe_capture(self, page: Page, label: str | None) -> None:
        try:
            async with self._capture_lock:
                await self._capture(page, label)
        except Exception as e:
            # Never let a capture failure kill the recorder.
            log.warning("capture failed: %s", e)
            append_jsonl(
                paths.trace_path(self._rec),
                {
                    "ts": _iso_now(),
                    "type": "capture_error",
                    "url": redact_url(page.url if page else ""),
                    "label": label,
                    "error": str(e)[:200],
                },
            )

    async def _capture(self, page: Page, label: str | None) -> None:
        self._seq += 1
        url = page.url
        base = slug_for_label(label) if label else slug_for_url(url)
        filename = f"{self._seq:04d}-{base}"

        html = await page.content()
        write_snapshot_html(
            paths.snapshots_dir(self._rec) / f"{filename}.html", html
        )

        png = await page.screenshot()
        write_snapshot_png(
            paths.snapshots_dir(self._rec) / f"{filename}.png", png
        )

        append_jsonl(
            paths.trace_path(self._rec),
            {
                "ts": _iso_now(),
                "type": "marker" if label else "navigation",
                "seq": self._seq,
                "url": redact_url(url),
                "label": label,
                "slug": base,
            },
        )

    async def _watch_mark_req(self) -> None:
        req = paths.mark_req_path(self._rec)
        while not self._stop.is_set():
            try:
                if req.exists():
                    try:
                        label = req.read_text(encoding="utf-8").strip()
                    except OSError:
                        label = ""
                    with contextlib.suppress(FileNotFoundError):
                        req.unlink()
                    if label:
                        await self._on_mark(label)
            except Exception as e:
                log.warning("mark.req watcher hiccup: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

    async def _shutdown(self) -> None:
        # Best-effort — nothing useful we can do if these fail.
        if self._context is not None:
            browser = self._context.browser
            if browser is not None:
                with contextlib.suppress(Exception):
                    # close() on the CDP-attached browser disconnects
                    # Playwright but leaves Chromium alive; the CLI
                    # terminates Chromium itself via the subprocess handle.
                    await browser.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
