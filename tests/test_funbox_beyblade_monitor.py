from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts.funbox_beyblade_monitor import (
    EnvNotifier,
    MonitorRunner,
    MonitorState,
    NotificationError,
    ProductSnapshot,
    _split_csv_values,
    build_lazy_notification_sender,
    build_next_state,
    diff_products,
    format_status_message,
    parse_args,
    parse_product_detail,
    run_send_status_report,
)


DETAIL_HTML_IN_STOCK = """
<html>
  <body>
    <h1>BEYBLADE X 戰鬥陀螺 BX-44 三角強襲</h1>
    <div>NT$550</div>
    <div>商品編號: BB93952</div>
    <div>線上庫存: 尚有庫存 門市庫存狀態查詢</div>
  </body>
</html>
"""


DETAIL_HTML_SOLD_OUT = """
<html>
  <body>
    <h1>BEYBLADE X 戰鬥陀螺 UX-09 武士星劍 豪華組</h1>
    <div>NT$1,999</div>
    <div>商品編號: BB93953</div>
    <div>線上庫存: 已售完 補貨中</div>
  </body>
</html>
"""


DETAIL_HTML_LOW_STOCK_BLOCKED = """
<html>
  <body>
    <h1>BEYBLADE X 戰鬥陀螺 CX-14 騎士堡壘</h1>
    <div>NT$495</div>
    <div>商品編號: BB09726</div>
    <div>線上庫存: 庫存不足 門市庫存狀態查詢</div>
    <button>售完待補貨</button>
  </body>
</html>
"""


def make_snapshot(
    *,
    url: str = "https://shop.funbox.com.tw/products/bb93952",
    catalog_id: str = "66836005",
    name: str = "BEYBLADE X 戰鬥陀螺 BX-44 三角強襲",
    code: str = "BB93952",
    price: int = 550,
    stock: str = "in_stock",
    first_seen: str = "2026-04-13T00:00:00+00:00",
    last_seen: str = "2026-04-13T00:00:00+00:00",
) -> ProductSnapshot:
    return ProductSnapshot(
        product_url=url,
        catalog_id=catalog_id,
        product_code=code,
        name=name,
        price_twd=price,
        stock_status=stock,
        first_seen_at=first_seen,
        last_seen_at=last_seen,
    )


class ParsingAndDiffTests(unittest.TestCase):
    def test_parse_product_detail_extracts_core_fields(self) -> None:
        detail = parse_product_detail(DETAIL_HTML_IN_STOCK)

        self.assertEqual(detail.name, "BEYBLADE X 戰鬥陀螺 BX-44 三角強襲")
        self.assertEqual(detail.product_code, "BB93952")
        self.assertEqual(detail.price_twd, 550)
        self.assertEqual(detail.stock_status, "in_stock")

    def test_parse_product_detail_marks_sold_out_status(self) -> None:
        detail = parse_product_detail(DETAIL_HTML_SOLD_OUT)

        self.assertEqual(detail.product_code, "BB93953")
        self.assertEqual(detail.price_twd, 1999)
        self.assertEqual(detail.stock_status, "sold_out")

    def test_parse_product_detail_marks_low_stock_blocked_as_sold_out(self) -> None:
        detail = parse_product_detail(DETAIL_HTML_LOW_STOCK_BLOCKED)

        self.assertEqual(detail.product_code, "BB09726")
        self.assertEqual(detail.price_twd, 495)
        self.assertEqual(detail.stock_status, "sold_out")

    def test_diff_products_reports_new_listing_and_restock_only(self) -> None:
        previous = [
            make_snapshot(url="https://shop.funbox.com.tw/products/bb-old", stock="sold_out"),
            make_snapshot(
                url="https://shop.funbox.com.tw/products/bb-known",
                code="BBKNOWN",
                catalog_id="old-2",
                name="Known Product",
                stock="unknown",
            ),
        ]
        current = [
            make_snapshot(url="https://shop.funbox.com.tw/products/bb-old", stock="in_stock"),
            make_snapshot(
                url="https://shop.funbox.com.tw/products/bb-known",
                code="BBKNOWN",
                catalog_id="old-2",
                name="Known Product",
                stock="in_stock",
            ),
            make_snapshot(
                url="https://shop.funbox.com.tw/products/bb-new",
                code="BBNEW",
                catalog_id="new-1",
                name="New Product",
            ),
        ]

        events = diff_products(previous, current)

        self.assertEqual(
            [(event.event_type, event.product.product_url) for event in events],
            [
                ("restock", "https://shop.funbox.com.tw/products/bb-old"),
                ("new_listing", "https://shop.funbox.com.tw/products/bb-new"),
            ],
        )

    def test_build_next_state_preserves_first_seen_and_updates_last_seen(self) -> None:
        previous = MonitorState(
            checked_at="2026-04-13T00:00:00+00:00",
            products=[
                make_snapshot(
                    first_seen="2026-04-12T23:00:00+00:00",
                    last_seen="2026-04-13T00:00:00+00:00",
                )
            ],
        )
        current = [
            make_snapshot(
                first_seen="ignored",
                last_seen="ignored",
            )
        ]

        next_state = build_next_state(previous, current, checked_at="2026-04-13T01:00:00+00:00")

        self.assertEqual(next_state.checked_at, "2026-04-13T01:00:00+00:00")
        self.assertEqual(next_state.products[0].first_seen_at, "2026-04-12T23:00:00+00:00")
        self.assertEqual(next_state.products[0].last_seen_at, "2026-04-13T01:00:00+00:00")


