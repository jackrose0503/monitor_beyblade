from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from typing import Callable, Literal
try:
    import requests
except ImportError:  # pragma: no cover - optional at test time
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - optional at test time
    BeautifulSoup = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional at test time
    sync_playwright = None


DEFAULT_CATEGORY_URL = "https://shop.funbox.com.tw/categories/takaratomy/beyblade"
DEFAULT_STATE_FILE = "monitor-state/state/funbox-beyblade.json"
DEFAULT_TIMEOUT_SECONDS = 30

StockStatus = Literal["in_stock", "sold_out", "unknown"]


@dataclass(frozen=True)
class CategoryProduct:
    product_url: str
    catalog_id: str
    name: str


@dataclass(frozen=True)
class ProductDetail:
    name: str
    product_code: str
    price_twd: int | None
    stock_status: StockStatus


@dataclass(frozen=True)
class ProductSnapshot:
    product_url: str
    catalog_id: str
    product_code: str
    name: str
    price_twd: int | None
    stock_status: StockStatus
    first_seen_at: str
    last_seen_at: str

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProductSnapshot":
        return cls(
            product_url=str(payload["product_url"]),
            catalog_id=str(payload.get("catalog_id", "")),
            product_code=str(payload.get("product_code", "")),
            name=str(payload["name"]),
            price_twd=int(payload["price_twd"]) if payload.get("price_twd") is not None else None,
            stock_status=_normalize_stock_status(str(payload.get("stock_status", "unknown"))),
            first_seen_at=str(payload["first_seen_at"]),
            last_seen_at=str(payload["last_seen_at"]),
        )


@dataclass(frozen=True)
class MonitorState:
    checked_at: str
    products: list[ProductSnapshot]

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "MonitorState":
        products = [ProductSnapshot.from_dict(item) for item in payload.get("products", [])]
        return cls(checked_at=str(payload.get("checked_at", "")), products=products)

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at,
            "products": [asdict(product) for product in self.products],
        }


@dataclass(frozen=True)
class ProductEvent:
    event_type: Literal["new_listing", "restock"]
    product: ProductSnapshot


@dataclass(frozen=True)
class RunResult:
    mode: Literal["baseline_created", "baseline_reset", "no_changes", "notified"]
    checked_at: str
    product_count: int
    events: list[ProductEvent]


class NotificationError(RuntimeError):
    pass


