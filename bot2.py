import os
import sys
import time
import json
import re
import math
import base64
import threading
import yaml
import requests
import urllib3
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================================
# 0. 檢查是否啟用日誌模式 (--log)
# ================================
if '--log' in sys.argv:
    os.environ['ENABLE_LOG'] = '1'
ENABLE_LOG = os.environ.get('ENABLE_LOG') == '1'

if ENABLE_LOG:
    print("====================================")
    print("🚀 已啟用詳細日誌模式 (--log)")
    print("====================================")

# ================================
# 1. 載入設定與純文字檔案
# ================================
def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_text_file(filename, default_text):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return default_text

config = load_config()

# ================================
# 2. 初始化 API 客戶端
# ================================
app = FastAPI()

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_LINE_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET', 'YOUR_LINE_SECRET'))

client_main = OpenAI(
    api_key=os.getenv("MAIN_LLM_API_KEY", "YOUR_API_KEY"),
    base_url=os.getenv("MAIN_LLM_BASE_URL", "https://api.openai.com/v1")
)

# 圖片視覺大腦 (只有開啟視覺或 RAG 向量模式時才初始化)
client_vision = None
if config['features'].get('enable_vision', False) or (config['features'].get('enable_rag', False) and config['features'].get('rag_type', 'local') == 'vector'):
    client_vision = OpenAI(
        api_key=os.getenv("VISION_API_KEY", "YOUR_VISION_KEY"),
        base_url=os.getenv("VISION_BASE_URL", "https://api.openai.com/v1")
    )

# ================================
# 3. 狀態儲存
# ================================
history = defaultdict(lambda: deque(maxlen=20))
batch_messages = defaultdict(list)
debounce_timers = {}
source_types = {} 

state_lock = threading.Lock()
file_lock = threading.Lock()
AFFINITY_FILE = "affinity.json"

# --- 輔助函式 ---
def fetch_url_content(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=3, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for script in soup(["script", "style"]):
            script.extract()
        return soup.get_text(separator=' ', strip=True)[:800]
    except Exception:
        return None

def load_json_db(filename):
    with file_lock:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                try: return json.load(f)
                except: return []
    return [] if filename != AFFINITY_FILE else {}

def save_json_db(filename, data):
    with file_lock:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# --- RAG 演算法區 ---

# 1. 向量模型生成
def get_embedding(text):
    if client_vision is None:
        print("[⚠️ 警告] 視覺/向量 API 未啟用或未設定 API Key。")
        return []
    try:
        model_name = config['api'].get('embedding_model', 'text-embedding-3-small')
        res = client_vision.embeddings.create(input=text, model=model_name)
        return res.data[0].embedding
    except Exception as e:
        print(f"[❌ Embedding 錯誤] {e}")
        return []

def cosine_similarity(v1, v2):
    dot_product = sum(a * b for a, b in zip(v1, v2))
    magnitude1 = math.sqrt(sum(a * a for a in v1))
    magnitude2 = math.sqrt(sum(b * b for b in v2))
    if magnitude1 == 0 or magnitude2 == 0: return 0.0
    return dot_product / (magnitude1 * magnitude2)

def search_similar_vector(query_text, db_list, top_k=3, source_id=None):
    if not db_list: return ""
    query_emb = get_embedding(query_text)
    if not query_emb: return ""
    
    results = []
    for item in db_list:
        if source_id and item.get("source_id") != source_id: continue
        if "embedding" not in item: continue
        sim = cosine_similarity(query_emb, item["embedding"])
        results.append((sim, item["text"]))
        
    results.sort(key=lambda x: x[0], reverse=True)
    return "\n".join([r[1] for r in results[:top_k] if r[0] > 0.3])

# 2. 純本地 N-gram 字詞比對 (免 API)
def ngram_similarity(query, text, n=2):
    def get_ngrams(s):
        s = s.replace(" ", "")
        if len(s) < n: return set([s])
        return set(s[i:i+n] for i in range(len(s)-n+1))
    q_grams = get_ngrams(query)
    t_grams = get_ngrams(text)
    if not q_grams or not t_grams: return 0.0
    intersection = q_grams.intersection(t_grams)
    union_len = len(q_grams) + len(t_grams) - len(intersection)
    return len(intersection) / union_len if union_len > 0 else 0.0

def search_similar_local(query_text, db_list, top_k=3, source_id=None):
    if not db_list: return ""
    results = []
    for item in db_list:
        if source_id and item.get("source_id") != source_id: continue
        sim = ngram_similarity(query_text, item["text"])
        results.append((sim, item["text"]))
    results.sort(key=lambda x: x[0], reverse=True)
    return "\n".join([r[1] for r in results[:top_k] if r[0] > 0.05])

# 3. 統一記憶寫入處理
def save_memory(source_id, memory_text):
    if not config['features']['enable_rag']: return
    rag_type = config['features'].get('rag_type', 'local')
    
    try:
        mem_data = {
            "source_id": source_id, 
            "time": str(datetime.now()), 
            "text": memory_text
        }
        
        if rag_type == "vector":
            emb = get_embedding(memory_text)
            if emb:
                mem_data["embedding"] = emb
            else:
                print("[⚠️ 警告] 記憶向量化失敗，將不儲存此記憶。")
                return
                
        db = load_json_db('memory_db.json')
        if not isinstance(db, list): db = []
        db.append(mem_data)
        save_json_db('memory_db.json', db)
        print(f"[💾 記憶已更新 ({rag_type})] {memory_text}", flush=True)
    except Exception as e:
        print(f"[❌ 記憶寫入失敗] {e}", flush=True)

def send_reply(source_id, reply_token, messages, text_to_save):
    try:
        line_bot_api.reply_message(reply_token, messages)
        print(f"[✅ 成功發送回覆] 已回應 {source_id}", flush=True)
        with state_lock:
            history[source_id].append(text_to_save)
    except Exception as e:
        print(f"[❌ Reply 錯誤] {e}", flush=True)

def describe_image(image_content):
    if client_vision is None: return "視覺功能未啟用。"
    try:
        base64_image = base64.b64encode(image_content).decode('utf-8')
        response = client_vision.chat.completions.create(
            model=config['api']['vision_model'],
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": "請簡短描述這張圖片的內容。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]}
            ],
            max_tokens=200
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"無法解析圖片: {e}"