class StubStorage:
    def __init__(self, state: MonitorState | None) -> None:
        self.state = state
        self.saved_state: MonitorState | None = None

    def load(self) -> MonitorState | None:
        return self.state

    def save(self, state: MonitorState) -> None:
        self.saved_state = state
        self.state = state


class StubFetcher:
    def __init__(self, products: list[ProductSnapshot]) -> None:
        self.products = products

    def fetch(self) -> list[ProductSnapshot]:
        return self.products


class StubNotifier:
    def __init__(self, *, fail_channels: set[str] | None = None) -> None:
        self.fail_channels = fail_channels or set()
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    def send(self, channel: str, message: str) -> None:
        self.sent.append((channel, tuple(message.splitlines())))
        if channel in self.fail_channels:
            raise NotificationError(f"{channel} failed")


class MonitorRunnerTests(unittest.TestCase):
    def test_runner_bootstraps_state_without_notifications(self) -> None:
        storage = StubStorage(state=None)
        fetcher = StubFetcher(products=[make_snapshot()])
        notifier = StubNotifier()
        runner = MonitorRunner(
            state_store=storage,
            fetch_current_products=fetcher.fetch,
            send_notification=notifier.send,
            now=lambda: "2026-04-13T01:00:00+00:00",
        )

        result = runner.run(reset_baseline=False)

        self.assertEqual(result.mode, "baseline_created")
        self.assertEqual(notifier.sent, [])
        self.assertIsNotNone(storage.saved_state)
        self.assertEqual(
            storage.saved_state.products[0].first_seen_at,
            "2026-04-13T01:00:00+00:00",
        )

    def test_runner_does_not_persist_state_when_any_notification_fails(self) -> None:
        previous_state = MonitorState(
            checked_at="2026-04-13T00:00:00+00:00",
            products=[make_snapshot(stock="sold_out")],
        )
        current_products = [make_snapshot(stock="in_stock")]
        storage = StubStorage(state=previous_state)
        fetcher = StubFetcher(products=current_products)
        notifier = StubNotifier(fail_channels={"email"})
        runner = MonitorRunner(
            state_store=storage,
            fetch_current_products=fetcher.fetch,
            send_notification=notifier.send,
            now=lambda: "2026-04-13T01:00:00+00:00",
        )

        with self.assertRaises(NotificationError):
            runner.run(reset_baseline=False)

        self.assertEqual([channel for channel, _ in notifier.sent], ["telegram", "email"])
        self.assertIsNone(storage.saved_state)
        self.assertEqual(storage.state, previous_state)

    def test_runner_rejects_empty_product_results(self) -> None:
        storage = StubStorage(state=None)
        fetcher = StubFetcher(products=[])
        notifier = StubNotifier()
        runner = MonitorRunner(
            state_store=storage,
            fetch_current_products=fetcher.fetch,
            send_notification=notifier.send,
            now=lambda: "2026-04-13T01:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "0 products"):
            runner.run(reset_baseline=False)


class LazyNotifierTests(unittest.TestCase):
    def test_lazy_notification_sender_constructs_notifier_only_on_first_send(self) -> None:
        constructed: list[str] = []
        sent: list[tuple[str, str]] = []

        class LazyNotifier:
            def send(self, channel: str, message: str) -> None:
                sent.append((channel, message))

        def factory() -> LazyNotifier:
            constructed.append("built")
            return LazyNotifier()

        sender = build_lazy_notification_sender(factory)

        self.assertEqual(constructed, [])
        sender("telegram", "hello")
        sender("email", "world")

        self.assertEqual(constructed, ["built"])
        self.assertEqual(sent, [("telegram", "hello"), ("email", "world")])

    def test_run_send_status_report_sends_both_channels(self) -> None:
        sent: list[tuple[str, str]] = []

        def sender(channel: str, message: str) -> None:
            sent.append((channel, message))

        run_send_status_report(
            send_notification=sender,
            checked_at="2026-04-13T01:00:00+00:00",
            products=[
                make_snapshot(
                    name="A",
                    stock="in_stock",
                    price=100,
                    url="https://shop.funbox.com.tw/products/a",
                ),
                make_snapshot(
                    name="B",
                    stock="sold_out",
                    price=200,
                    url="https://shop.funbox.com.tw/products/b",
                ),
            ],
        )

        self.assertEqual([channel for channel, _ in sent], ["telegram", "email"])
        self.assertIn("目前網站狀態", sent[0][1])

    def test_format_status_message_summarizes_stock_counts(self) -> None:
        message = format_status_message(
            checked_at="2026-04-13T01:00:00+00:00",
            products=[
                make_snapshot(
                    name="A",
                    stock="in_stock",
                    price=100,
                    url="https://shop.funbox.com.tw/products/a",
                ),
                make_snapshot(
                    name="B",
                    stock="sold_out",
                    price=200,
                    url="https://shop.funbox.com.tw/products/b",
                ),
                make_snapshot(
                    name="C",
                    stock="unknown",
                    price=300,
                    url="https://shop.funbox.com.tw/products/c",
                ),
            ],
        )

        self.assertIn("商品總數: 3", message)
        self.assertIn("現貨: 1", message)
        self.assertIn("缺貨: 1", message)
        self.assertIn("庫存未知: 1", message)
        self.assertIn("https://shop.funbox.com.tw/categories/takaratomy/beyblade", message)


class CliArgumentTests(unittest.TestCase):
    def test_parse_args_accepts_status_query_mode(self) -> None:
        args = parse_args(["--send-status-report"])

        self.assertTrue(args.send_status_report)


class RecipientParsingTests(unittest.TestCase):
    def test_split_csv_values_handles_multiple_chat_ids(self) -> None:
        self.assertEqual(_split_csv_values("12345, -10098765"), ["12345", "-10098765"])

    @patch("scripts.funbox_beyblade_monitor.requests.post")
    def test_env_notifier_sends_telegram_to_multiple_chat_ids(self, post: MagicMock) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        post.return_value = response

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "12345,-10098765",
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
                "SMTP_USERNAME": "user",
                "SMTP_PASSWORD": "pass",
                "EMAIL_FROM": "from@example.com",
                "EMAIL_TO": "to@example.com",
            },
            clear=False,
        ):
            notifier = EnvNotifier()
            notifier._send_telegram("hello")

        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["chat_id"], "12345")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["chat_id"], "-10098765")
