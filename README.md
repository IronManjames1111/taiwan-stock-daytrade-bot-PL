# Taiwan Stock Daytrade AI Bot (台股當沖 AI 自動回測系統)

基於 Google Gemini 2.0 與富果行情的全自動台股當沖回測系統，使用 GitHub Actions 雲端定時排程運行。

## 🌟 核心功能
1. **09:05 自動選股**：免費自 Yahoo 股市抓取盤中成交量排行前 5 檔活絡股（過濾 ETF、特別股、權證）。
2. **每 10 分鐘例行分析**：調用技術指標（VWAP、Wilder ATR、均線、多週期趨勢）並由 Gemini 判定進出場價與停利停損點。
3. **交易紀錄與收盤結算**：出現 BUY / SHORT 訊號時自動存檔，13:25 自動回放當日 1 分K線結算真實賺賠與勝率。
4. **自動提交報表**：每日收盤後自動產出 CSV 報表並提交保存於 `history_records/`。

## 🔐 GitHub Secrets 設定
請至 Repository -> **Settings** -> **Secrets and variables** -> **Actions** 新增以下兩筆：
- `FUGLE_API_KEY`: 富果 API 金鑰
- `GEMINI_API_KEY`: Google Gemini API 金鑰

## ⚙️ 權限設定
至 **Settings** -> **Actions** -> **General** -> **Workflow permissions** 勾選 **Read and write permissions**。
