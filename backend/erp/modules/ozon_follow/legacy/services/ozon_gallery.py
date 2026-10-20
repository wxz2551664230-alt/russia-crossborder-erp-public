from __future__ import annotations

import json
import os
import re
import threading
import time
import ctypes
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests

from erp.modules.ozon_follow.legacy.paths import DATA_DIR


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_BROWSER_LOCK = threading.Lock()
_EXTENSION_CAPTURE_FRAGMENT = "ozon-gallery-bridge-auto-close"
_EXTENSION_CAPTURE_QUERY = "__ozon_gallery_bridge"


def _is_ozon_image_host(hostname: str | None) -> bool:
    host = str(hostname or "").casefold()
    return any(host == domain or host.endswith(f".{domain}") for domain in (
        "ozone.ru",
        "ozonstatic.cn",
        "ozonusercontent.com",
    ))


def _extension_capture_path(source_sku: str) -> Path:
    safe_sku = re.sub(r"[^A-Za-z0-9._-]+", "_", str(source_sku or "").strip())
    if not safe_sku:
        raise ValueError("图库桥接缺少商品SKU")
    target_dir = DATA_DIR / "gallery_bridge"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{safe_sku}.json"


def _extension_capture_url(url: str, source_sku: str = "") -> str:
    """Mark only program-opened gallery tabs for capture and automatic closing."""
    parts = urlsplit(str(url or "").strip())
    query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != _EXTENSION_CAPTURE_QUERY
    ]
    query.append((_EXTENSION_CAPTURE_QUERY, "1"))
    if str(source_sku or "").strip():
        query = [(key, value) for key, value in query if key != "__erp_source_sku"]
        query.append(("__erp_source_sku", str(source_sku).strip()))
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        _EXTENSION_CAPTURE_FRAGMENT,
    ))


def _open_normal_browser(url: str) -> bool:
    """Open a visible tab through Windows itself before using webbrowser fallback."""
    if os.name == "nt":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except OSError:
            pass
    return bool(webbrowser.open(url, new=2, autoraise=True))


def save_extension_gallery_capture(
    source_sku: str,
    *,
    page_url: str,
    title: str,
    images: list[Any],
) -> list[str]:
    normalized = normalize_gallery_images(images)
    if not normalized:
        raise ValueError("Edge扩展没有读取到有效的Ozon商品图片")
    target = _extension_capture_path(source_sku)
    payload = {
        "source_sku": str(source_sku),
        "page_url": str(page_url or ""),
        "title": str(title or ""),
        "images": normalized,
        "captured_at": time.time(),
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    return normalized


def _recent_extension_gallery(target: Path, fallback_url: str, *, max_age: int = 120) -> OzonGallery | None:
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        captured_at = float(payload.get("captured_at") or 0)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if captured_at <= 0 or time.time() - captured_at > max(10, int(max_age)):
        return None
    images = normalize_gallery_images(list(payload.get("images") or []))
    if not images:
        return None
    return OzonGallery(
        url=str(payload.get("page_url") or fallback_url),
        title=str(payload.get("title") or "").strip(),
        images=images,
        source="edge-extension",
    )


def wait_for_extension_gallery(
    url: str,
    source_sku: str,
    *,
    timeout: int = 180,
    on_progress: Callable[[str], None] | None = None,
) -> OzonGallery:
    """Open the normal Edge session and wait for the local gallery extension."""
    progress = on_progress or (lambda _message: None)
    target = _extension_capture_path(source_sku)
    existing = _recent_extension_gallery(target, url)
    if existing:
        progress("已收到当前Edge页面回传的Ozon图库")
        return existing
    target.unlink(missing_ok=True)
    progress("Ozon直连受限，正在使用平时的Edge打开商品页")
    if not _open_normal_browser(_extension_capture_url(url, source_sku)):
        raise RuntimeError("无法使用默认浏览器打开Ozon商品页")
    progress("等待Edge中的Ozon图库桥接扩展回传商品图片")
    deadline = time.monotonic() + max(10, min(300, int(timeout or 180)))
    while time.monotonic() < deadline:
        captured = _recent_extension_gallery(target, url, max_age=timeout)
        if captured:
            return captured
        time.sleep(0.5)
    raise RuntimeError(
        "等待正常Edge回传Ozon图库超时；请确认已安装并启用“Ozon图库桥接”扩展"
    )


def _visible_edge_windows() -> set[int]:
    """Return Chromium/Edge top-level handles, including hidden windows."""
    if os.name != "nt":
        return set()
    user32 = ctypes.windll.user32
    handles: set[int] = set()
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd: int, _lparam: int) -> bool:
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, len(class_name))
        if class_name.value.startswith("Chrome_WidgetWin_"):
            handles.add(int(hwnd))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return handles


