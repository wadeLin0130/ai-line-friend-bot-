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
BOT_NAME = config['system'].get('bot_name', 'Bot')

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

client_vision = None
if config['features'].get('enable_vision', False) or (config['features'].get('enable_rag', False) and config['features'].get('rag_type', 'local') == 'vector'):
    client_vision = OpenAI(
        api_key=os.getenv("VISION_API_KEY", "YOUR_VISION_KEY"),
        base_url=os.getenv("VISION_BASE_URL", "https://api.openai.com/v1")
    )

# ================================
# 3. 狀態儲存 (結構更新)
# ================================
history = defaultdict(lambda: deque(maxlen=40))
batch_messages = defaultdict(list)
debounce_timers = {}
source_types = {} 

# 💡【修復死結的關鍵】：這裡從 Lock 改成了可重入鎖 RLock！
state_lock = threading.RLock()
file_lock = threading.RLock()

AFFINITY_FILE = "affinity.json"
MEMORY_FILE = "memory_db.json"

# --- 輔助函式 ---
def load_json_db(filename):
    with file_lock:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                try: return json.load(f)
                except: return {} if filename == MEMORY_FILE else []
    return {} if filename in [AFFINITY_FILE, MEMORY_FILE] else []

def save_json_db(filename, data):
    with file_lock:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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