# --- 核心邏輯 ---
def process_batch(source_id, reply_token):
    with state_lock:
        current_batch = list(batch_messages[source_id])
        batch_messages[source_id].clear()
        recent_history = list(history[source_id])
        s_type = source_types.get(source_id, 'user')

    if not current_batch: return

    latest_msgs_text = "\n".join([f"- {msg['user_name']}: {msg['text']}" for msg in current_batch])
    recent_history_text = "\n".join([f"- {msg}" for msg in recent_history])
    
    # --- RAG 檢索 ---
    rag_prompt = ""
    if config['features']['enable_rag']:
        docs_db = load_json_db('docs_db.json')
        memory_db = load_json_db('memory_db.json')
        rag_type = config['features'].get('rag_type', 'local')
        
        if rag_type == "vector":
            rag_docs = search_similar_vector(latest_msgs_text, docs_db if isinstance(docs_db, list) else [], top_k=3)
            rag_mems = search_similar_vector(latest_msgs_text, memory_db if isinstance(memory_db, list) else [], top_k=3, source_id=source_id)
        else:
            rag_docs = search_similar_local(latest_msgs_text, docs_db if isinstance(docs_db, list) else [], top_k=3)
            rag_mems = search_similar_local(latest_msgs_text, memory_db if isinstance(memory_db, list) else [], top_k=3, source_id=source_id)
        
        if rag_docs or rag_mems:
            rag_prompt = f"【系統檢索資料庫(RAG)提供之輔助資訊（模式: {rag_type}）】\n"
            if rag_docs: rag_prompt += f"相關客服/文件資料：\n{rag_docs}\n\n"
            if rag_mems: rag_prompt += f"相關歷史記憶：\n{rag_mems}\n\n"

    # --- 好感度處理 ---
    affinity_prompt = ""
    if config['features']['enable_affinity']:
        affinity_db = load_json_db(AFFINITY_FILE)
        current_speakers = {}
        for msg in current_batch:
            uid, uname = msg['user_id'], msg['user_name']
            if uid not in affinity_db:
                affinity_db[uid] = {"name": uname, "score": 50}
            current_speakers[uid] = affinity_db[uid]
        save_json_db(AFFINITY_FILE, affinity_db)
        
        aff_lines = [f"{info['name']} (ID:{uid}): {info['score']}分" for uid, info in current_speakers.items()]
        affinity_prompt = "【好感度系統】\n發言成員好感度：\n" + "\n".join(aff_lines) + "\n(請依據好感度調整語氣，但絕對不要在回覆中提到好感度機制或分數)\n\n"

    # --- 強制回覆判定 ---
    is_always_reply = False
    if s_type == 'user' and config['system']['always_reply_private']: is_always_reply = True
    if s_type in ['group', 'room'] and config['system']['always_reply_group']: is_always_reply = True

    reply_instruction = (
        "1. 是否回覆(should_reply)：因為目前設定要求【強制有問必答】，此欄位請務必填 true，並給出適當回應。\n"
        if is_always_reply else 
        "1. 是否回覆(should_reply)：觀察最新訊息決定是否接話. 打招呼或話題結束選 false。\n"
    )

    now = datetime.now(timezone(timedelta(hours=8)))
    persona_text = load_text_file("persona.txt", "你是一個真正的人類，懂得察言觀色，能記得大部分有可能需要記得的聊天內容。")
    rules_text = load_text_file("rules.txt", "請保持禮貌。")
    
    system_prompt = (
        f"【角色設定 (Persona)】\n{persona_text}\n\n"
        f"【絕對遵守規則 (Rules)】\n{rules_text}\n\n"
        f"{rag_prompt}"
        f"【當前時間】{now.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"{affinity_prompt}"
        "【決策與輸出格式要求（嚴格 JSON）】\n"
        f"{reply_instruction}"
        "2. 記憶更新(update_memory)：是否需要提取事實作為記憶？盡量記得每一句話\n"
        "3. 新記憶(new_memory)：提取事實(20字內)，否則留空。\n"
        "4. 好感度變更(affinity_adjustments)：根據語氣給予 -2 到 +2 分微調，無感覺給0，判斷極度惡劣者-30。\n"
        "格式：\n"
        '{"should_reply": true/false, "message": "回覆內容", "update_memory": true/false, "new_memory": "...", "affinity_adjustments": {"ID": 0}}\n'
    )

    user_content = f"【歷史】\n{recent_history_text}\n\n【最新】\n{latest_msgs_text}\n\n請給出 JSON："

    if ENABLE_LOG:
        print("\n" + "▼"*50, flush=True)
        print("[🔍 發送給 LLM 的請求內容]", flush=True)
        print(f"--- 🟢 System Prompt ---\n{system_prompt}", flush=True)
        print(f"--- 🔵 User Content ---\n{user_content}", flush=True)
        print("▲"*50 + "\n", flush=True)

    try:
        response = client_main.chat.completions.create(
            model=config['api']['main_model'],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"}
        )
        
        raw_response = response.choices[0].message.content.strip()

        if ENABLE_LOG:
            print("\n" + "▼"*50, flush=True)
            print("[📥 接收到 LLM 的原始回應]", flush=True)
            print(raw_response, flush=True)
            print("▲"*50 + "\n", flush=True)

        decision = json.loads(raw_response)
        
        should_reply = True if is_always_reply else decision.get("should_reply", False)
        message = decision.get("message", "")

        if decision.get("affinity_adjustments") and config['features']['enable_affinity']:
            affinity_db = load_json_db(AFFINITY_FILE)
            for uid, adj in decision["affinity_adjustments"].items():
                if uid in affinity_db and isinstance(adj, int):
                    affinity_db[uid]["score"] = max(0, min(100, affinity_db[uid]["score"] + max(-2, min(2, adj))))
            save_json_db(AFFINITY_FILE, affinity_db)

        if decision.get("update_memory") and decision.get("new_memory"):
            threading.Thread(target=save_memory, args=(source_id, decision["new_memory"])).start()

        if should_reply and message.strip():
            messages_to_send = [TextSendMessage(text=line) for line in message.split('\n') if line.strip()][:2]
            send_reply(source_id, reply_token, messages_to_send, f"(你): {' / '.join([m.text for m in messages_to_send])}")

    except Exception as e:
        print(f"[❌ AI 決策錯誤] {e}", flush=True)

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get('X-Line-Signature')
    body = await request.body()
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400)
    return "OK"