def _force_window_foreground(handles: set[int]) -> int | None:
    if os.name != "nt" or not handles:
        return None
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    screen_width = max(1024, int(user32.GetSystemMetrics(0)))
    screen_height = max(768, int(user32.GetSystemMetrics(1)))
    width = min(1360, screen_width - 80)
    height = min(900, screen_height - 100)
    for hwnd in sorted(handles, reverse=True):
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.MoveWindow(hwnd, 40, 40, width, height, True)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 40, 40, width, height, SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        if user32.IsWindowVisible(hwnd):
            return hwnd
    return None


@dataclass(slots=True)
class OzonGallery:
    url: str
    title: str
    images: list[str]
    source: str


def prefer_high_resolution_image_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if _is_ozon_image_host(parsed.hostname):
            path = re.sub(r"/wc\d+/", "/wc2000/", parsed.path, flags=re.I)
            return parsed._replace(path=path).geturl()
    except ValueError:
        pass
    return raw


def _image_identity(value: str) -> str:
    try:
        parsed = urlparse(value)
        match = re.search(r"/s3/multimedia[^/]*/wc\d+/([^/?#]+)", parsed.path, flags=re.I)
        return (match.group(1) if match else parsed.netloc + parsed.path).casefold()
    except ValueError:
        return str(value).casefold()


def normalize_gallery_images(values: list[Any], *, limit: int = 15) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        url = prefer_high_resolution_image_url(str(item or ""))
        parsed = urlparse(url)
        if (
            not url.startswith("https://")
            or not _is_ozon_image_host(parsed.hostname)
            or "/s3/multimedia" not in parsed.path.casefold()
        ):
            continue
        identity = _image_identity(url)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(url)
        if len(result) >= limit:
            break
    return result


def additional_gallery_images(images: list[str], primary_image: str, *, limit: int = 14) -> list[str]:
    primary_identity = _image_identity(prefer_high_resolution_image_url(primary_image))
    return [
        url for url in normalize_gallery_images(images, limit=limit + 1)
        if _image_identity(url) != primary_identity
    ][:limit]


def _json_ld_products(page: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for raw in re.findall(
        r'<script[^>]+type=["\'][^"\']*ld\+json[^"\']*["\'][^>]*>(.*?)</script>',
        page,
        flags=re.I | re.S,
    ):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        queue = value if isinstance(value, list) else [value]
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict):
                continue
            if str(item.get("@type") or "").casefold() == "product":
                products.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
    return products


def parse_ozon_gallery_html(url: str, page: str) -> OzonGallery:
    product = next(iter(_json_ld_products(page)), {})
    raw_images = product.get("image") or []
    if not isinstance(raw_images, list):
        raw_images = [raw_images]
    images = normalize_gallery_images(raw_images)
    title = str(product.get("name") or "").strip()
    if not images:
        raise RuntimeError("Ozon页面没有返回可识别的商品图库")
    return OzonGallery(url=url, title=title, images=images, source="json-ld")


