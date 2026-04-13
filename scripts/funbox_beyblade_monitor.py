from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
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
DISPLAY_TIMEZONE = timezone(timedelta(hours=8))

StockStatus = Literal["in_stock", "sold_out", "unknown"]
StoreInventoryStatus = Literal["TRUE", "FALSE", "UNKNOWN"]

TRACKED_STORE_LABELS = {
    "AD318": "AD318台南西門(Funbox Toys & Sanrio Gift Gate)",
    "AD331": "AD331南紡購物中心(Funbox Toys)",
    "AD351": "AD351台南三井(Funbox Toys)",
    "AD311": "AD311台南三越(Funbox Toys)",
    "AD316": "AD316台南遠百(Funbox Toys)",
}
OTHER_STORE_LABEL = "其他"


@dataclass(frozen=True)
class CategoryProduct:
    product_url: str
    catalog_id: str
    name: str
    stock_status: StockStatus = "unknown"


@dataclass(frozen=True)
class ProductDetail:
    name: str
    product_code: str
    price_twd: int | None
    stock_status: StockStatus
    store_inventory: dict[str, StoreInventoryStatus] = field(default_factory=dict)


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
    store_inventory: dict[str, StoreInventoryStatus] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProductSnapshot":
        return cls(
            product_url=str(payload["product_url"]),
            catalog_id=str(payload.get("catalog_id", "")),
            product_code=str(payload.get("product_code", "")),
            name=str(payload["name"]),
            price_twd=int(payload["price_twd"]) if payload.get("price_twd") is not None else None,
            stock_status=_normalize_stock_status(str(payload.get("stock_status", "unknown"))),
            store_inventory=_normalize_store_inventory_summary(payload.get("store_inventory")),
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
        self.telegram_chat_ids = _split_csv_values(_require_env("TELEGRAM_CHAT_ID"))
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
        errors: list[str] = []
        for chat_id in self.telegram_chat_ids:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            try:
                response.raise_for_status()
            except Exception as exc:
                errors.append(f"{chat_id}: {exc}")
        if errors:
            raise NotificationError("; ".join(errors))

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
        store_inventory=_default_store_inventory_summary(),
    )


def fetch_current_products(category_url: str) -> list[ProductSnapshot]:
    if sync_playwright is None:  # pragma: no cover - runtime dependency
        raise RuntimeError("playwright is required to fetch current products")

    with sync_playwright() as playwright:  # pragma: no cover - runtime dependency
        browser = playwright.chromium.launch(headless=True)
        category_page = browser.new_page()
        detail_page = browser.new_page()
        category_products = _fetch_category_products_with_page(category_page, category_url)
        snapshots = []
        for category_product in category_products:
            detail = fetch_product_detail_with_page(detail_page, category_product.product_url)
            snapshots.append(build_product_snapshot(category_product=category_product, detail=detail))
        browser.close()
    return snapshots


def build_product_snapshot(
    *,
    category_product: CategoryProduct,
    detail: ProductDetail,
) -> ProductSnapshot:
    stock_status = _merge_stock_status(
        category_stock_status=category_product.stock_status,
        detail_stock_status=detail.stock_status,
    )
    return ProductSnapshot(
        product_url=category_product.product_url,
        catalog_id=category_product.catalog_id,
        product_code=detail.product_code,
        name=detail.name or category_product.name,
        price_twd=detail.price_twd,
        stock_status=stock_status,
        first_seen_at="",
        last_seen_at="",
        store_inventory=detail.store_inventory,
    )


def fetch_category_products(category_url: str) -> list[CategoryProduct]:
    if sync_playwright is None:  # pragma: no cover - runtime dependency
        raise RuntimeError("playwright is required to fetch the category page")

    with sync_playwright() as playwright:  # pragma: no cover - runtime dependency
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        products = _fetch_category_products_with_page(page, category_url)
        browser.close()
    return products


