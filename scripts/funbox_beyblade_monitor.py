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
DEFAULT_STORE_SUBSCRIPTIONS_FILE = "config/store_subscriptions.json"
DEFAULT_TIMEOUT_SECONDS = 30
DISPLAY_TIMEZONE = timezone(timedelta(hours=8))
DISPLAY_TIMEZONE_LABEL = "UTC+8"
OTHER_STORE_LABEL = "其他"

StockStatus = Literal["in_stock", "sold_out", "unknown"]
StoreInventoryStatus = Literal["TRUE", "FALSE", "UNKNOWN"]

TRACKED_STORE_LABELS = {
    "AD318": "AD318台南西門(Funbox Toys & Sanrio Gift Gate)",
    "AD331": "AD331南紡購物中心(Funbox Toys)",
    "AD351": "AD351台南三井(Funbox Toys)",
    "AD311": "AD311台南三越(Funbox Toys)",
    "AD316": "AD316台南遠百(Funbox Toys)",
}


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


@dataclass(frozen=True)
class StoreSubscription:
    store_code: str
    store_label: str
    enabled: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "store_code": self.store_code,
            "store_label": self.store_label,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class StoreSubscriptions:
    include_other: bool
    stores: list[StoreSubscription]

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "StoreSubscriptions":
        include_other = _coerce_bool(payload.get("include_other", True))
        stores_by_code: dict[str, StoreSubscription] = {}
        for item in payload.get("stores", []):
            if not isinstance(item, dict):
                continue
            store_code = _extract_store_code(
                str(item.get("store_code") or item.get("store_label") or "")
            )
            store_label = _canonicalize_store_label(
                str(item.get("store_label") or item.get("store_code") or "")
            )
            if not store_code or not store_label:
                continue
            stores_by_code[store_code] = StoreSubscription(
                store_code=store_code,
                store_label=store_label,
                enabled=_coerce_bool(item.get("enabled", False)),
            )
        stores = sorted(stores_by_code.values(), key=lambda item: _sort_store_code(item.store_code))
        return cls(include_other=include_other, stores=stores)

    def to_dict(self) -> dict[str, object]:
        return {
            "include_other": self.include_other,
            "stores": [store.to_dict() for store in self.stores],
        }


def default_store_subscriptions() -> StoreSubscriptions:
    return StoreSubscriptions(
        include_other=True,
        stores=[
            StoreSubscription(store_code=code, store_label=label, enabled=True)
            for code, label in sorted(TRACKED_STORE_LABELS.items(), key=lambda item: _sort_store_code(item[0]))
        ],
    )


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


