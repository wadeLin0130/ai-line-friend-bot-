FROM python:3.10-slim

# 設定工作目錄
WORKDIR /app

# 安裝系統依賴 (ChromaDB 可能需要一些編譯工具)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 複製並安裝 Python 依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案原始碼
COPY . .

# 暴露 FastAPI 埠號
EXPOSE 8000

# 啟動伺服器
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8000"]