def _fetch_category_products_with_page(page: object, category_url: str) -> list[CategoryProduct]:
    page.goto(category_url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT_SECONDS * 1000)
    page.wait_for_timeout(2_000)
    items = page.locator('a[href*="/products/"]').evaluate_all(
        """
        (nodes) => {
          const seen = new Map();
          for (const node of nodes) {
            const href = new URL(node.getAttribute('href'), window.location.origin).toString();
            const card = node.closest('[class*="product"], [data-product-id], [data-id], li, .thumbnail') || node.parentElement || node;
            const candidates = [
              node.getAttribute('title'),
              node.textContent,
              node.querySelector('img')?.getAttribute('alt'),
              card.textContent,
            ]
              .map((value) => (value || '').replace(/\\s+/g, ' ').trim())
              .filter(Boolean);
            const name = candidates.sort((a, b) => b.length - a.length)[0] || href.split('/').pop();
            const stockText = (card.textContent || '').replace(/\\s+/g, ' ').trim();
            const dataset = Object.assign({}, card.dataset || {}, node.dataset || {});
            const catalogId = Object.values(dataset).find((value) => /^\\d+$/.test(String(value || ''))) || '';
            if (!seen.has(href) || seen.get(href).name.length < name.length) {
              let stockStatus = 'unknown';
              if (/商品已售完|售完待補貨|庫存不足|已售完|缺貨/.test(stockText)) {
                stockStatus = 'sold_out';
              } else if (/加入購物車|尚有庫存|可購買/.test(stockText)) {
                stockStatus = 'in_stock';
              }
              seen.set(href, { product_url: href, catalog_id: String(catalogId), name, stock_status: stockStatus });
            }
          }
          return Array.from(seen.values());
        }
        """
    )
    html = page.content()

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


def fetch_product_detail_with_page(page: object, product_url: str) -> ProductDetail:
    page.goto(product_url, wait_until="networkidle", timeout=DEFAULT_TIMEOUT_SECONDS * 1000)
    page.wait_for_timeout(1_000)
    store_rows = _fetch_store_inventory_rows_with_page(page)
    payload = page.evaluate(
        """
        () => {
          const text = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const bodyText = text(document.body?.innerText || '');
          const pickText = (selectors) => {
            for (const selector of selectors) {
              const element = document.querySelector(selector);
              const value = text(element?.textContent || '');
              if (value) return value;
            }
            return '';
          };
          const stockElement = Array.from(document.querySelectorAll('body *')).find((element) => {
            const value = text(element.textContent || '');
            return value.startsWith('線上庫存');
          });
          const actionText = Array.from(document.querySelectorAll('button, a, input[type="submit"]'))
            .map((element) => text(element.textContent || element.value || ''))
            .find((value) => /(加入購物車|售完待補貨|商品已售完|已售完|補貨中)/.test(value)) || '';
          return {
            name: pickText(['h1', '.product-title', '[class*="title"]']),
            stock_text: text(stockElement?.textContent || ''),
            action_text: actionText,
            body_text: bodyText,
          };
        }
        """
    )
    body_text = payload["body_text"]
    product_code_match = re.search(r"商品編號\s*[:：]\s*([A-Za-z0-9-]+)", body_text)
    price_match = re.search(r"NT\$\s*([\d,]+)", body_text)
    return ProductDetail(
        name=payload["name"] or body_text[:120].strip(),
        product_code=product_code_match.group(1) if product_code_match else "",
        price_twd=int(price_match.group(1).replace(",", "")) if price_match else None,
        stock_status=resolve_stock_status_from_signals(
            stock_text=payload["stock_text"],
            action_text=payload["action_text"],
            fallback_text=body_text,
        ),
        store_inventory=_summarize_store_inventory_rows(store_rows),
    )


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
    display_checked_at = _format_display_timestamp(checked_at)
    lines = [
        f"Funbox Beyblade 監控通知",
        f"檢查時間: {display_checked_at}",
        f"事件數量: {len(events)}",
        "",
    ]
    for event in events:
        label = "新上架" if event.event_type == "new_listing" else "補貨"
        lines.append(f"[{label}]")
        lines.extend(_format_product_lines(event.product))
        lines.append("")
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
    display_checked_at = _format_display_timestamp(checked_at)
    in_stock = sum(1 for product in products if product.stock_status == "in_stock")
    sold_out = sum(1 for product in products if product.stock_status == "sold_out")
    unknown = sum(1 for product in products if product.stock_status == "unknown")
    lines = [
        "Funbox Beyblade 目前網站狀態",
        f"分類頁: {DEFAULT_CATEGORY_URL}",
        f"檢查時間: {display_checked_at}",
        f"商品總數: {len(products)}",
        f"線上現貨: {in_stock}",
        f"線上缺貨: {sold_out}",
        f"線上庫存未知: {unknown}",
        "",
        "前 10 項商品:",
    ]
    for product in products[:10]:
        lines.extend(_format_product_lines(product, prefix="- "))
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


