# Funbox Beyblade Monitor

監控 [Funbox Beyblade 分類頁](https://shop.funbox.com.tw/categories/takaratomy/beyblade) 是否有：

- `new_listing` 新上架
- `restock` 補貨

預設由 GitHub Actions 每 5 分鐘執行一次，狀態檔寫入 `monitor-state` 分支。

## Required Secrets

在 GitHub repository secrets 設定以下值：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

可先參考 [.env.example](./.env.example) 的欄位名稱與格式。
`TELEGRAM_CHAT_ID` 可填單一 chat id，或用逗號分隔多個 chat id，例如 `123456789,-1001234567890`，就能同時通知私訊與群組。

## Store Subscriptions

實體門市通知由 [config/store_subscriptions.json](./config/store_subscriptions.json) 控制。

- `enabled: true` 的門市，會出現在 Email / Telegram 的商品明細中。
- `enabled: false` 的門市，不會逐店列出。
- `include_other: true` 時，會額外顯示一列 `其他`，表示未勾選門市是否仍有任一店有貨。
- 預設已幫你啟用 5 間台南店。

格式範例：

```json
{
  "include_other": true,
  "stores": [
    {
      "store_code": "AD311",
      "store_label": "AD311台南三越(Funbox Toys)",
      "enabled": true
    }
  ]
}
```

## Sync Store Catalog

如果你想把官網目前所有門市名單同步回 repo：

1. 到 `Actions -> Funbox Beyblade Monitor -> Run workflow`
2. branch 選 `main`
3. 把 `sync_store_catalog` 設成 `true`
4. 執行 workflow

workflow 會抓最新門市清單，並更新 `config/store_subscriptions.json`。
既有門市的 `enabled` 設定會保留；新發現的門市會加入清單，預設 `enabled: false`。

同步完成後，你可以直接在 GitHub 或本地修改 `config/store_subscriptions.json`，決定哪些門市要列入通知。

## Workflow Behavior

- 首次執行只建立 baseline，不發通知。
- `workflow_dispatch` 可手動執行，並支援 `reset_baseline=true` 強制重建 baseline。
- `workflow_dispatch` 也支援 `send_status_report=true`，手動回報 `categories/takaratomy/beyblade` 目前網站狀態到 Telegram 與 Email。
- `workflow_dispatch` 也支援 `sync_store_catalog=true`，刷新 `config/store_subscriptions.json` 的門市名單。
- 只有 Telegram 和 Email 都發送成功時，才會更新 `monitor-state/state/funbox-beyblade.json`。
- baseline 建立與 `reset_baseline` 不會主動初始化通知器，所以可先驗證抓取流程，再補通知 secrets。
- 若當輪抓到 0 個商品，workflow 會失敗，避免覆蓋有效 state。

## Local Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python scripts/funbox_beyblade_monitor.py --state-file ./tmp/state/funbox-beyblade.json --reset-baseline
```

同步門市清單：

```bash
python scripts/funbox_beyblade_monitor.py \
  --sync-store-catalog \
  --store-subscriptions-file config/store_subscriptions.json
```

如果這次執行會產生通知，才需要先設定 Telegram / SMTP 環境變數。

## GitHub Setup

1. 建立一個新的 public GitHub repository，並把這個目錄內容 push 上去。
2. 到 `Settings -> Secrets and variables -> Actions`，新增 README 上列出的 8 個 secrets。
3. 到 `Actions` 頁面，第一次手動執行 `Funbox Beyblade Monitor` workflow。
4. 第一次建議把 `reset_baseline` 設成 `true`，先建立 `monitor-state` 分支與 baseline。
5. baseline 建立成功後，再手動跑一次 `reset_baseline=false`，確認 workflow 能正常完成。
6. 之後讓排程每 5 分鐘自動執行即可。

## Sending A Status Report

如果你想手動查詢 `categories/takaratomy/beyblade` 目前狀態，可到 `Actions -> Funbox Beyblade Monitor -> Run workflow`：

1. branch 選 `main`
2. `send_status_report` 設成 `true`
3. 其他輸入維持 `false`
4. 執行 workflow

workflow 會把目前商品總數、現貨/缺貨/未知統計，以及前 10 項商品摘要送到 Telegram 與 Email。

## First Run Checklist

- Telegram bot 已能傳訊息給你的 chat
- SMTP 帳號可用 `STARTTLS` 與密碼登入
- repository 已啟用 GitHub Actions
- `monitor-state` 分支第一次建立後，能看到 `state/funbox-beyblade.json`
- `config/store_subscriptions.json` 已確認哪些門市要顯示在通知中