def scrape_ozon_gallery_browser(
    url: str,
    *,
    timeout: int = 180,
    on_progress: Callable[[str], None] | None = None,
) -> OzonGallery:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Ozon直连受限且未安装Playwright浏览器组件") from exc

    progress = on_progress or (lambda _message: None)
    # Keep one dedicated Ozon profile so a completed verification survives the
    # 10-second retry cycle.  _BROWSER_LOCK guarantees that only one helper
    # uses this profile at a time.
    profile_dir = DATA_DIR / "ozon_browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    windows_before = _visible_edge_windows()
    progress("Ozon直连受限，正在打开Edge补抓图库；如出现验证页请完成验证")
    with sync_playwright() as playwright:
        context = None
        last_error: Exception | None = None
        for channel in ("msedge", "chrome"):
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel=channel,
                    headless=False,
                    locale="ru-RU",
                    viewport={"width": 1360, "height": 900},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--new-window",
                        "--start-maximized",
                        "--window-position=40,40",
                        "--window-size=1360,900",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                break
            except Exception as exc:
                last_error = exc
        if context is None:
            raise RuntimeError(f"无法启动独立Edge/Chrome读取Ozon图库：{last_error}")
        try:
            browser_page = context.pages[0] if context.pages else context.new_page()
            browser_page.bring_to_front()
            try:
                browser_page.goto(url, wait_until="domcontentloaded", timeout=min(timeout * 1000, 120000))
            except Exception:
                pass
            new_windows: set[int] = set()
            for _ in range(20):
                new_windows = _visible_edge_windows() - windows_before
                if new_windows:
                    break
                time.sleep(0.25)
            foreground = _force_window_foreground(new_windows)
            if foreground:
                progress("独立Edge窗口已强制恢复并显示到桌面前方")
            else:
                raise RuntimeError("Edge进程已启动，但没有检测到真正可显示的窗口")
            deadline = time.monotonic() + timeout
            best_images: list[str] = []
            stable_rounds = 0
            challenge_reported = False
            while time.monotonic() < deadline:
                if browser_page.is_closed():
                    raise RuntimeError("浏览器窗口已关闭，未完成Ozon图库抓取")
                data = browser_page.evaluate(r"""
                    () => {
                      const result = [];
                      const add = (raw) => {
                        let value = String(raw || '').trim();
                        if (value.startsWith('//')) value = `https:${value}`;
                        if (!/^https:\/\//.test(value) || !/ozone\.ru/i.test(value)) return;
                        if (/captcha|abt-challenge|\/icons?\//i.test(value)) return;
                        try {
                          const parsed = new URL(value);
                          parsed.pathname = parsed.pathname.replace(/\/wc\d+\//i, '/wc2000/');
                          value = parsed.toString();
                        } catch (_) {}
                        result.push(value);
                      };
                      const source = (img) => {
                        const set = String(img.getAttribute('srcset') || '').split(',')
                          .map(x => x.trim().split(/\s+/)[0]).filter(Boolean);
                        return set.at(-1) || img.currentSrc || img.src || img.getAttribute('data-src') || '';
                      };
                      const records = [];
                      for (const selector of ['[data-widget="webGallery"] img', '[data-widget*="Gallery"] img', '[data-widget*="gallery"] img']) {
                        for (const img of document.querySelectorAll(selector)) {
                          const rect = img.getBoundingClientRect();
                          records.push({url: source(img), x: rect.x, y: rect.y, w: rect.width, h: rect.height});
                        }
                      }
                      const visible = records.filter(x => x.url && x.w > 0 && x.h > 0);
                      const preview = visible.reduce((a, b) => !a || b.w * b.h > a.w * a.h ? b : a, null);
                      const thumbs = visible.filter(x => Math.max(x.w, x.h) <= 180 && (!preview || x.x + x.w <= preview.x + Math.max(24, preview.w * .12)));
                      for (const item of (thumbs.length ? thumbs : records).sort((a, b) => a.y - b.y || a.x - b.x)) add(item.url);
                      if (!result.length) {
                        for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                          try {
                            const raw = JSON.parse(script.textContent || '{}');
                            const queue = Array.isArray(raw) ? raw : [raw];
                            for (const item of queue) {
                              const images = item?.image || [];
                              for (const value of (Array.isArray(images) ? images : [images])) add(value);
                            }
                          } catch (_) {}
                        }
                      }
                      return {
                        title: document.querySelector('h1')?.innerText?.trim() || '',
                        images: result,
                        pageTitle: document.title || '',
                        pageUrl: location.href,
                        bodyText: document.body?.innerText?.slice(0, 2000) || ''
                      };
                    }
                """)
                images = normalize_gallery_images(list(data.get("images") or []))
                if len(images) > len(best_images):
                    best_images = images
                    stable_rounds = 0
                    progress(f"已读取到{len(best_images)}张Ozon商品图片，正在等待图库完整加载")
                else:
                    stable_rounds += 1
                challenge_text = " ".join((
                    str(data.get("pageTitle") or ""),
                    str(data.get("pageUrl") or ""),
                    str(data.get("bodyText") or ""),
                )).casefold()
                challenged = any(token in challenge_text for token in (
                    "captcha", "abt-challenge", "доступ ограничен", "похоже",
                    "подтвердите", "я не робот",
                ))
                if challenged and not challenge_reported:
                    progress("Ozon要求验证，请在当前专用Edge窗口完成验证")
                    challenge_reported = True
                elif not challenged and challenge_reported:
                    progress("Ozon验证已通过，正在读取商品图库")
                    challenge_reported = False
                if best_images and stable_rounds >= 3 and not challenged:
                    return OzonGallery(
                        url=url,
                        title=str(data.get("title") or "").strip(),
                        images=best_images,
                        source="browser",
                    )
                browser_page.wait_for_timeout(1000)
            raise RuntimeError("等待Ozon页面或人工验证超时，未能读取商品图库")
        finally:
            context.close()