class JsonStoreSubscriptionsStore:
    def __init__(self, subscriptions_file: Path) -> None:
        self.subscriptions_file = subscriptions_file

    def load(self) -> StoreSubscriptions:
        if not self.subscriptions_file.exists():
            return default_store_subscriptions()

        payload = json.loads(self.subscriptions_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return default_store_subscriptions()
        return StoreSubscriptions.from_dict(payload)

    def save(self, subscriptions: StoreSubscriptions) -> None:
        self.subscriptions_file.parent.mkdir(parents=True, exist_ok=True)
        self.subscriptions_file.write_text(
            json.dumps(subscriptions.to_dict(), ensure_ascii=False, indent=2) + "\n",
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
        render_notification: Callable[[list[ProductEvent], str], str] | None = None,
    ) -> None:
        self.state_store = state_store
        self.fetch_current_products = fetch_current_products
        self.send_notification = send_notification
        self.now = now
        self.render_notification = render_notification or (
            lambda events, checked_at: format_notification_message(
                events=events,
                checked_at=checked_at,
            )
        )

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

        message = self.render_notification(events, checked_at)
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
    _goto_page_with_ready_dom(page, category_url)
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
              .map((value) => (value || '').replace(/\s+/g, ' ').trim())
              .filter(Boolean);
            const name = candidates.sort((a, b) => b.length - a.length)[0] || href.split('/').pop();
            const stockText = (card.textContent || '').replace(/\s+/g, ' ').trim();
            const dataset = Object.assign({}, card.dataset || {}, node.dataset || {});
            const catalogId = Object.values(dataset).find((value) => /^\d+$/.test(String(value || ''))) || '';
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
    _goto_page_with_ready_dom(page, product_url)
    page.wait_for_timeout(1_000)
    store_rows = _fetch_store_inventory_rows_with_page(page)
    payload = page.evaluate(
        """
        () => {
          const text = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const visible = (element) =>
            Boolean(element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length));
          const disabled = (element) => {
            if (!element) return true;
            const classText = String(element.className || '');
            return (
              element.hasAttribute?.('disabled') ||
              element.getAttribute?.('aria-disabled') === 'true' ||
              /disabled|is-disabled|btn-disabled/.test(classText)
            );
          };
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
          const canAddToCart = Array.from(document.querySelectorAll('button, a, input[type="submit"], input[type="button"]'))
            .some((element) => {
              const value = text(element.textContent || element.value || '');
              return /加入購物車/.test(value) && visible(element) && !disabled(element);
            });
          return {
            name: pickText(['h1', '.product-title', '[class*="title"]']),
            stock_text: text(stockElement?.textContent || ''),
            action_text: actionText,
            can_add_to_cart: canAddToCart,
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
            can_add_to_cart=bool(payload.get("can_add_to_cart")),
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


def sync_store_catalog(
    *,
    category_url: str,
    subscriptions_store: JsonStoreSubscriptionsStore,
) -> tuple[StoreSubscriptions, list[ProductSnapshot]]:
    products = fetch_current_products(category_url)
    if not products:
        raise ValueError("Category fetch returned 0 products; cannot sync store catalog.")
    existing = subscriptions_store.load()
    synced = build_synced_store_subscriptions(existing, products)
    subscriptions_store.save(synced)
    return synced, products


def build_synced_store_subscriptions(
    existing: StoreSubscriptions,
    products: list[ProductSnapshot],
) -> StoreSubscriptions:
    discovered_catalog = build_store_catalog_from_products(products)
    existing_by_code = {store.store_code: store for store in existing.stores}
    merged_codes = sorted(
        set(discovered_catalog) | set(existing_by_code),
        key=_sort_store_code,
    )
    merged_stores = []
    for code in merged_codes:
        existing_store = existing_by_code.get(code)
        store_label = discovered_catalog.get(code, existing_store.store_label if existing_store else code)
        merged_stores.append(
            StoreSubscription(
                store_code=code,
                store_label=store_label,
                enabled=existing_store.enabled if existing_store is not None else False,
            )
        )
    return StoreSubscriptions(include_other=existing.include_other, stores=merged_stores)


def build_store_catalog_from_products(products: list[ProductSnapshot]) -> dict[str, str]:
    catalog: dict[str, str] = {}
    for product in products:
        for label in _normalize_store_inventory_summary(product.store_inventory):
            if label == OTHER_STORE_LABEL:
                continue
            store_code = _extract_store_code(label)
            if not store_code:
                continue
            candidate_label = _canonicalize_store_label(label)
            current_label = catalog.get(store_code)
            if current_label is None:
                catalog[store_code] = candidate_label
                continue
            catalog[store_code] = _prefer_store_label(current_label, candidate_label)
    return {
        code: catalog[code]
        for code in sorted(catalog, key=_sort_store_code)
    }


def _goto_page_with_ready_dom(page: object, url: str) -> None:
    last_error: Exception | None = None
    for wait_until in ("domcontentloaded", "load"):
        try:
            page.goto(url, wait_until=wait_until, timeout=DEFAULT_TIMEOUT_SECONDS * 1000)
            return
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def format_notification_message(
    *,
    events: list[ProductEvent],
    checked_at: str,
    store_subscriptions: StoreSubscriptions | None = None,
) -> str:
    display_checked_at = _format_display_timestamp(checked_at)
    new_listing_count = sum(1 for event in events if event.event_type == "new_listing")
    restock_count = sum(1 for event in events if event.event_type == "restock")
    lines = [
        "Funbox Beyblade 監控通知",
        f"檢查時間: {display_checked_at}",
        f"事件數量: {len(events)}",
        f"異動統計: 新上架 {new_listing_count} | 補貨 {restock_count}",
        "",
    ]
    for event in events:
        lines.extend(_format_product_lines(event.product, store_subscriptions=store_subscriptions))
        lines.append("")
    return "\n".join(lines).strip()


def main() -> int:
    args = parse_args()
    subscriptions_store = JsonStoreSubscriptionsStore(Path(args.store_subscriptions_file))

    if args.sync_store_catalog:
        synced_subscriptions, products = sync_store_catalog(
            category_url=args.category_url,
            subscriptions_store=subscriptions_store,
        )
        checked_at = current_timestamp()
        print(
            json.dumps(
                {
                    "mode": "store_catalog_synced",
                    "checked_at": checked_at,
                    "product_count": len(products),
                    "store_count": len(synced_subscriptions.stores),
                    "enabled_store_count": sum(1 for store in synced_subscriptions.stores if store.enabled),
                },
                ensure_ascii=False,
            )
        )
        return 0

    store_subscriptions = subscriptions_store.load()
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
            category_url=args.category_url,
            store_subscriptions=store_subscriptions,
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
        render_notification=lambda events, checked_at: format_notification_message(
            events=events,
            checked_at=checked_at,
            store_subscriptions=store_subscriptions,
        ),
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
    parser.add_argument("--store-subscriptions-file", default=DEFAULT_STORE_SUBSCRIPTIONS_FILE)
    parser.add_argument("--reset-baseline", action="store_true")
    parser.add_argument("--send-status-report", action="store_true")
    parser.add_argument("--sync-store-catalog", action="store_true")
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
    category_url: str = DEFAULT_CATEGORY_URL,
    store_subscriptions: StoreSubscriptions | None = None,
) -> None:
    message = format_status_message(
        checked_at=checked_at,
        products=products,
        category_url=category_url,
        store_subscriptions=store_subscriptions,
    )
    for channel in ("telegram", "email"):
        send_notification(channel, message)


def format_status_message(
    *,
    checked_at: str,
    products: list[ProductSnapshot],
    category_url: str = DEFAULT_CATEGORY_URL,
    store_subscriptions: StoreSubscriptions | None = None,
) -> str:
    display_checked_at = _format_display_timestamp(checked_at)
    in_stock = sum(1 for product in products if product.stock_status == "in_stock")
    sold_out = sum(1 for product in products if product.stock_status == "sold_out")
    unknown = sum(1 for product in products if product.stock_status == "unknown")
    lines = [
        "Funbox Beyblade 目前網站狀態",
        f"分類頁: {category_url}",
        f"檢查時間: {display_checked_at}",
        f"商品總數: {len(products)}",
        f"線上統計: 🟢 {in_stock} | 🔴 {sold_out} | 🟡 {unknown}",
        "",
        "前 10 項商品:",
    ]
    for index, product in enumerate(products[:10], start=1):
        lines.extend(
            _format_product_lines(
                product,
                index=index,
                store_subscriptions=store_subscriptions,
            )
        )
        lines.append("")
    return "\n".join(lines).strip()


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
    display_time = parsed.astimezone(DISPLAY_TIMEZONE)
    return f"{display_time.strftime('%Y-%m-%d %H:%M:%S')} ({DISPLAY_TIMEZONE_LABEL})"


def _format_product_lines(
    product: ProductSnapshot,
    *,
    index: int | None = None,
    store_subscriptions: StoreSubscriptions | None = None,
) -> list[str]:
    title = product.name if index is None else f"{index}. {product.name}"
    lines = [
        title,
        f"線上: {_format_online_stock(product.stock_status)}",
        "實體:",
        *_format_store_inventory_lines(product.store_inventory, store_subscriptions=store_subscriptions),
        f"價格: {_format_price(product.price_twd)}",
        f"連結: {product.product_url}",
    ]
    return lines


def _format_online_stock(stock_status: StockStatus) -> str:
    return {
        "in_stock": "🟢 有貨",
        "sold_out": "🔴 沒貨",
        "unknown": "🟡 未知",
    }[stock_status]


def _format_price(price_twd: int | None) -> str:
    return f"NT${price_twd:,}" if price_twd is not None else "價格未知"


def _format_store_inventory_lines(
    store_inventory: dict[str, StoreInventoryStatus],
    *,
    store_subscriptions: StoreSubscriptions | None = None,
) -> list[str]:
    subscriptions = store_subscriptions or default_store_subscriptions()
    inventory_by_code, legacy_other_statuses = _index_store_inventory_by_code(store_inventory)
    enabled_stores = [store for store in subscriptions.stores if store.enabled]
    lines: list[str] = []

    if enabled_stores:
        for store in enabled_stores:
            inventory_entry = inventory_by_code.get(store.store_code)
            status = inventory_entry[1] if inventory_entry is not None else "UNKNOWN"
            lines.append(
                f"- {_short_store_display_label(store.store_label)}: {_format_store_inventory_status(status)}"
            )
    else:
        lines.append("- 未設定訂閱門市")

    if subscriptions.include_other:
        enabled_codes = {store.store_code for store in enabled_stores}
        other_statuses = [
            status
            for code, (_, status) in inventory_by_code.items()
            if code not in enabled_codes
        ]
        other_statuses.extend(legacy_other_statuses)
        other_status = _aggregate_other_store_statuses(other_statuses)
        lines.append(
            f"- {OTHER_STORE_LABEL}: {_format_store_inventory_status(other_status)} 請直接上官網查詢"
        )

    return lines


def _format_store_inventory_status(status: StoreInventoryStatus) -> str:
    return {
        "TRUE": "🟢",
        "FALSE": "🔴",
        "UNKNOWN": "🟡",
    }[status]


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

    _click_locator_with_force_fallback(inventory_trigger)
    page.wait_for_timeout(500)

    payload = page.evaluate(
        """
        () => {
          const text = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const paneElements = Array.from(document.querySelectorAll('[id*="inventory_quantities_tab_content"], .tab-pane'))
            .filter((element) => /AD\d{3}/.test(text(element.textContent || '')));
          const rowContainers = paneElements.length ? paneElements : [document.body];
          const seen = new Map();

          for (const container of rowContainers) {
            const storeElements = Array.from(container.querySelectorAll('tr, li, .row, [class*="inventory"], [class*="store"]'));
            for (const element of storeElements) {
              const value = text(element.textContent || '');
              if (!/AD\d{3}/.test(value)) continue;

              const cells = Array.from(element.querySelectorAll('th, td'));
              let storeText = '';
              let statusText = '';
              let statusHtml = '';

              if (cells.length >= 2 && /AD\d{3}/.test(text(cells[0].textContent || ''))) {
                storeText = text(cells[0].textContent || '');
                const statusCell = cells[cells.length - 1];
                statusText = text(statusCell.textContent || '');
                statusHtml = statusCell.innerHTML || '';
              } else {
                const match = value.match(/(AD\d{3}.*?)(?:\s+|)([○△✕×]|熱賣中|即將完售|缺貨中|缺貨|售完|無庫存)?$/);
                if (!match) continue;
                storeText = text(match[1] || '');
                statusText = text(match[2] || '');
                statusHtml = element.innerHTML || '';
              }

              const storeCodeMatch = storeText.match(/AD\d{3}/);
              const key = storeCodeMatch ? storeCodeMatch[0] : storeText;
              if (!seen.has(key)) {
                seen.set(key, {
                  store_text: storeText,
                  status_text: statusText,
                  row_html: statusHtml,
                });
              }
            }
          }

          return {
            pane_candidates: paneElements.map((element) => ({
              id: element.id || '',
              text: element.innerText || element.textContent || '',
            })),
            rows: Array.from(seen.values()),
          };
        }
        """
    )
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    extracted_rows = _extract_all_store_inventory_rows_from_payload(payload)
    if extracted_rows:
        return extracted_rows

    fallback_rows = payload.get("rows")
    return fallback_rows if isinstance(fallback_rows, list) else []


def _extract_all_store_inventory_rows_from_payload(payload: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    pane_candidates = payload.get("pane_candidates")
    if isinstance(pane_candidates, list):
        for candidate in pane_candidates:
            if not isinstance(candidate, dict):
                continue
            rows.extend(_extract_store_inventory_rows_from_text(str(candidate.get("text", ""))))

    pane_text = payload.get("pane_text")
    if isinstance(pane_text, str):
        rows.extend(_extract_store_inventory_rows_from_text(pane_text))

    fallback_rows = payload.get("rows")
    if isinstance(fallback_rows, list):
        rows.extend(item for item in fallback_rows if isinstance(item, dict))
    return rows


def _click_locator_with_force_fallback(locator: object, *, tolerate_failure: bool = False) -> bool:
    try:
        locator.click()
        return True
    except Exception:
        try:
            locator.click(force=True)
            return True
        except Exception:
            if tolerate_failure:
                return False
            raise


def _extract_store_inventory_rows_from_text(text_blob: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_store_codes: set[str] = set()
    if not text_blob.strip():
        return rows

    status_pattern = r"(○|△|✕|×|熱賣中|即將完售|缺貨中|缺貨|售完|無庫存)"
    lines = [line.strip() for line in text_blob.splitlines() if line.strip()]
    index = 0
    while index < len(lines):
        line = re.sub(r"\s+", " ", lines[index]).strip()
        if not re.search(r"AD\d{3}", line):
            index += 1
            continue

        status_match = re.search(rf"{status_pattern}\s*$", line)
        store_text = line
        status_text = ""

        if status_match is not None:
            store_text = line[: status_match.start()].strip()
            status_text = status_match.group(1)
        elif index + 1 < len(lines):
            next_line = re.sub(r"\s+", " ", lines[index + 1]).strip()
            if re.fullmatch(status_pattern, next_line):
                status_text = next_line
                index += 1

        store_code = _extract_store_code(store_text)
        if store_code is None or store_code in seen_store_codes:
            index += 1
            continue

        rows.append(
            {
                "store_text": store_text,
                "status_text": status_text,
                "row_html": "",
            }
        )
        seen_store_codes.add(store_code)
        index += 1

    return rows


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
    return {}


def _normalize_store_inventory_summary(
    payload: object,
) -> dict[str, StoreInventoryStatus]:
    summary: dict[str, StoreInventoryStatus] = {}
    if not isinstance(payload, dict):
        return summary

    for key, value in payload.items():
        label = _canonicalize_store_label(str(key))
        if not label:
            continue
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
    summary_by_code: dict[str, tuple[str, StoreInventoryStatus]] = {}
    if not isinstance(rows, list):
        return {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        store_text = _canonicalize_store_label(str(row.get("store_text", "")))
        status = _resolve_store_inventory_status(
            status_text=str(row.get("status_text", "")),
            row_html=str(row.get("row_html", "")),
        )
        store_code = _extract_store_code(store_text)
        if store_code is None:
            continue

        current = summary_by_code.get(store_code)
        if current is None:
            summary_by_code[store_code] = (store_text, status)
            continue

        current_label, current_status = current
        summary_by_code[store_code] = (
            _prefer_store_label(current_label, store_text),
            _prefer_store_inventory_status(current_status, status),
        )

    return {
        label: status
        for _, (label, status) in sorted(summary_by_code.items(), key=lambda item: _sort_store_code(item[0]))
    }


def _index_store_inventory_by_code(
    store_inventory: dict[str, StoreInventoryStatus],
) -> tuple[dict[str, tuple[str, StoreInventoryStatus]], list[StoreInventoryStatus]]:
    inventory_by_code: dict[str, tuple[str, StoreInventoryStatus]] = {}
    legacy_other_statuses: list[StoreInventoryStatus] = []
    for label, status in _normalize_store_inventory_summary(store_inventory).items():
        if label == OTHER_STORE_LABEL:
            legacy_other_statuses.append(status)
            continue
        store_code = _extract_store_code(label)
        if not store_code:
            continue
        current = inventory_by_code.get(store_code)
        if current is None:
            inventory_by_code[store_code] = (label, status)
            continue
        current_label, current_status = current
        inventory_by_code[store_code] = (
            _prefer_store_label(current_label, label),
            _prefer_store_inventory_status(current_status, status),
        )
    return inventory_by_code, legacy_other_statuses


def _resolve_store_inventory_status(
    *,
    status_text: str,
    row_html: str,
) -> StoreInventoryStatus:
    combined = f"{status_text} {row_html}".lower()
    sold_out_keywords = (
        "✕",
        "×",
        "缺貨中",
        "缺貨",
        "售完",
        "無庫存",
        "soldout",
        "sold-out",
        "outofstock",
        "out-of-stock",
        "fa-times",
        "fa-close",
        "text-danger",
        "status-danger",
        "inventory-status-danger",
        "text-red",
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
        "fa-circle",
        "fa-dot-circle",
        "text-success",
        "status-success",
        "inventory-status-success",
        "text-warning",
        "status-warning",
        "inventory-status-warning",
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


def _canonicalize_store_label(store_text: str) -> str:
    value = re.sub(r"\s+", " ", store_text).strip()
    if not value:
        return ""
    if value == OTHER_STORE_LABEL:
        return OTHER_STORE_LABEL
    match = re.search(r"(AD\d{3}.*?\))", value)
    if match:
        return match.group(1).strip()
    match = re.search(r"(AD\d{3}.*)", value)
    if match:
        return match.group(1).strip()
    return value


def _short_store_display_label(store_label: str) -> str:
    canonical = _canonicalize_store_label(store_label)
    match = re.match(r"(AD\d{3})(.*?)(?:\([^)]*\))?$", canonical)
    if not match:
        return canonical
    store_code = match.group(1)
    store_name = re.sub(r"\s+", " ", match.group(2)).strip()
    if not store_name:
        return store_code
    return f"{store_code} {store_name}"


def _prefer_store_label(current: str, candidate: str) -> str:
    current_has_brand = "(" in current and ")" in current
    candidate_has_brand = "(" in candidate and ")" in candidate
    current_score = (int(current_has_brand), -len(current), current)
    candidate_score = (int(candidate_has_brand), -len(candidate), candidate)
    return candidate if candidate_score > current_score else current


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


def _sort_store_code(store_code: str) -> tuple[str, int, str]:
    match = re.match(r"([A-Za-z]+)(\d+)", store_code)
    if not match:
        return (store_code, 0, store_code)
    return (match.group(1), int(match.group(2)), store_code)


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
    if detail_stock_status != "unknown":
        return detail_stock_status
    return category_stock_status


def resolve_stock_status_from_signals(
    *,
    stock_text: str,
    action_text: str = "",
    can_add_to_cart: bool = False,
    fallback_text: str = "",
) -> StockStatus:
    sold_out_keywords = ("已售完", "補貨中", "缺貨", "暫無庫存", "庫存不足", "售完待補貨", "商品已售完")
    in_stock_keywords = ("尚有庫存", "可購買", "現貨供應", "熱賣中", "即將完售")
    combined = " ".join(part for part in (stock_text, action_text, fallback_text) if part)
    if can_add_to_cart:
        return "in_stock"
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


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


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
