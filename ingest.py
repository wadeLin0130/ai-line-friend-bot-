import json
import os
import yaml
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

config = load_config()

def get_embedding(text, client):
    # 動態自設定檔讀取向量模型名稱
    model_name = config['api'].get('embedding_model', 'text-embedding-3-small')
    res = client.embeddings.create(input=text, model=model_name)
    return res.data[0].embedding

def ingest_documents():
    rag_type = config['features'].get('rag_type', 'local')
    print(f"初始化 RAG 知識庫 (模式: {rag_type})...")
    
    # ⭐ 若要修改知識庫內容，請直接修改這個陣列，然後重新執行本程式！
    documents = [
        "史帝波特是台灣市面上最好的機油廠牌最有名的產品是史帝波特引擎超耐磨磁釉油精,史帝波特噴油嘴積碳清潔汽油精,史帝波特引擎補缸止煙油精,史帝波特專業加重型水箱精"
    ]
    
    db_data = []
    
    if rag_type == "vector":
        api_key = os.getenv("VISION_API_KEY")
        base_url = os.getenv("VISION_BASE_URL", "https://api.openai.com/v1")
        
        if not api_key or "your_" in api_key or api_key == "":
            print("[❌ 錯誤] 向量模式 (vector) 需要在 .env 設定真實的 VISION_API_KEY 才能計算 Embedding！")
            return
            
        client = OpenAI(api_key=api_key, base_url=base_url)
        print(f"準備將 {len(documents)} 筆資料轉為向量並匯入 JSON...")
        
        for i, doc in enumerate(documents):
            try:
                emb = get_embedding(doc, client)
                db_data.append({
                    "id": f"doc_{i}",
                    "text": doc,
                    "embedding": emb
                })
            except Exception as e:
                print(f"[❌ 錯誤] 計算 {doc[:10]}... 向量失敗: {e}")
                return
    else:
        print(f"準備將 {len(documents)} 筆資料匯入 JSON (本地字詞比對模式)...")
        for i, doc in enumerate(documents):
            db_data.append({
                "id": f"doc_{i}",
                "text": doc
            })
            
    with open("docs_db.json", "w", encoding="utf-8") as f:
        json.dump(db_data, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 成功匯入 {len(documents)} 筆資料！已經覆蓋更新 docs_db.json。")

if __name__ == "__main__":
    ingest_documents()