def scrape_ozon_gallery_visible(
    url: str,
    *,
    timeout: int = 180,
    on_progress: Callable[[str], None] | None = None,
) -> OzonGallery:
    """Launch Edge from the running desktop process and keep it user-visible."""
    return scrape_ozon_gallery_browser(
        url,
        timeout=timeout,
        on_progress=on_progress,
    )


def scrape_ozon_gallery(
    url: str,
    *,
    timeout: int = 30,
    browser_fallback: bool = True,
    browser_attempts: int = 3,
    retry_wait_seconds: int = 10,
    on_progress: Callable[[str], None] | None = None,
) -> OzonGallery:
    normalized_url = str(url or "").strip()
    if not normalized_url.startswith(("https://www.ozon.ru/", "https://ozon.ru/")):
        raise ValueError("没有有效的Ozon商品链接，无法补抓附图")
    try:
        response = requests.get(
            normalized_url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
            timeout=timeout,
        )
        response.raise_for_status()
        return parse_ozon_gallery_html(normalized_url, response.text)
    except (requests.RequestException, RuntimeError) as exc:
        if not browser_fallback:
            raise RuntimeError(f"Ozon图库直连抓取失败：{exc}") from exc
        attempts = max(1, min(5, int(browser_attempts or 1)))
        retry_wait = max(1, min(60, int(retry_wait_seconds or 10)))
        progress = on_progress or (lambda _message: None)
        last_error: Exception = exc
        with _BROWSER_LOCK:
            for attempt in range(1, attempts + 1):
                progress(f"正在进行Ozon浏览器图库抓取（第{attempt}/{attempts}次）")
                try:
                    return scrape_ozon_gallery_visible(
                        normalized_url,
                        timeout=max(120, timeout),
                        on_progress=on_progress,
                    )
                except RuntimeError as browser_error:
                    last_error = browser_error
                    if attempt < attempts:
                        progress(
                            f"第{attempt}/{attempts}次抓取失败：{browser_error}；"
                            f"等待{retry_wait}秒后自动重试"
                        )
                        time.sleep(retry_wait)
        raise RuntimeError(f"Ozon图库自动重试{attempts}次后仍失败：{last_error}") from last_error