def _format_display_timestamp(timestamp: str) -> str:
    if not timestamp:
        return timestamp

    candidate = timestamp.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return timestamp

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TIMEZONE).isoformat()


def _format_product_lines(product: ProductSnapshot, *, prefix: str = "") -> list[str]:
    store_inventory_lines = [f"實體庫存:"]
    store_inventory_lines.extend(
        _format_store_inventory_lines(product.store_inventory or _default_store_inventory_summary())
    )
    lines = [
        f"{prefix}商品品項: {product.name}",
        f"線上庫存: {_format_online_stock(product.stock_status)}",
        *store_inventory_lines,
        f"價格: {_format_price(product.price_twd)}",
        f"連結: {product.product_url}",
    ]
    return lines


def _format_online_stock(stock_status: StockStatus) -> str:
    return {
        "in_stock": "線上現貨",
        "sold_out": "線上缺貨",
        "unknown": "線上庫存未知",
    }[stock_status]


def _format_price(price_twd: int | None) -> str:
    return f"NT${price_twd:,}" if price_twd is not None else "價格未知"


def _format_store_inventory_lines(
    store_inventory: dict[str, StoreInventoryStatus],
) -> list[str]:
    normalized = _normalize_store_inventory_summary(store_inventory)
    lines = [
        f"{label}: {normalized[label]}"
        for label in TRACKED_STORE_LABELS.values()
    ]
    lines.append(f"{OTHER_STORE_LABEL}: {normalized[OTHER_STORE_LABEL]}（請直接上官網查詢）")
    return lines


def _fetch_store_inventory_rows_with_page(page: object) -> list[dict[str, str]]:
    inventory_trigger = _first_present_locator(
        page,
        [
            'text=門市庫存狀態查詢',
            'a[href*="inventory_quantities"]',
        ],
    )
    if inventory_trigger is None:
        return []

    inventory_trigger.click()
    page.wait_for_timeout(500)

    south_tab = _first_present_locator(
        page,
        [
            'text=南區',
            'button:has-text("南區")',
            '[role="tab"]:has-text("南區")',
        ],
    )
    if south_tab is not None:
        south_tab.click()
        page.wait_for_timeout(500)

    return page.evaluate(
        """
        () => {
          const text = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const visible = (element) =>
            Boolean(element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length));

          const storeElements = Array.from(document.querySelectorAll('body *')).filter((element) => {
            if (!visible(element)) return false;
            const value = text(element.textContent || '');
            return /AD\\d{3}/.test(value) && /[○△✕×]|熱賣中|即將完售|缺貨中|缺貨|售完|無庫存/.test(value);
          });

          const seen = new Map();
          for (const element of storeElements) {
            const value = text(element.textContent || '');
            const match = value.match(/(AD\\d{3}[^○△✕×]*?)(?:\\s+|)([○△✕×]|熱賣中|即將完售|缺貨中|缺貨|售完|無庫存)/);
            if (!match) continue;
            const storeText = text(match[1]);
            const statusText = text(match[2]);
            const storeCodeMatch = storeText.match(/AD\\d{3}/);
            const key = storeCodeMatch ? storeCodeMatch[0] : storeText;
            if (!seen.has(key)) {
              seen.set(key, {
                store_text: storeText,
                status_text: statusText,
                row_html: element.innerHTML || '',
              });
            }
          }

          return Array.from(seen.values());
        }
        """
    )


def _first_present_locator(page: object, selectors: list[str]) -> object | None:
    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        if not count:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return locator.first
    return None


def _default_store_inventory_summary() -> dict[str, StoreInventoryStatus]:
    return {
        label: "UNKNOWN"
        for label in [*TRACKED_STORE_LABELS.values(), OTHER_STORE_LABEL]
    }


def _normalize_store_inventory_summary(
    payload: object,
) -> dict[str, StoreInventoryStatus]:
    summary = _default_store_inventory_summary()
    if not isinstance(payload, dict):
        return summary

    for key, value in payload.items():
        label = str(key)
        if label in summary:
            summary[label] = _normalize_store_inventory_status(str(value))
    return summary


def _normalize_store_inventory_status(value: str) -> StoreInventoryStatus:
    normalized = value.upper()
    if normalized in {"TRUE", "FALSE", "UNKNOWN"}:
        return normalized
    return "UNKNOWN"