# --- 背景任務：動態結構化總結記憶 ---
def generate_memory_summary(unsum_msgs, source_id):
    try:
        if ENABLE_LOG:
            print(f"\n[⚙️ 背景任務] 開始為 {source_id} 總結 {len(unsum_msgs)} 則歷史對話...", flush=True)

        uids = set(m['user_id'] for m in unsum_msgs if m['user_id'] != 'BOT')
        db = load_json_db(MEMORY_FILE)
        if not isinstance(db, dict): db = {}

        existing_profiles = {}
        for uid in uids:
            existing_profiles[uid] = db.get(uid, {"name": "", "profile": {}})

        chat_log = "\n".join([f"{m['name']} (ID:{m['user_id']}): {m['text']}" for m in unsum_msgs])

        prompt = f"""
你是一個專業的 AI 記憶管理員。請閱讀以下近期的歷史對話，並動態更新參與者的「結構化個人畫像(User Profile)」。
目標：提取有用的長期事實（喜好、狀態、計畫、背景），忽略沒有意義的閒聊（如哈哈、早安）。
如果發現事實更新（例如：原本說要去日本，現在說剛回來），請覆寫舊記憶。

【現有畫像 (供你參考與更新)】
{json.dumps(existing_profiles, ensure_ascii=False, indent=2)}

【近期未總結對話紀錄】
{chat_log}

請輸出更新後的 JSON，必須以 user_id 作為 key，格式如下：
{{
  "user_id_1": {{
    "name": "使用者名字",
    "profile": {{
      "喜好": "...",
      "近期狀態": "..."
    }}
  }}
}}
如果沒有任何需要更新或記錄的，請回傳空物件 {{}}。
"""
        response = client_main.chat.completions.create(
            model=config['api']['main_model'],
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        
        # 💡【新增除錯Log】：把記憶總結產出的原始 JSON 吐出來看
        raw_response = response.choices[0].message.content.strip()
        if ENABLE_LOG:
            print(f"\n[📥 記憶總結 API 回應]\n{raw_response}\n", flush=True)
            
        updated_data = json.loads(raw_response)
        
        if updated_data:
            with file_lock:
                current_db = load_json_db(MEMORY_FILE)
                if not isinstance(current_db, dict): current_db = {}
                for uid, data in updated_data.items():
                    if uid not in current_db:
                        current_db[uid] = {"name": data.get("name", "Unknown"), "profile": {}}
                    current_db[uid]['profile'].update(data.get('profile', {}))
                    if data.get('name'):
                        current_db[uid]['name'] = data['name']
                with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(current_db, f, ensure_ascii=False, indent=2)
            if ENABLE_LOG:
                print(f"[💾 記憶總結完成] 已更新畫像: {list(updated_data.keys())}", flush=True)
        else:
            if ENABLE_LOG:
                print(f"[💾 記憶總結完成] 無重要事實，未更新。", flush=True)

    except Exception as e:
        print(f"[❌ 記憶總結失敗] {e}", flush=True)

# --- 核心邏輯 ---
def process_batch(source_id, reply_token):
    with state_lock:
        current_batch = list(batch_messages[source_id])
        batch_messages[source_id].clear()
        # 將新訊息加入歷史紀錄
        for msg in current_batch:
            history[source_id].append({
                "role": "user",
                "user_id": msg["user_id"],
                "name": msg["user_name"],
                "text": msg["text"],
                "summarized": False,
                "is_mentioned": msg.get("is_mentioned", False)
            })
        recent_history = list(history[source_id])
        s_type = source_types.get(source_id, 'user')

    if not current_batch: return

    # 1. 計算並處理未總結對話 (unSummary)
    unsum_msgs = [m for m in recent_history if not m.get('summarized')]
    summary_threshold = config['system'].get('summary_threshold', 10)
    
    if len(unsum_msgs) >= summary_threshold:
        # 先標記為已總結，避免重複觸發
        with state_lock:
            for m in history[source_id]:
                if not m.get('summarized'): m['summarized'] = True
        # 開啟背景執行緒進行總結，不阻塞回覆
        threading.Thread(target=generate_memory_summary, args=(unsum_msgs, source_id)).start()

    # 2. 判斷是否被硬性阻擋 (Tag-only 模式)
    is_group = s_type in ['group', 'room']
    is_mentioned = any(m.get('is_mentioned') for m in current_batch)
    hard_blocked = False
    
    if is_group and config['system'].get('group_tag_only', False):
        if not is_mentioned:
            hard_blocked = True
            if ENABLE_LOG:
                print(f"[🚫 被動阻擋] 群組開啟僅 Tag 模式，且無人呼叫，已跳過 LLM 請求。", flush=True)

    if hard_blocked:
        return 

    # 3. 準備給 LLM 的上下文
    latest_msgs_text = "\n".join([f"- {m['user_name']}: {m['text']}" for m in current_batch])
    
    recent_history_text = ""
    for msg in recent_history:
        status = "[已總結]" if msg.get('summarized') else "[未總結]"
        recent_history_text += f"{status} {msg['name']}: {msg['text']}\n"

    # --- 結構化記憶提取 ---
    memory_prompt = ""
    if config['features'].get('enable_rag', True):
        db = load_json_db(MEMORY_FILE)
        if isinstance(db, dict) and db:
            current_uids = set(m['user_id'] for m in current_batch)
            added_uids = set()
            memory_list = []
            
            # 優先提取當前講話者的畫像
            for uid in current_uids:
                if uid in db:
                    memory_list.append(f"- {db[uid].get('name', 'Unknown')}: {json.dumps(db[uid].get('profile',{}), ensure_ascii=False)}")
                    added_uids.add(uid)
            
            # 全域匹配關鍵字
            if config['features'].get('shared_memory', False):
                keywords = set(re.findall(r'\w{2,}', latest_msgs_text))
                for uid, data in db.items():
                    if uid in added_uids: continue
                    profile_str = json.dumps(data.get('profile',{}), ensure_ascii=False)
                    if any(kw in profile_str for kw in keywords):
                        memory_list.append(f"- [全域聯想] {data.get('name', 'Unknown')}: {profile_str}")
            
            if memory_list:
                memory_prompt = "【相關用戶畫像 (User Profiles)】\n" + "\n".join(memory_list) + "\n\n"

    # --- 好感度處理 ---
    affinity_prompt = ""
    if config['features'].get('enable_affinity', False):
        affinity_db = load_json_db(AFFINITY_FILE)
        current_speakers = {}
        for msg in current_batch:
            uid, uname = msg['user_id'], msg['user_name']
            if uid not in affinity_db:
                affinity_db[uid] = {"name": uname, "score": 50}
            current_speakers[uid] = affinity_db[uid]
        save_json_db(AFFINITY_FILE, affinity_db)
        
        aff_lines = [f"{info['name']} (ID:{uid}): {info['score']}分" for uid, info in current_speakers.items()]
        affinity_prompt = "【好感度系統】\n發言成員目前好感度：\n" + "\n".join(aff_lines) + "\n(請依據好感度微調語氣，但絕對不要在回覆中提到好感度機制或分數)\n\n"

    # --- 回話頻率設定 ---
    freq_level = config['system'].get('reply_frequency_level', 2)
    level_desc = {
        1: "等級 1 (極低頻率)：你現在處於潛水狀態。除非被明確點名、問問題或極度需要你協助，否則請填寫 should_reply: false。",
        2: "等級 2 (低頻率)：你是一個安靜的傾聽者。偶爾搭腔，不主動主導話題，多數時候請填寫 should_reply: false。",
        3: "等級 3 (中頻率)：你像普通朋友一樣參與討論。視話題熱度決定是否回覆。",
        4: "等級 4 (高頻率)：你是一個話嘮。積極接話，幾乎每句都回，盡量填寫 should_reply: true。"
    }.get(freq_level, "等級 2 (低頻率)")

    is_always_reply = (not is_group) and config['system'].get('always_reply_private', True)
    reply_instruction = f"1. 是否回覆(should_reply)：請根據你的頻率設定決定。\n當前設定為：{level_desc}\n" if not is_always_reply else "1. 是否回覆(should_reply)：請務必填 true，給出回應。\n"

    now = datetime.now(timezone(timedelta(hours=8)))
    persona_text = load_text_file("persona.txt", "你是一個普通的聊天機器人。")
    rules_text = load_text_file("rules.txt", "請保持禮貌。")
    
    system_prompt = (
        f"【角色設定 (Persona)】\n{persona_text}\n\n"
        f"【絕對遵守規則 (Rules)】\n{rules_text}\n\n"
        f"{memory_prompt}"
        f"{affinity_prompt}"
        f"【當前時間】{now.strftime('%Y-%m-%d %H:%M')}\n\n"
        "【決策與輸出格式要求（嚴格 JSON）】\n"
        f"{reply_instruction}"
        "2. message：若決定回覆，請填寫內容。否則留空。\n"
        "3. affinity_adjustments：(可選) 根據使用者當前的語氣，給予 -2 到 +2 分的好感度微調，若無特別感覺請給 0。\n"
        "格式：\n"
        '{"should_reply": true/false, "message": "回覆內容", "affinity_adjustments": {"ID": 0}}\n'
    )

    user_content = f"【歷史對話 (含標記)】\n{recent_history_text}\n\n【本次最新收到】\n{latest_msgs_text}\n\n請給出 JSON："

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

        # 處理好感度更新並打印日誌
        if decision.get("affinity_adjustments") and config['features'].get('enable_affinity', False):
            affinity_db = load_json_db(AFFINITY_FILE)
            updated_affinity = False
            for uid, adj in decision["affinity_adjustments"].items():
                if uid in affinity_db and isinstance(adj, (int, float)) and adj != 0:
                    old_score = affinity_db[uid]["score"]
                    new_score = max(0, min(100, old_score + max(-2, min(2, int(adj)))))
                    affinity_db[uid]["score"] = new_score
                    updated_affinity = True
                    if ENABLE_LOG:
                        print(f"[💖 好感度變更] {affinity_db[uid]['name']}: {old_score} -> {new_score}", flush=True)
            if updated_affinity:
                save_json_db(AFFINITY_FILE, affinity_db)

        if should_reply and message.strip():
            messages_to_send = [TextSendMessage(text=line) for line in message.split('\n') if line.strip()][:2]
            try:
                line_bot_api.reply_message(reply_token, messages_to_send)
                with state_lock:
                    history[source_id].append({
                        "role": "bot",
                        "user_id": "BOT",
                        "name": BOT_NAME,
                        "text": f"({' / '.join([m.text for m in messages_to_send])})",
                        "summarized": False,
                        "is_mentioned": False
                    })
            except Exception as e:
                print(f"[❌ Reply 錯誤] {e}", flush=True)

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
    is_mentioned = False
    if BOT_NAME.lower() in user_text.lower() or f"@{BOT_NAME}" in user_text:
        is_mentioned = True
        
    with state_lock:
        source_types[source_id] = source_type
        batch_messages[source_id].append({
            "user_id": user_id, 
            "user_name": user_name, 
            "text": user_text,
            "is_mentioned": is_mentioned
        })
        
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
    if ENABLE_LOG:
        print(f"\n[📥 收到文字] {user_name} ({source_type})", flush=True)
    handle_incoming(event, user_text, user_id, source_id, source_type, user_name)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=8000, reload=True)
