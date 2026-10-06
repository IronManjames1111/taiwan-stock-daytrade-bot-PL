# Taiwan Stock Daytrade Technical Strategy (台股當沖技術策略系統)

以富果 1 分K與本地技術指標策略執行台股當沖訊號、停利停損追蹤與日結，使用 GitHub Actions 雲端排程運行，不呼叫生成式 AI。

## 🌟 核心功能
1. **09:05 自動選股**：從 Yahoo 成交量排行抓取候選股，排除 ETF、權證及漲停標的，並取成交量前 8 檔監控。
2. **策略分析（09:05～13:00）**：八個策略並行判斷：VWAP 動能突破、EMA 趨勢回檔、RSI 布林反轉、MACD 量能確認、開盤區間突破、EMA 快慢線動能、KD 順勢交叉、區間高低突破。策略依「家族」分組計票（同家族只算 1 票），預設至少 2 個獨立家族同向才進場；逆勢（RSI 反轉）與順勢方向相反時觀望。訊號只使用已收完的 1 分K。
3. **每日資金池與持倉監控**：每天開盤給固定本金（預設 100 萬，`daily_capital` 或 repo Variable `DAILY_CAPITAL`）。進場先扣除佔用資金，出場後「本金＋淨損益」回補；同輪多檔訊號由程式依票數與淨賺賠比分配資金，不足則縮減張數或略過。以 ATR 計算停損與停利，每輪從「進場後的第一根K」逐根回放比對高低價；同根K線同時觸及時保守以停損出場，跳空越過停損以開盤價成交。13:00 後停止新進場，持倉仍持續監控，13:25 以最新K線強制平倉。
   進場前會檢查：每檔每日次數（預設 5 次）與停損後冷卻（預設 5 分鐘）、處置股／注意股／可否當沖／漲跌停、固定風險部位大小、**扣除手續費與證交稅後的淨賺賠比**。
   儀表板的「💰 資金變化」卡片顯示當日資金曲線、資金佔用與每日資金變化圖。
4. **日報與策略績效**：記錄每筆進場策略、價位與淨損益，收盤產出交易 CSV、每日快照；網頁列出目前持倉、各策略進場數、勝率及累積淨損益。
5. **跨日資料自動搶救**：若前一天沒有成功完成收盤結算，隔天第一次執行時會自動偵測並補跑該日結算。

## 🎛️ 網頁策略設定
看板的「策略與風控設定」可以開關策略、設定最低同向票數及領先票數、量能倍數、最低K線數、停損/停利 ATR 倍數與最大持倉數。設定會暫存在目前瀏覽器，也可以下載 `strategy_settings.json`。GitHub Pages 是靜態頁面，不能直接改寫 repository；要套用到雲端，請將下載檔放在 repository 根目錄並提交，之後 Actions 執行便會讀取新設定。

預設需 2 個獨立家族同向才進場（原為 1 票）。`RISK_MODE`（auto / aggressive / conservative / relaxed）現在會真正覆蓋部分門檻，`auto` 完全使用 `strategy_settings.json`。新增的成本、風控與資金參數（`daily_capital`、`min_net_rr`、`min_target_cost_multiple`、`sizing_mode`、`risk_per_trade`、`max_entries_per_symbol`、`cooldown_minutes` 等）直接編輯 `strategy_settings.json`，說明見「更新說明_v23.md」與「優化說明_v22.md」。

## 🔐 GitHub Secrets 設定
請至 Repository -> **Settings** -> **Secrets and variables** -> **Actions** 新增：
- `FUGLE_API_KEY`: 富果 API 金鑰

## 💰 手續費與證交稅設定（可選）
`config.json`（手機端設定）不會推上雲端，雲端排程改用 **Repository Variables** 覆蓋，
不設定則使用保守預設值（無折扣、當沖稅率）。至 **Settings** -> **Secrets and
variables** -> **Actions** -> **Variables** 新增：
- `BROKER_DISCOUNT`：券商手續費折扣，範圍 0.1～1.0（例如 6 折填 `0.6`，無折扣填 `1.0`）。
- `IS_DAY_TRADE_TAX`：是否適用當沖證交稅減半（0.15%），填 `true` 或 `false`；不填預設 `true`。

## ⚙️ 權限設定
至 **Settings** -> **Actions** -> **General** -> **Workflow permissions** 勾選 **Read and write permissions**。

## 📅 歷史紀錄與查看日期
網頁右上角「查看日期」下拉選單，選項來自 `history_records/index.json`，
會依 `history_records/` 資料夾內實際存在的 `analysis_YYYY-MM-DD.json`
快照檔案動態重建，每天 13:25 收盤結算成功後自動新增當天一筆。若某天
結算沒有準時觸發成功，系統會在隔天第一次執行時自動偵測並補跑該日結算，
執行紀錄（Actions log）會出現「搶救性收盤結算已完成」的訊息，之後該日
即可在下拉選單正常查看。

## ⏱️ 雲端觸發方式（兩種擇一）
- **single（預設）**：cron-job.org 週一～週五 08:50～13:30 每 5 分鐘觸發，每次只跑一輪。
- **loop（建議）**：cron-job.org 只在週一～週五 08:50 觸發**一次**，呼叫 API 時帶 `{"ref":"main","inputs":{"run_mode":"loop"}}`（或在 repo Variables 新增 `RUN_MODE=loop`）。Job 內每 60 秒跑一輪直到 13:32，並每 5 分鐘推送一次網站。

Workflow 一開始會先檢查「是否交易日、是否在交易時段」，不是就直接結束。國定假日取自證交所 OpenAPI；颱風假等臨時休市請手動寫入 `market_holidays.json` 的 `closed`。手動測試（夜間／假日）請在 Run workflow 勾選 `force_run`。

每輪最多對 8 檔股票各取一次日內K線（4 條執行緒並行），遠低於富果基本方案日內行情 60 次/分鐘上限。參考[富果行情方案及價格](https://developer.fugle.tw/docs/pricing/)。

## 🧪 測試
```
pip install -r requirements-dev.txt
python -m pytest tests -q
```
涵蓋逐根出場、成本檢查、資金池扣款與回補、資金分配、重複進場、結算清單、渲染邊界，以及資金圖表（需要 node，沒有會自動略過）。推送程式碼到 GitHub 時會自動執行（`.github/workflows/tests.yml`）。
