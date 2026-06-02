# AI-LINE-Friend-Bot 🤖

這是一個具有「長期記憶」、「好感度系統」以及「網頁摘要」功能的群組/個人 LINE 聊天機器人。
底層基於 FastAPI 構建，並串接 DeepSeek API（或任何相容 OpenAI 格式的 LLM）作為 AI 核心，能夠像真實朋友一樣參與 LINE 群組對話。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## ✨ 核心特色

* **🧠 智能記憶與壓縮**：會自動記錄對話重點，當記憶庫過大時，會觸發 AI 進行智能精簡，保留關鍵事實。
* **💖 隱藏好感度系統**：根據使用者的對話態度，AI 會自動在後台微調對使用者的好感度（0~100分），進而影響回覆的語氣與親密度。
* **⏳ 防抖與批次處理 (Debounce)**：在群組中不會每句話都回，而是等待一段冷卻期，將連續訊息打包一次交給 AI 判斷是否需要回覆，大幅節省 API 成本並讓對話更自然。
* **🌐 自動網頁摘要**：當使用者在聊天中貼出網址時，Bot 會自動爬取網頁文字並產生摘要，作為 AI 回覆的輔助參考。
* **🎭 可自訂人設**：透過 `persona.txt` 輕鬆切換機器人的性格與回覆風格。
* **🖼️ 圖片理解**（可選）：支援 Vision 模型分析使用者傳送的圖片。
* **📦 Docker 支援**：提供 Dockerfile 與 docker-compose 方便部署。

## 🛠️ 安裝與執行

### 1. 安裝依賴套件

```bash
pip install -r requirements.txt
```

### 2. 設定環境變數

複製範例並填入你的金鑰：

```bash
cp env.example .env
```

編輯 `.env`：

```env
LINE_CHANNEL_ACCESS_TOKEN=你的LINE_CHANNEL_ACCESS_TOKEN
LINE_CHANNEL_SECRET=你的LINE_CHANNEL_SECRET
MAIN_LLM_API_KEY=你的LLM_API_KEY
MAIN_LLM_BASE_URL=https://api.deepseek.com/v1   # 或其他相容端點
VISION_API_KEY=你的VISION_KEY                 # 如需圖片功能
VISION_BASE_URL=...
```

### 3. 調整設定與人設（重要）

- `config.yaml`：調整機器人名稱、debounce 時間、回話頻率、功能開關等。
- `persona.txt`：定義機器人的個性、背景、說話風格（目前預設為「芯瑜」）。
- `rules.txt`：安全規則與管理員權限設定。

### 4. 啟動伺服器

```bash
uvicorn bot:app --host 0.0.0.0 --port 8000 --reload
```

在 LINE Developers Console 設定 Webhook URL 為 `https://你的域名/callback`（需 HTTPS，建議使用 ngrok 或雲端部署測試）。

### 使用 Docker 部署

```bash
docker compose -f docker.yaml up --build -d
```

## 📁 專案結構

```
.
├── bot.py              # 主要機器人邏輯（FastAPI + LINE SDK + 記憶/好感度）
├── config.yaml         # 系統設定
├── persona.txt         # 機器人人設
├── rules.txt           # 安全與管理規則
├── requirements.txt
├── env.example
├── ingest.py           # （可選）資料 ingest 腳本
├── Dockerfile
├── docker.yaml
├── archive/            # 舊版迭代檔案（bot2.py, bot3.py, old.py）
├── affinity.json       # 執行時產生（已 gitignore）
├── memory_db.json      # 執行時產生（已 gitignore）
├── docs_db.json        # 執行時產生（已 gitignore）
└── .github/            # Issue / PR 模板 + CI
```

**注意**：`pic/` 資料夾（先前 demo 圖片）與舊版 bot 檔案已移至 `archive/`，以保持主要程式碼乾淨。
```

## 🔧 進階設定

詳細說明請參考 `config.yaml` 內的註解。

- `reply_frequency_level`：控制機器人話痨程度。
- `enable_affinity` / `enable_vision` / `enable_rag`：功能開關。
- `summary_threshold`：記憶壓縮觸發門檻。

## 🤝 貢獻

歡迎貢獻！請先閱讀 [CONTRIBUTING.md](CONTRIBUTING.md)。

1. Fork 專案
2. 建立功能分支
3. 提交 Pull Request

我們使用 [Contributor Covenant](CODE_OF_CONDUCT.md) 作為行為準則。

## 📄 授權條款

本專案採用 [MIT License](LICENSE) 授權。

## 🙏 致謝

- LINE Bot SDK
- DeepSeek / OpenAI 相容 API
- 所有貢獻者與測試者

---

如果你喜歡這個專案，歡迎給個 ⭐ Star 並在 LINE 群組中試用！

如有問題，請開 Issue 或參考 [SECURITY.md](SECURITY.md) 回報安全問題。
