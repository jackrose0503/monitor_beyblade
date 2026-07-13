from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import funbox_beyblade_monitor as monitor


class CategoryFetchUnavailable(RuntimeError):
    """Raised when the category page loaded but usable product links are unavailable."""


def install_runtime_patches() -> None:
    monitor.CategoryFetchUnavailable = CategoryFetchUnavailable
    monitor.build_next_state = build_next_state
    monitor.diff_products = diff_products
    monitor._extract_catalog_ids_from_category_html = extract_catalog_ids_from_category_html
    monitor._goto_page_with_ready_dom = goto_page_with_ready_dom
    monitor._fetch_category_products_with_page = fetch_category_products_with_page
    monitor.MonitorRunner.run = monitor_runner_run


def monitor_runner_run(self: object, *, reset_baseline: bool) -> monitor.RunResult:
    checked_at = self.now()
    previous_state = self.state_store.load()
    try:
        current_products = self.fetch_current_products()
    except CategoryFetchUnavailable:
        if previous_state is None or reset_baseline:
            raise
        return monitor.RunResult(
            mode="no_changes",
            checked_at=checked_at,
            product_count=len(previous_state.products),
            events=[],
        )
    if not current_products:
        raise ValueError("Category fetch returned 0 products; aborting state update.")

    next_state = build_next_state(previous_state, current_products, checked_at=checked_at)

    if reset_baseline:
        self.state_store.save(next_state)
        return monitor.RunResult(
            mode="baseline_reset",
            checked_at=checked_at,
            product_count=len(current_products),
            events=[],
        )

    if previous_state is None:
        self.state_store.save(next_state)
        return monitor.RunResult(
            mode="baseline_created",
            checked_at=checked_at,
            product_count=len(current_products),
            events=[],
        )

    events = diff_products(previous_state.products, next_state.products)
    if not events:
        self.state_store.save(next_state)
        return monitor.RunResult(
            mode="no_changes",
            checked_at=checked_at,
            product_count=len(current_products),
            events=[],
        )

    render_notification = getattr(
        self,
        "render_notification",
        lambda events, checked_at: monitor.format_notification_message(
            events=events,
            checked_at=checked_at,
        ),
    )
    message = render_notification(events, checked_at)
    monitor._send_both_notifications(self.send_notification, message)
    self.state_store.save(next_state)
    return monitor.RunResult(
        mode="notified",
        checked_at=checked_at,
        product_count=len(current_products),
        events=events,
    )


def build_next_state(
    previous_state: monitor.MonitorState | None,
    current_products: list[monitor.ProductSnapshot],
    *,
    checked_at: str,
) -> monitor.MonitorState:
    previous_by_key = {}
    if previous_state is not None:
        previous_by_key = {product_identity(product): product for product in previous_state.products}

    merged_products = []
    for product in current_products:
        existing = previous_by_key.get(product_identity(product))
        first_seen_at = existing.first_seen_at if existing is not None else checked_at
        merged_products.append(
            replace(
                product,
                first_seen_at=first_seen_at,
                last_seen_at=checked_at,
            )
        )

    return monitor.MonitorState(checked_at=checked_at, products=merged_products)


def diff_products(
    previous_products: list[monitor.ProductSnapshot],
    current_products: list[monitor.ProductSnapshot],
) -> list[monitor.ProductEvent]:
    previous_by_key = {product_identity(product): product for product in previous_products}
    events: list[monitor.ProductEvent] = []
    for product in current_products:
        previous = previous_by_key.get(product_identity(product))
        if previous is None:
            events.append(monitor.ProductEvent(event_type="new_listing", product=product))
            continue
        if previous.stock_status == "sold_out" and product.stock_status == "in_stock":
            events.append(monitor.ProductEvent(event_type="restock", product=product))
    return events


def product_identity(product: monitor.ProductSnapshot) -> str:
    return product.catalog_id or product.product_code or product.product_url


def fetch_category_products_with_page(page: object, category_url: str) -> list[monitor.CategoryProduct]:
    goto_page_with_ready_dom(page, category_url)
    wait_for_category_product_links(page)
    items = page.locator('a[href*="/products/"]').evaluate_all(
        r"""
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

    products = [monitor.CategoryProduct(**item) for item in items]
    catalog_ids = extract_catalog_ids_from_category_html(html)
    if products and catalog_ids:
        missing_ids = all(not product.catalog_id for product in products)
        if missing_ids and len(catalog_ids) >= len(products):
            products = [
                replace(product, catalog_id=catalog_ids[index])
                for index, product in enumerate(products)
            ]
    if not products:
        if catalog_ids:
            raise CategoryFetchUnavailable(
                f"Category page exposed {len(catalog_ids)} catalog ids but no product links; preserving previous state."
            )
        raise CategoryFetchUnavailable("Category page exposed no product links.")
    return products


def goto_page_with_ready_dom(page: object, url: str) -> None:
    last_error: Exception | None = None
    for wait_until, timeout_ms in (
        ("commit", 15_000),
        ("domcontentloaded", monitor.DEFAULT_TIMEOUT_SECONDS * 1000),
        ("load", monitor.DEFAULT_TIMEOUT_SECONDS * 1000),
    ):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return
        except Exception as exc:
            last_error = exc
            if page_has_loaded_content(page):
                return
            try:
                page.wait_for_timeout(1_000)
            except Exception:
                pass
    if last_error is not None:
        raise last_error


def page_has_loaded_content(page: object) -> bool:
    try:
        html = page.content()
    except Exception:
        return False
    normalized = html.strip().lower()
    return bool(normalized) and (
        "<body" in normalized
        or "/products/" in normalized
        or "線上庫存" in html
        or bool(extract_catalog_ids_from_category_html(html))
    )


def wait_for_category_product_links(page: object) -> None:
    selector = 'a[href*="/products/"]'
    for attempt in range(5):
        try:
            if page.locator(selector).count() > 0:
                return
        except Exception:
            pass
        try:
            page.wait_for_selector(selector, timeout=5_000)
            return
        except Exception:
            pass
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        try:
            page.wait_for_timeout(1_000 + attempt * 500)
        except Exception:
            pass


def extract_catalog_ids_from_category_html(html: str) -> list[str]:
    match = re.search(r"(\d{8}(?:,\d{8})*,?)", html)
    if not match:
        return []
    return [item for item in match.group(1).split(",") if item]


def main() -> int:
    install_runtime_patches()
    return monitor.main()


if __name__ == "__main__":
    raise SystemExit(main())
