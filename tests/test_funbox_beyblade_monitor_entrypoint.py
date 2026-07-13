from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from scripts import funbox_beyblade_monitor as monitor
from scripts import funbox_beyblade_monitor_entrypoint as entrypoint


def make_snapshot(
    *,
    url: str = "https://shop.funbox.com.tw/products/bbpr98085",
    catalog_id: str = "68051695",
    product_code: str = "BBPR98085",
    stock_status: str = "in_stock",
) -> monitor.ProductSnapshot:
    return monitor.ProductSnapshot(
        product_url=url,
        catalog_id=catalog_id,
        product_code=product_code,
        name="BEYBLADE X 戰鬥陀螺 BXG-30 蜘蛛人 / 猛毒",
        price_twd=799,
        stock_status=stock_status,
        first_seen_at="2026-04-13T00:00:00+00:00",
        last_seen_at="2026-04-13T00:00:00+00:00",
        store_inventory={},
    )


class StubStateStore:
    def __init__(self, state: monitor.MonitorState | None) -> None:
        self.state = state
        self.saved_state: monitor.MonitorState | None = None

    def load(self) -> monitor.MonitorState | None:
        return self.state

    def save(self, state: monitor.MonitorState) -> None:
        self.saved_state = state
        self.state = state


class CategoryPageStubLocator:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self.items = items

    def count(self) -> int:
        return len(self.items)

    def evaluate_all(self, _script: str) -> list[dict[str, str]]:
        return self.items


class CategoryPageStub:
    def __init__(self, *, items: list[dict[str, str]], html: str) -> None:
        self.items = items
        self.html = html

    def goto(self, _url: str, *, wait_until: str, timeout: int) -> None:
        return None

    def locator(self, _selector: str) -> CategoryPageStubLocator:
        return CategoryPageStubLocator(self.items)

    def wait_for_selector(self, _selector: str, *, timeout: int) -> None:
        if not self.items:
            raise RuntimeError("no product links")

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def evaluate(self, _script: str) -> None:
        return None

    def content(self) -> str:
        return self.html


class EntrypointPatchTests(unittest.TestCase):
    def test_entrypoint_can_be_executed_by_script_path(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "scripts/funbox_beyblade_monitor_entrypoint.py",
                "--help",
            ],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--send-status-report", result.stdout)

    def test_diff_products_uses_catalog_id_not_product_url(self) -> None:
        previous = [
            make_snapshot(
                url="https://shop.funbox.com.tw/products/bbpr98085",
                catalog_id="68051695",
                stock_status="sold_out",
            )
        ]
        current = [
            make_snapshot(
                url="https://shop.funbox.com.tw/products/68051695",
                catalog_id="68051695",
                stock_status="in_stock",
            )
        ]

        events = entrypoint.diff_products(previous, current)

        self.assertEqual([(event.event_type, event.product.catalog_id) for event in events], [("restock", "68051695")])

    def test_category_fetch_refuses_to_build_synthetic_product_urls_from_catalog_ids(self) -> None:
        page = CategoryPageStub(
            items=[],
            html="<html><body>68051695,67777867,67754775,</body></html>",
        )

        with self.assertRaises(entrypoint.CategoryFetchUnavailable):
            entrypoint.fetch_category_products_with_page(
                page,
                "https://shop.funbox.com.tw/categories/takaratomy/beyblade",
            )

    def test_runner_preserves_previous_state_when_category_links_are_unavailable(self) -> None:
        previous_state = monitor.MonitorState(
            checked_at="2026-04-13T00:00:00+00:00",
            products=[make_snapshot()],
        )
        state_store = StubStateStore(previous_state)
        sent: list[tuple[str, str]] = []
        runner = monitor.MonitorRunner(
            state_store=state_store,
            fetch_current_products=lambda: (_ for _ in ()).throw(
                entrypoint.CategoryFetchUnavailable("links unavailable")
            ),
            send_notification=lambda channel, message: sent.append((channel, message)),
            now=lambda: "2026-04-13T01:00:00+00:00",
        )
        original_run = monitor.MonitorRunner.run
        try:
            monitor.MonitorRunner.run = entrypoint.monitor_runner_run
            result = runner.run(reset_baseline=False)
        finally:
            monitor.MonitorRunner.run = original_run

        self.assertEqual(result.mode, "no_changes")
        self.assertEqual(result.product_count, 1)
        self.assertEqual(sent, [])
        self.assertIsNone(state_store.saved_state)


if __name__ == "__main__":
    unittest.main()