def _summarize_store_inventory_rows(
    rows: object,
) -> dict[str, StoreInventoryStatus]:
    summary = _default_store_inventory_summary()
    other_statuses: list[StoreInventoryStatus] = []
    if not isinstance(rows, list):
        return summary

    for row in rows:
        if not isinstance(row, dict):
            continue

        store_text = str(row.get("store_text", ""))
        status = _resolve_store_inventory_status(
            status_text=str(row.get("status_text", "")),
            row_html=str(row.get("row_html", "")),
        )
        store_code = _extract_store_code(store_text)
        if store_code is None:
            continue
        if store_code in TRACKED_STORE_LABELS:
            label = TRACKED_STORE_LABELS[store_code]
            summary[label] = _prefer_store_inventory_status(summary[label], status)
            continue
        other_statuses.append(status)

    if other_statuses:
        summary[OTHER_STORE_LABEL] = _aggregate_other_store_statuses(other_statuses)
    return summary


def _resolve_store_inventory_status(
    *,
    status_text: str,
    row_html: str,
) -> StoreInventoryStatus:
    combined = f"{status_text} {row_html}".lower()
    sold_out_keywords = (
        "✕",
        "×",
        "x",
        "缺貨中",
        "缺貨",
        "售完",
        "無庫存",
        "soldout",
        "sold-out",
        "outofstock",
        "out-of-stock",
    )
    available_keywords = (
        "○",
        "△",
        "熱賣中",
        "即將完售",
        "尚有庫存",
        "有庫存",
        "available",
        "instock",
        "in-stock",
    )
    if any(keyword in combined for keyword in sold_out_keywords):
        return "FALSE"
    if any(keyword in combined for keyword in available_keywords):
        return "TRUE"
    return "UNKNOWN"


def _extract_store_code(store_text: str) -> str | None:
    match = re.search(r"\b(AD\d{3})\b", store_text)
    if match:
        return match.group(1)
    match = re.match(r"(AD\d{3})", store_text)
    if match:
        return match.group(1)
    return None


def _prefer_store_inventory_status(
    current: StoreInventoryStatus,
    candidate: StoreInventoryStatus,
) -> StoreInventoryStatus:
    priority = {"UNKNOWN": 0, "FALSE": 1, "TRUE": 2}
    return candidate if priority[candidate] > priority[current] else current


def _aggregate_other_store_statuses(
    statuses: list[StoreInventoryStatus],
) -> StoreInventoryStatus:
    if any(status == "TRUE" for status in statuses):
        return "TRUE"
    if any(status == "FALSE" for status in statuses):
        return "FALSE"
    return "UNKNOWN"


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
    stock_text = _extract_primary_stock_text(text)
    if not stock_text:
        return "unknown"
    return resolve_stock_status_from_signals(stock_text=stock_text)


def _merge_stock_status(*, category_stock_status: StockStatus, detail_stock_status: StockStatus) -> StockStatus:
    if category_stock_status == "sold_out" or detail_stock_status == "sold_out":
        return "sold_out"
    if category_stock_status == "in_stock" or detail_stock_status == "in_stock":
        return "in_stock"
    return "unknown"


def resolve_stock_status_from_signals(
    *,
    stock_text: str,
    action_text: str = "",
    fallback_text: str = "",
) -> StockStatus:
    sold_out_keywords = ("已售完", "補貨中", "缺貨", "暫無庫存", "庫存不足", "售完待補貨", "商品已售完")
    in_stock_keywords = ("尚有庫存", "可購買", "現貨供應", "加入購物車")
    combined = " ".join(part for part in (stock_text, action_text, fallback_text) if part)
    if any(keyword in combined for keyword in sold_out_keywords):
        return "sold_out"
    if any(keyword in combined for keyword in in_stock_keywords):
        return "in_stock"
    return "unknown"


def _extract_primary_stock_text(text: str) -> str:
    match = re.search(
        r"線上庫存\s*[:：]\s*(.+?)(?:門市庫存狀態查詢|數量\s*[:：]|商品編號\s*[:：]|加入收藏|加入購物車|$)",
        text,
    )
    if not match:
        return ""
    return match.group(1).strip()


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def _split_csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _require_requests() -> None:
    if requests is None:  # pragma: no cover - runtime dependency
        raise RuntimeError("requests is required to fetch remote pages")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