def handle_incoming(event, user_text, user_id, source_id, source_type, user_name):
    with state_lock:
        source_types[source_id] = source_type
        history[source_id].append(f"{user_name}: {user_text}")
        batch_messages[source_id].append({"user_id": user_id, "user_name": user_name, "text": user_text})
        
        if source_id in debounce_timers:
            debounce_timers[source_id].cancel()
            
        timer = threading.Timer(config['system']['debounce_seconds'], process_batch, args=(source_id, event.reply_token))
        debounce_timers[source_id] = timer
        timer.start()

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    source_type = event.source.type
    source_id = getattr(event.source, f"{source_type}_id", user_id)
    user_name = "某位朋友"
    try:
        if source_type == 'group': user_name = line_bot_api.get_group_member_profile(source_id, user_id).display_name
        elif source_type == 'room': user_name = line_bot_api.get_room_member_profile(source_id, user_id).display_name
        else: user_name = line_bot_api.get_profile(user_id).display_name
    except: pass
    
    user_text = event.message.text
    for url in re.findall(r'(https?://[^\s]+)', user_text):
        content = fetch_url_content(url)
        if content: user_text += f"\n【網頁摘要】{content}..."
        
    print(f"\n[📥 收到文字] {user_name} ({source_type})", flush=True)
    handle_incoming(event, user_text, user_id, source_id, source_type, user_name)

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    if not config['features'].get('enable_vision', False) or client_vision is None: 
        print("[🖼️ 收到圖片] 但已關閉視覺功能，忽略此訊息。")
        return
        
    user_id = event.source.user_id
    source_type = event.source.type
    source_id = getattr(event.source, f"{source_type}_id", user_id)
    user_name = "某位朋友"
    
    message_content = line_bot_api.get_message_content(event.message.id)
    image_bytes = b""
    for chunk in message_content.iter_content(): image_bytes += chunk
        
    desc = describe_image(image_bytes)
    user_text = f"[傳送了一張圖片] 系統視覺解析結果：{desc}"
    handle_incoming(event, user_text, user_id, source_id, source_type, user_name)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=8000, reload=True)