class JsonStateStore:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file

    def load(self) -> MonitorState | None:
        if not self.state_file.exists():
            return None

        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        return MonitorState.from_dict(payload)

    def save(self, state: MonitorState) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class EnvNotifier:
    def __init__(self) -> None:
        self.telegram_bot_token = _require_env("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = _require_env("TELEGRAM_CHAT_ID")
        self.smtp_host = _require_env("SMTP_HOST")
        self.smtp_port = int(_require_env("SMTP_PORT"))
        self.smtp_username = _require_env("SMTP_USERNAME")
        self.smtp_password = _require_env("SMTP_PASSWORD")
        self.email_from = _require_env("EMAIL_FROM")
        self.email_to = _require_env("EMAIL_TO")

    def send(self, channel: str, message: str) -> None:
        if channel == "telegram":
            self._send_telegram(message)
            return
        if channel == "email":
            self._send_email(message)
            return
        raise ValueError(f"Unsupported notification channel: {channel}")

    def _send_telegram(self, message: str) -> None:
        _require_requests()
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        response = requests.post(
            url,
            json={
                "chat_id": self.telegram_chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

    def _send_email(self, message: str) -> None:
        email = EmailMessage()
        email["From"] = self.email_from
        email["To"] = self.email_to
        email["Subject"] = "Funbox Beyblade 監控通知"
        email.set_content(message)

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=DEFAULT_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            smtp.login(self.smtp_username, self.smtp_password)
            smtp.send_message(email)


class MonitorRunner:
    def __init__(
        self,
        *,
        state_store: object,
        fetch_current_products: Callable[[], list[ProductSnapshot]],
        send_notification: Callable[[str, str], None],
        now: Callable[[], str],
    ) -> None:
        self.state_store = state_store
        self.fetch_current_products = fetch_current_products
        self.send_notification = send_notification
        self.now = now

    def run(self, *, reset_baseline: bool) -> RunResult:
        checked_at = self.now()
        current_products = self.fetch_current_products()
        if not current_products:
            raise ValueError("Category fetch returned 0 products; aborting state update.")

        previous_state = self.state_store.load()
        next_state = build_next_state(previous_state, current_products, checked_at=checked_at)

        if reset_baseline:
            self.state_store.save(next_state)
            return RunResult(
                mode="baseline_reset",
                checked_at=checked_at,
                product_count=len(current_products),
                events=[],
            )

        if previous_state is None:
            self.state_store.save(next_state)
            return RunResult(
                mode="baseline_created",
                checked_at=checked_at,
                product_count=len(current_products),
                events=[],
            )

        events = diff_products(previous_state.products, next_state.products)
        if not events:
            self.state_store.save(next_state)
            return RunResult(
                mode="no_changes",
                checked_at=checked_at,
                product_count=len(current_products),
                events=[],
            )

        message = format_notification_message(events=events, checked_at=checked_at)
        _send_both_notifications(self.send_notification, message)
        self.state_store.save(next_state)
        return RunResult(
            mode="notified",
            checked_at=checked_at,
            product_count=len(current_products),
            events=events,
        )


def build_next_state(
    previous_state: MonitorState | None,
    current_products: list[ProductSnapshot],
    *,
    checked_at: str,
) -> MonitorState:
    previous_by_url = {}
    if previous_state is not None:
        previous_by_url = {product.product_url: product for product in previous_state.products}

    merged_products = []
    for product in current_products:
        existing = previous_by_url.get(product.product_url)
        first_seen_at = existing.first_seen_at if existing is not None else checked_at
        merged_products.append(
            replace(
                product,
                first_seen_at=first_seen_at,
                last_seen_at=checked_at,
            )
        )

    return MonitorState(checked_at=checked_at, products=merged_products)


def diff_products(
    previous_products: list[ProductSnapshot],
    current_products: list[ProductSnapshot],
) -> list[ProductEvent]:
    previous_by_url = {product.product_url: product for product in previous_products}
    events: list[ProductEvent] = []
    for product in current_products:
        previous = previous_by_url.get(product.product_url)
        if previous is None:
            events.append(ProductEvent(event_type="new_listing", product=product))
            continue
        if previous.stock_status == "sold_out" and product.stock_status == "in_stock":
            events.append(ProductEvent(event_type="restock", product=product))
    return events


def parse_product_detail(html: str) -> ProductDetail:
    text = _extract_text(html)
    name = _extract_name(html, text)
    product_code_match = re.search(r"商品編號\s*[:：]\s*([A-Za-z0-9-]+)", text)
    product_code = product_code_match.group(1) if product_code_match else ""
    price_match = re.search(r"NT\$\s*([\d,]+)", text)
    price_twd = int(price_match.group(1).replace(",", "")) if price_match else None
    stock_status = _parse_stock_status(text)
    return ProductDetail(
        name=name,
        product_code=product_code,
        price_twd=price_twd,
        stock_status=stock_status,
    )


def fetch_current_products(category_url: str) -> list[ProductSnapshot]:
    category_products = fetch_category_products(category_url)
    snapshots = []
    for category_product in category_products:
        detail_html = fetch_url_text(category_product.product_url)
        detail = parse_product_detail(detail_html)
        name = detail.name or category_product.name
        snapshots.append(
            ProductSnapshot(
                product_url=category_product.product_url,
                catalog_id=category_product.catalog_id,
                product_code=detail.product_code,
                name=name,
                price_twd=detail.price_twd,
                stock_status=detail.stock_status,
                first_seen_at="",
                last_seen_at="",
            )
        )
    return snapshots


def fetch_category_products(category_url: str) -> list[CategoryProduct]:
    if sync_playwright is None:  # pragma: no cover - runtime dependency
        raise RuntimeError("playwright is required to fetch the category page")

    with sync_playwright() as playwright:  # pragma: no cover - runtime dependency
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(category_url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT_SECONDS * 1000)
        page.wait_for_timeout(2_000)
        items = page.locator('a[href*="/products/"]').evaluate_all(
            """
            (nodes) => {
              const seen = new Map();
              for (const node of nodes) {
                const href = new URL(node.getAttribute('href'), window.location.origin).toString();
                const card = node.closest('[class*="product"], [data-product-id], [data-id]') || node;
                const candidates = [
                  node.getAttribute('title'),
                  node.textContent,
                  node.querySelector('img')?.getAttribute('alt'),
                  card.textContent,
                ]
                  .map((value) => (value || '').replace(/\\s+/g, ' ').trim())
                  .filter(Boolean);
                const name = candidates.sort((a, b) => b.length - a.length)[0] || href.split('/').pop();
                const dataset = Object.assign({}, card.dataset || {}, node.dataset || {});
                const catalogId = Object.values(dataset).find((value) => /^\\d+$/.test(String(value || ''))) || '';
                if (!seen.has(href) || seen.get(href).name.length < name.length) {
                  seen.set(href, { product_url: href, catalog_id: String(catalogId), name });
                }
              }
              return Array.from(seen.values());
            }
            """
        )
        html = page.content()
        browser.close()

    products = [CategoryProduct(**item) for item in items]
    catalog_ids = _extract_catalog_ids_from_category_html(html)
    if products and catalog_ids:
        missing_ids = all(not product.catalog_id for product in products)
        if missing_ids and len(catalog_ids) >= len(products):
            products = [
                replace(product, catalog_id=catalog_ids[index])
                for index, product in enumerate(products)
            ]
    return products


def fetch_url_text(url: str) -> str:
    _require_requests()
    response = requests.get(
        url,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    response.raise_for_status()
    return response.text


def format_notification_message(*, events: list[ProductEvent], checked_at: str) -> str:
    lines = [
        f"Funbox Beyblade 監控通知",
        f"檢查時間: {checked_at}",
        f"事件數量: {len(events)}",
        "",
    ]
    for event in events:
        label = "新上架" if event.event_type == "new_listing" else "補貨"
        price = f"NT${event.product.price_twd:,}" if event.product.price_twd is not None else "價格未知"
        stock = {
            "in_stock": "尚有庫存",
            "sold_out": "已售完",
            "unknown": "庫存未知",
        }[event.product.stock_status]
        lines.extend(
            [
                f"[{label}] {event.product.name}",
                f"商品編號: {event.product.product_code or '未知'}",
                f"分類商品 ID: {event.product.catalog_id or '未知'}",
                f"價格: {price}",
                f"庫存: {stock}",
                f"連結: {event.product.product_url}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def main() -> int:
    args = parse_args()
    send_notification = build_lazy_notification_sender(EnvNotifier)
    if args.send_status_report:
        products = fetch_current_products(args.category_url)
        if not products:
            raise ValueError("Category fetch returned 0 products; cannot build status report.")
        checked_at = current_timestamp()
        run_send_status_report(
            send_notification=send_notification,
            checked_at=checked_at,
            products=products,
        )
        print(
            json.dumps(
                {
                    "mode": "status_report_sent",
                    "checked_at": checked_at,
                    "product_count": len(products),
                },
                ensure_ascii=False,
            )
        )
        return 0

    state_store = JsonStateStore(Path(args.state_file))
    runner = MonitorRunner(
        state_store=state_store,
        fetch_current_products=lambda: fetch_current_products(args.category_url),
        send_notification=send_notification,
        now=current_timestamp,
    )
    result = runner.run(reset_baseline=args.reset_baseline)
    print(
        json.dumps(
            {
                "mode": result.mode,
                "checked_at": result.checked_at,
                "product_count": result.product_count,
                "event_count": len(result.events),
            },
            ensure_ascii=False,
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Funbox Beyblade listings for new products and restocks.")
    parser.add_argument("--category-url", default=DEFAULT_CATEGORY_URL)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--reset-baseline", action="store_true")
    parser.add_argument("--send-status-report", action="store_true")
    return parser.parse_args(argv)


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_lazy_notification_sender(factory: Callable[[], EnvNotifier]) -> Callable[[str, str], None]:
    notifier: EnvNotifier | None = None

    def send(channel: str, message: str) -> None:
        nonlocal notifier
        if notifier is None:
            notifier = factory()
        notifier.send(channel, message)

    return send


def run_send_status_report(
    *,
    send_notification: Callable[[str, str], None],
    checked_at: str,
    products: list[ProductSnapshot],
) -> None:
    message = format_status_message(checked_at=checked_at, products=products)
    for channel in ("telegram", "email"):
        send_notification(channel, message)


def format_status_message(*, checked_at: str, products: list[ProductSnapshot]) -> str:
    in_stock = sum(1 for product in products if product.stock_status == "in_stock")
    sold_out = sum(1 for product in products if product.stock_status == "sold_out")
    unknown = sum(1 for product in products if product.stock_status == "unknown")
    lines = [
        "Funbox Beyblade 目前網站狀態",
        f"分類頁: {DEFAULT_CATEGORY_URL}",
        f"檢查時間: {checked_at}",
        f"商品總數: {len(products)}",
        f"現貨: {in_stock}",
        f"缺貨: {sold_out}",
        f"庫存未知: {unknown}",
        "",
        "前 10 項商品:",
    ]
    for product in products[:10]:
        price = f"NT${product.price_twd:,}" if product.price_twd is not None else "價格未知"
        stock = {
            "in_stock": "現貨",
            "sold_out": "缺貨",
            "unknown": "未知",
        }[product.stock_status]
        lines.extend(
            [
                f"- {product.name}",
                f"  庫存: {stock} | 價格: {price}",
                f"  連結: {product.product_url}",
            ]
        )
    return "\n".join(lines)


def _send_both_notifications(send_notification: Callable[[str, str], None], message: str) -> None:
    errors: list[str] = []
    for channel in ("telegram", "email"):
        try:
            send_notification(channel, message)
        except Exception as exc:  # pragma: no cover - narrow behavior exercised by stubs
            errors.append(f"{channel}: {exc}")
    if errors:
        raise NotificationError("; ".join(errors))


def _extract_text(html: str) -> str:
    if BeautifulSoup is not None:
        return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    stripped = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(stripped)).strip()


def _extract_name(html: str, text: str) -> str:
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.find("h1")
        if heading is not None:
            return heading.get_text(" ", strip=True)
        if soup.title is not None:
            return soup.title.get_text(" ", strip=True)

    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if h1_match:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h1_match.group(1))).strip()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_match:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title_match.group(1))).strip()
    return text[:120].strip()


def _extract_catalog_ids_from_category_html(html: str) -> list[str]:
    match = re.search(r"(\d{8}(?:,\d{8})+,?)", html)
    if not match:
        return []
    return [item for item in match.group(1).split(",") if item]


def _normalize_stock_status(value: str) -> StockStatus:
    if value in {"in_stock", "sold_out", "unknown"}:
        return value
    return "unknown"


def _parse_stock_status(text: str) -> StockStatus:
    if "線上庫存" not in text:
        return "unknown"
    if any(keyword in text for keyword in ("尚有庫存", "可購買", "現貨供應")):
        return "in_stock"
    if any(
        keyword in text
        for keyword in ("已售完", "補貨中", "缺貨", "暫無庫存", "庫存不足", "售完待補貨")
    ):
        return "sold_out"
    return "unknown"


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def _require_requests() -> None:
    if requests is None:  # pragma: no cover - runtime dependency
        raise RuntimeError("requests is required to fetch remote pages")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
