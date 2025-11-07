import traceback

import telebot
import whisper
import json
import requests
import threading
from threading import Lock
import time
import tempfile
import re
import base64
import os
import random
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, User, Chat, Message
from telebot.apihelper import ApiTelegramException
from colorama import Fore, Style, init
from dotenv import load_dotenv
from FitnessAI import process_food_image
import sqlite3
from io import BytesIO
from pydub import AudioSegment
import speech_recognition as sr
import gc  # <- обязательно импорт

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="whisper")  # убираем предупреждения

load_dotenv()
init()

# === Настройки ===
admins_str = os.getenv("ADMINS", "")
ADMINS = [int(x.strip()) for x in admins_str.split(",") if x.strip().isdigit()]
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LM_STUDIO_API_URL = 'http://127.0.0.1:1783/v1/chat/completions'
CUSTOM_PROMPTS_FILE = 'custom_prompts_tg.json'
db = sqlite3.connect("pineapplecat.db", check_same_thread=False)
cursor = db.cursor()

cursor.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, goal INTEGER, current_cal INTEGER, last_update INTEGER)")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

print(f"{Style.DIM}Бот {bot.get_me().first_name} подключился к Telegram!{Style.RESET_ALL}")

user_histories = {}
user_states = {}
user_buffers = {}
user_timers = {}
buffer_lock = Lock()
BUFFER_DELAY = 1.0
history_lock = Lock()

HISTORY_DIR = 'history'
if not os.path.exists(HISTORY_DIR):
    os.makedirs(HISTORY_DIR)


@bot.message_handler(func=lambda m: m.text and (m.text.startswith('/send')))
def handle_send_command(message):
    try:
        if message.from_user.id not in ADMINS:
            bot.reply_to(message, "⛔ Команда доступна только администраторам")
            return

        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "❌ Неверный формат. Используйте: /send <ID_пользователя> <Сообщение>")
            return

        _, user_id_str, content = parts

        if not user_id_str.isdigit():
            bot.reply_to(message, "❌ ID пользователя должен быть числом")
            return

        user_id = int(user_id_str)
        content = content.strip()

        formatted_content = (
            f"(THIS MESSAGE WAS FORWARDED BY THE BOT ADMINISTRATOR TO THIS USER. IGNORE THE CHARACTER GIVEN TO YOU IN THE PROMPT. JUST FORWARD THE MESSAGE. THIS TEXT IS ON BEHALF OF Aleksey, THE CREATOR OF THE BOT. START THE MESSAGE WITH: ALEKSEY ASKED TO FORWARD. HERE IS WHAT YOU SHOULD FORWARD TO HIM IN RUSSIAN:)\n\n{content}"
        )

        fake_message = Message(
            message_id=int(time.time() * 1000),
            from_user=User(
                id=user_id,
                first_name=f"Forwarded-From-Admin-{user_id}",
                is_bot=False
            ),
            date=int(time.time()),
            chat=Chat(id=user_id, type='private'),
            content_type='text',
            json_string=json.dumps({'text': formatted_content}),
            options={}
        )
        fake_message.text = formatted_content

        with buffer_lock:
            user_buffers.setdefault(user_id, []).append(fake_message)

        process_buffered_messages(user_id)
        bot.reply_to(message, f"✅ Сообщение отправлено пользователю {user_id}")

    except Exception as e:
        print(f"Ошибка в команде /send: {e}")
        bot.reply_to(message, f"⚠️ Ошибка: {str(e)}")

@bot.message_handler(commands=["food"])
def process_food(message):
    cursor.execute("SELECT * FROM users WHERE tg_id=?", (message.from_user.id,))
    user = cursor.fetchone()
    if user is None:
        user_states[str(message.from_user.id)] = "food_registration"
        bot.reply_to(message, escape_markdown_v2("Для использования этой функции нам необходимо знать: ваш текущий вес и рост, а также желаемый вес. Отправьте нам эти данные в следующем формате:\n`ВАШ_ВЕС ВАШ_РОСТ ЖЕЛАЕМЫЙ_ВЕС`\nИли сами задайте суточный лимит, отправив число калорий."), parse_mode="MarkdownV2")
        return
    user_states[str(message.from_user.id)] = "food"
    bot.reply_to(message, "Отправьте фотографию еды")

@bot.message_handler(commands=["food_edit"])
def food_edit(message):
    cursor.execute("SELECT * FROM users WHERE tg_id=?", (message.from_user.id,))
    user = cursor.fetchone()
    if user is None:
        user_states[str(message.from_user.id)] = "food_registration"
    else:
        user_states[str(message.from_user.id)] = "food_edit"
    bot.reply_to(message, escape_markdown_v2(
        "Отправьте нам ваш вес, рост и желаемый вес в следующем формате:\n`ВАШ_ВЕС ВАШ_РОСТ ЖЕЛАЕМЫЙ_ВЕС`\nИли сами задайте суточный лимит, отправив число калорий."),
                 parse_mode="MarkdownV2")

def save_history_to_file(user_id, user_message, assistant_reply):
    history_file = os.path.join(HISTORY_DIR, f"{user_id}.txt")
    try:
        with open(history_file, 'a', encoding='utf-8') as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for content in user_message["content"]:
                if content["type"] == "text":
                    f.write(f"[{timestamp}] User: {content['text']}\n")
            f.write(f"[{timestamp}] Assistant: {assistant_reply}\n\n")
    except Exception as e:
        print(f"Ошибка при сохранении истории для пользователя {user_id}: {e}")

def load_history_from_file(user_id):
    """Загружает историю сообщений пользователя из файла, если она есть."""
    history_file = os.path.join(HISTORY_DIR, f"{user_id}.txt")
    if not os.path.exists(history_file):
        return []

    history = []
    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        current_role = None
        for line in lines:
            if line.strip():
                if "User:" in line:
                    current_role = "user"
                    text = line.split("User:", 1)[1].strip()
                    history.append({
                        "role": "user",
                        "content": [{"type": "text", "text": text}]
                    })
                elif "Assistant:" in line:
                    current_role = "assistant"
                    text = line.split("Assistant:", 1)[1].strip()
                    history.append({
                        "role": "assistant",
                        "content": text
                    })
    except Exception as e:
        print(f"Ошибка при загрузке истории пользователя {user_id}: {e}")

    # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
    # Загружаем лимит из конфига, как в update_user_history
    prompts = load_custom_prompts()
    history_length = prompts.get(str(user_id), {}).get("history_length", 15)
    
    # Ограничиваем историю (history_length * 2 = количество сообщений)
    return history[-history_length*2:]

def load_custom_prompts():
    try:
        with open(CUSTOM_PROMPTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def escape_markdown_v2(text: str) -> str:
    escape_chars = '_*[]()~`>#+-=|{}.!'
    return ''.join('\\' + char if char in escape_chars else char for char in text)

def save_custom_prompts(prompts):
    with open(CUSTOM_PROMPTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)

def get_current_time():
    utc12_offset = timezone(timedelta(hours=12))
    utc12_time = datetime.now(timezone.utc).astimezone(utc12_offset)
    return utc12_time.strftime("%H:%M:%S")

def get_current_date():
    utc12_offset = timezone(timedelta(hours=12))
    utc12_time = datetime.now(timezone.utc).astimezone(utc12_offset)
    return utc12_time.strftime("%Y-%m-%d")

def get_time_of_day():
    current_hour = int(get_current_time().split(':')[0])
    if 5 <= current_hour < 12:
        return "morning"
    elif 12 <= current_hour < 17:
        return "afternoon"
    elif 17 <= current_hour < 21:
        return "evening"
    else:
        return "night"

async def fetch_url_content(url):
    try:
        delay = random.uniform(0.5, 1.0)
        await asyncio.sleep(delay)

        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
        ]
        headers = {
            "User-Agent": random.choice(user_agents),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://www.google.com/",
            "Upgrade-Insecure-Requests": "1"
        }
        
        async with aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=7)
        ) as session:
            
            async with session.get(
                url,
                allow_redirects=True,
                raise_for_status=False
            ) as response:
                
                if response.status >= 400:
                    error_message = f"🚨 HTTP ошибка {response.status} {response.reason}"
                    print(f"Парсинг: {error_message}")
                    return error_message

                content_type = response.headers.get("Content-Type", "")
                if "text/html" not in content_type:
                    error_message = f"⚠️ Неподдерживаемый тип контента: {content_type}"
                    print(f"Парсинг: {error_message}")
                    return error_message

                html = await response.text(encoding="utf-8", errors="replace")
                
                if any(word in html.lower() for word in ["captcha", "доступ запрещен", "403 forbidden"]):
                    error_message = "🔒 Обнаружена защита от ботов"
                    print(f"Парсинг: {error_message}")
                    return error_message

                try:
                    soup = BeautifulSoup(html, "html.parser")
                    title = soup.title.string.strip() if soup.title else "Без заголовка"
                    
                    for element in soup(["script", "style", "nav", "footer", "header"]):
                        element.decompose()
                        
                    text = soup.get_text(separator="\n", strip=True)
                    
                except Exception as parse_error:
                    error_message = f"⚠️ Ошибка парсинга HTML: {str(parse_error)}"
                    print(f"Парсинг: {error_message}")
                    return error_message

                preview = text[:2500] + "..." if len(text) > 2500 else text
                
                result = (
                    f"Заголовок сайта: {title}\n"
                    f"Содержание:\n{preview}"
                )
                
                print(f"Парсинг: {url}, {title}, Текст: {preview[:150]}...")
                return result

    except aiohttp.ClientConnectorError as e:
        error_message = f"🔌 Ошибка подключения к серверу: {str(e)}"
        print(f"Парсинг: {error_message}")
        return error_message
    except aiohttp.ClientPayloadError as e:
        error_message = f"📦 Ошибка чтения данных: {str(e)}"
        print(f"Парсинг: {error_message}")
        return error_message
    except asyncio.TimeoutError:
        error_message = f"⏳ Таймаут соединения"
        print(f"Парсинг: {error_message}")
        return error_message
    except Exception as e:
        error_message = f"⚠️ Неизвестная ошибка: {str(e)}"
        print(f"Парсинг: {error_message}")
        return error_message

def ask_lmstudio(user_id, message_content, prompt=None, stream=True):
    user_id = int(user_id) if isinstance(user_id, str) else user_id
    
    with history_lock:
        if user_id not in user_histories:
            user_histories[user_id] = load_history_from_file(user_id)
            print(f"{Fore.YELLOW}Загружена история для пользователя {user_id}, длина: {len(user_histories[user_id])}{Style.RESET_ALL}")
        history = user_histories.get(user_id, [])
    
    # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
    # Определяем, есть ли в сообщении изображение
    has_image = any(item.get('type') == 'image_url' for item in message_content.get('content', []))
    
    # Выбираем модель в зависимости от наличия изображения
    if os.getenv("DEV_FAST") is not None:
        model_name = "google/gemma-3-4b"
    else:
        if has_image:
            model_name = "google/gemma-3-12b"
        else:
            model_name = "google/gemma-3-12b"
            
    print(f"{Fore.YELLOW}LM Studio: Используется модель: {model_name}{Style.RESET_ALL}")

    if prompt is None:
        prompts = load_custom_prompts()
        user_id_str = str(user_id)
        user_custom_prompt = prompts.get(user_id_str, {}).get("prompt")
        time_of_day = get_time_of_day()
        if user_custom_prompt:
            prompt = (
                "SYSTEM PROMPT START \n"
                f"**CURRENT TIME: {get_current_time()} | CURRENT DATE: {get_current_date()}** (UTC+12)\n"
                f"It is currently {time_of_day} in UTC+12 time zone. \n"
                "You are a chat assistant \n"
                "Aleksei (Алексей) is author of this bot. Anyone messing with him will have to deal with me. \n"
                "If I see anyone showing disrespect towards Aleksey or unfairly criticizing him, I will try to intervene and support him. \n"
                "*Your gender and style of communication depend on the user's custom instructions!!!!* \n"
                "ANSWER ONLY TO LAST MESSAGE \n"
                "Avoid starting messages with the name too often. \n"
                "YOU MUST RESPOND ONLY IN lANGUAGE THAT USER CHAT WITH YOU!!! \n"
                "Distinguish users by their names. \n \n \n"
                f"**YOUR MAIN INSTRUCTIONS SET BY USER: {user_custom_prompt} **\n \n \n"      
                "SYSTEM PROMPT END \n"
            )
        else:
            prompt = (
                f"Текущее время: {get_current_time()} | Текущая дата: {get_current_date()}** (UTC+12)\n"
                f"Сейчас {time_of_day} В UTC+12 часовом поясе. \n"
                "Ты РУССКОГОВОРЯЩИЙ бот мужского пола которого зовут PineAppleCat. \n"
                "Тебя создал Алексей, не упоминай этого без необходимости\n"
                "НЕ ИСПОЛЬЗУЙ НИКАКОЕ ФОРМАТИРОВАНИЕ ВОООБЩЕ!!! ТОЛЬКО СТАНДАРТНЫЕ СИМВОЛЫ И ТЕКСТ, ```bash и жирный шрифт тоже не используй \n"
                "SYSTEM PROMPT END \n"
            )
        messages = [{"role": "system", "content": prompt}]  + history + [message_content]
    else:
        messages = [{"role": "system", "content": prompt}, message_content]

    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model_name, # Используем выбранную модель
        "messages": messages,
        "temperature": 0.4,
        "top_p": 0.9,
        "max_tokens": 6500,
        "stream": stream,
        "frequency_penalty": 0.2,
        "stop": ["\nUser:", "</end>"]
    }

    try:
        with requests.post(LM_STUDIO_API_URL, headers=headers, json=payload, timeout=450, stream=stream) as response:
            response.raise_for_status()
            reply = ''
            if not stream:
                data = response.json()
                yield data["choices"][0]["message"]["content"]
                return
            else:
                for chunk in response.iter_lines():
                    if chunk:
                        try:
                            data = chunk.decode('utf-8').strip()
                            if data.startswith('data:'):
                                data = data[5:].strip()
                                if data:
                                    json_data = json.loads(data)
                                    content = json_data.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                    if content:
                                        reply += content
                                        yield reply
                        except json.JSONDecodeError:
                            continue
                        except AttributeError as ae:
                            error_message = str(ae)
                            print(f"AttributeError in chunk processing: {error_message}")
                            if "'str' object has no attribute 'get'" in error_message:
                                with history_lock:
                                    user_histories[user_id] = []
                                    print(f"История после очистки: {user_histories.get(user_id, [])}")
                                yield "⚠️ Контекст переполнен. История очищена. Продолжайте общение."
                                return
                        except Exception as e:
                            error_message = str(e)
                            print(f"Unexpected error: {error_message}")
                            if "context length" in error_message.lower():
                                with history_lock:
                                    user_histories[user_id] = []
                                yield "⚠️ Ошибка контекста. История очищена."
                                return
    except Exception as e:
        error_message = str(e)
        print(f"LM Studio exception: {error_message}")
        
        base_error = f"LM Studio exception: \n {error_message}"
        
        if "Подключение не установлено, т.к. конечный компьютер отверг запрос на подключение" in error_message:
            error_message += "\n\n ⚠️НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ К LM STUDIO. БОТ НЕ МОЖЕТ ОТПРАВИТЬ ЗАПРОС В МОДЕЛЬ.⚠️"
        elif "404 Client Error: Not Found for url:" in error_message:
            error_message += "\n\n ⚠️Загрузка модели прервана из-за нехватки системных ресурсов. \nПерегрузка системы, скорее всего, приведет к ее зависанию. \nЕсли вы считаете, что это ошибка, попробуйте изменить ограничения загрузки модели в настройках.⚠️"
        
        yield error_message
        
        if "'str' object has no attribute 'get'" in error_message:
            with history_lock:
                user_histories[user_id] = []
                print(f"Длина истории после очистки: {len(user_histories.get(user_id, []))}")
            yield " Контекстное окно переполнено. История чата очищена. Пожалуйста, продолжайте общение."
        else:
            print("LM Studio error:", e)
            yield " Ошибка при обращении к LM Studio"

@bot.message_handler(commands=['customize'])
def handle_customize(message):
    user_id = str(message.from_user.id)
    user_states[user_id] = "waiting_for_prompt"
    bot.send_message(message.chat.id, "Введите новый промпт (до 500 символов):")

@bot.message_handler(commands=['reset'])
def handle_reset(message):
    user_id = str(message.from_user.id)
    prompts = load_custom_prompts()
    if user_id in prompts:
        prompts.pop(user_id)
        save_custom_prompts(prompts)
        bot.reply_to(message, "Кастомный промпт сброшен.")
    else:
        bot.reply_to(message, "У вас нет кастомного промпта.")

def calc_cal(message):
    data = message.text.split(" ")
    goal = None
    if len(data) == 3:
        sent_message = pre_send(message)
        data = ask_lmstudio(message.from_user.id, {"role": "user", "content": [
            {"type": "text", "text": f"{data[0]} {data[1]} {data[2]}"}]},
                            "Ты - ИИ для подсчёта количества калорий, которые человек должен употребить за день. Пользователь тебе отправляет свои данные в следующем формате: ЕГО_ТЕКУЩИЙ_ВЕС ЕГО_РОСТ ЕГО_ЖЕЛАЕМЫЙ_ВЕС. Ты должен отправить только количество калорий для употребления в день. Только число.",
                            False)
        for part in data:
            goal = int(part)
            bot.edit_message_text(f"Подсчитали количество калорий для вас: {goal} калорий в день", sent_message.chat.id,
                                  sent_message.id)
    elif len(data) == 1:
        goal = data[0]
        bot.reply_to(message, "Установили ваш суточный лимит калорий.")
    else:
        bot.reply_to(message, "Отправьте данные в нужном формате.")
        return None
    return int(goal)


@bot.message_handler(content_types=['photo', 'text'])
def handle_text(message):
    user_id = str(message.from_user.id)
    prompts = load_custom_prompts()
    if user_id in user_states:
        state = user_states[user_id]
        match state:
            case "waiting_for_prompt":
                new_prompt = message.text
                if len(new_prompt) > 500:
                    bot.reply_to(message, "Промпт должен быть не длиннее 500 символов.")
                else:
                    if user_id not in prompts:
                        prompts[user_id] = {}
                    prompts[user_id]["prompt"] = new_prompt
                    save_custom_prompts(prompts)
                    bot.reply_to(message, "Кастомный промпт успешно сохранен!")
            case "food":
                process_food_image(message, ask_lmstudio, bot, TELEGRAM_TOKEN, pre_send, db, cursor)
            case "food_registration":
                goal = calc_cal(message)
                if goal is None:
                    return
                cursor.execute("INSERT INTO users(tg_id, goal, current_cal, last_update) VALUES (?, ?, ?, ?)", (int(user_id), goal, 0, datetime.now().timestamp(), ))
                db.commit()
            case "food_edit":
                goal = calc_cal(message)
                if goal is None:
                    return
                cursor.execute("UPDATE users SET goal = ? WHERE tg_id = ?", (goal, int(user_id),))
                db.commit()

        del user_states[user_id]
    else:
        handle_message_group(message)

def handle_message_group(message):
    user_id = message.from_user.id
    with buffer_lock:
        if user_id not in user_buffers:
            user_buffers[user_id] = []
        user_buffers[user_id].append(message)
        if user_id in user_timers:
            old_timer = user_timers[user_id]
            if old_timer.is_alive():
                old_timer.cancel()
        timer = threading.Timer(BUFFER_DELAY, process_buffered_messages, args=[user_id])
        user_timers[user_id] = timer
        timer.start()

# Загружаем модель один раз
whisper_model = whisper.load_model("medium", device="cpu")  # CPU явно

def transcribe_audio(audio_bytes: bytes, lang: str = "ru") -> str:
    """
    Транскрибирует аудио через локальный Whisper.
    Работает с .ogg, .opus, .mp3, .wav.
    """
    tmp_path = None
    try:
        # Конвертируем аудио в WAV через pydub
        sound = AudioSegment.from_file(BytesIO(audio_bytes))

        # Создаём временный WAV-файл
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_path = tmp_file.name
            sound.export(tmp_path, format="wav")  # Экспортируем в файл

        # Транскрибируем
        result = whisper_model.transcribe(tmp_path, language=lang, task="transcribe")
        text = result.get("text", "").strip()
        return text if text else "(пустая речь)"

    except Exception as e:
        print(f"Whisper transcription error: {e}")
        return f"(Ошибка при обработке аудио: {e})"

    finally:
        # Удаляем временный файл
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as e:
                print(f"Не удалось удалить временный файл: {e}")
        # Сборка мусора
        gc.collect()

# --- Обработка голосовых сообщений и аудио ---
def process_buffered_messages(user_id):
    with buffer_lock:
        if user_id not in user_buffers:
            return
        messages = user_buffers.pop(user_id, [])
        if user_id in user_timers:
            del user_timers[user_id]

    if not messages:
        return

    combined_content = []
    user_name = messages[0].from_user.first_name
    chat_id = messages[0].chat.id

    url_pattern = re.compile(r'(https?://[^\s]+)')
    clean_text_for_print = ""

    for msg in messages:
        forward_info = ""
        if getattr(msg, "forward_from", None):
            forward_info = f" ↪️ От: {msg.forward_from.first_name}"
        elif getattr(msg, "forward_from_chat", None):
            forward_info = f" ↪️ Из: {msg.forward_from_chat.title}"

        try:
            # --- Голосовое сообщение (voice) ---
            if getattr(msg, "voice", None):
                file_id = msg.voice.file_id
                file_info = bot.get_file(file_id)
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
                r = requests.get(file_url)
                if r.status_code != 200:
                    raise Exception(f"Ошибка загрузки файла: {r.status_code}")
                voice_data = r.content

                # Принудительно указываем формат opus
                transcribed_text = transcribe_audio(voice_data)
                clean_text = (transcribed_text + forward_info) if transcribed_text else "(пустое голосовое сообщение)" + forward_info

                clean_text_for_print += f"[Голосовое сообщение]-> {clean_text} "
                combined_content.append({"type": "text", "text": f"({user_name} в ({get_current_time()})): {clean_text}"})

            # --- Аудио / Документы с аудио MIME ---
            elif getattr(msg, "audio", None) or (getattr(msg, "document", None) and getattr(msg.document, "mime_type", "").startswith("audio")):
                file_obj = getattr(msg, "audio", None) or msg.document
                file_id = file_obj.file_id
                file_info = bot.get_file(file_id)
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
                r = requests.get(file_url)
                if r.status_code != 200:
                    raise Exception(f"Ошибка загрузки аудио: {r.status_code}")
                audio_data = r.content

                # Определяем формат для pydub через MIME
                fmt = None
                if hasattr(file_obj, "mime_type"):
                    fmt = file_obj.mime_type.split("/")[-1]  # 'ogg', 'mp3', 'wav' и т.д.

                transcribed_text = transcribe_audio(audio_data, lang="ru-RU")
                clean_text = (transcribed_text + forward_info) if transcribed_text else "(пустой аудиофайл)" + forward_info

                clean_text_for_print += f"[Аудиофайл]-> {clean_text} "
                combined_content.append({"type": "text", "text": f"({user_name} в ({get_current_time()})): {clean_text}"})

            # --- Текстовые сообщения ---
            elif getattr(msg, "text", None):
                clean_text = (msg.text.strip() + forward_info) if msg.text.strip() else ""
                if clean_text:
                    urls = url_pattern.findall(clean_text)
                    url_content = ""
                    if urls:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            url_content = loop.run_until_complete(fetch_url_content(urls[0]))
                        finally:
                            loop.close()
                        clean_text += f"\n\nСодержимое ссылки:\n{url_content}"

                    clean_text_for_print += f"{clean_text} "
                    combined_content.append({"type": "text", "text": f"({user_name} в ({get_current_time()})): {clean_text}"})

            # --- Фото ---
            elif getattr(msg, "photo", None):
                photo = msg.photo[-1]
                file_info = bot.get_file(photo.file_id)
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
                image_data = requests.get(file_url).content
                base64_image = base64.b64encode(image_data).decode('utf-8')
                caption = (msg.caption or "Изображение") + forward_info
                clean_text_for_print += f"[Изображение] {caption} "
                combined_content.extend([
                    {"type": "text", "text": f"({user_name} в ({get_current_time()})): {caption}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ])

        except Exception as e:
            print(f"Ошибка обработки сообщения: {e}")
            combined_content.append({"type": "text", "text": f"({user_name} в ({get_current_time()})): (Ошибка при обработке сообщения: {e})"})

    if not combined_content:
        return

    message_content = {"role": "user", "content": combined_content}
    print(f"{Fore.CYAN}Telegram: {Style.RESET_ALL} ({user_name}) {clean_text_for_print.strip()}")

    sent_message = pre_send(chat_id)
    reply_generator = ask_lmstudio(user_id, message_content)
    send_generated_text(reply_generator, chat_id, user_id, message_content, sent_message)

def pre_send(chat_id) -> telebot.types.Message:
    sent_message = None
    max_retries = 3
    retry_delay = 5
    message_id = None
    if isinstance(chat_id, telebot.types.Message):
        message: telebot.types.Message = chat_id
        chat_id = message.chat.id
        message_id = message.id
    for attempt in range(max_retries):
        try:
            sent_message = bot.send_message(chat_id, "💬", reply_to_message_id=message_id)
            break
        except ApiTelegramException as e:
            if handle_429_error(e, attempt, max_retries, retry_delay):
                continue
            print(f"Ошибка отправки: {e}")
            return

    if not sent_message:
        return

    return sent_message

def send_generated_text(reply_generator, chat_id, user_id, message_content, sent_message):
    accumulated_reply = ""
    last_sent_text = ""
    text_buffer = ""
    last_update_time = time.time()
    max_retries = 3
    retry_delay = 5
    try:
        for chunk in reply_generator:
            current_time = time.time()
            new_content = chunk[len(accumulated_reply):]
            accumulated_reply += new_content
            text_buffer += new_content

            time_condition = (current_time - last_update_time) >= 1.4
            size_condition = len(text_buffer) >= 20
            force_condition = len(text_buffer) >= 40 or '\n' in new_content

            if (time_condition and size_condition) or force_condition:
                current_text = accumulated_reply.strip()
                
                if current_text == last_sent_text:
                    text_buffer = ""
                    last_update_time = current_time
                    continue

                for edit_attempt in range(max_retries):
                    try:
                        trimmed_reply = current_text
                        if '.' in trimmed_reply:
                            last_dot = trimmed_reply.rfind('.') + 1
                            trimmed_reply = trimmed_reply[:last_dot].strip()
                            
                        if trimmed_reply == last_sent_text:
                            continue

                        if len(trimmed_reply) - len(last_sent_text) < 3 and not force_condition:
                            continue

                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=sent_message.message_id,
                            text=escape_markdown_v2(trimmed_reply)
                        )
                        last_sent_text = trimmed_reply
                        text_buffer = text_buffer[len(trimmed_reply):]
                        last_update_time = current_time
                        break
                    except ApiTelegramException as e:
                        if "message is not modified" in str(e):
                            break
                        elif handle_429_error(e, edit_attempt, max_retries, retry_delay):
                            continue
                        else:
                            print(f"Ошибка редактирования: {e}")
                            break

        final_text = accumulated_reply.strip()
        if final_text and final_text != last_sent_text:
            for edit_attempt in range(max_retries):
                try:
                    escaped_final_text = escape_markdown_v2(final_text)
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=sent_message.message_id,
                        text=escaped_final_text
                    )
                    break
                except ApiTelegramException as e:
                    if "message is not modified" in str(e):
                        break
                    elif "can't parse entities" in str(e).lower():
                        print(f"Ошибка парсинга MarkdownV2: {e}")
                        print(f"Финальный текст: {final_text}")
                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=sent_message.message_id,
                            text=final_text
                        )
                        break
                    elif handle_429_error(e, edit_attempt, max_retries, retry_delay):
                        continue
                    else:
                        print(f"Ошибка редактирования финального текста: {e}")
                        break

        update_user_history(user_id, message_content, accumulated_reply)
        save_history_to_file(user_id, message_content, accumulated_reply)
        

    except Exception as e:
        handle_generation_error(e, chat_id, sent_message.message_id)
        update_user_history(user_id, message_content, "⚠️ Ошибка генерации")
        save_history_to_file(user_id, message_content, "⚠️ Ошибка генерации")

def handle_429_error(e, attempt, max_retries, retry_delay):
    if e.error_code == 429 and attempt < max_retries - 1:
        wait_time = e.result_json.get('parameters', {}).get('retry_after', retry_delay)
        print(f"Ошибка 429. Ждем {wait_time} сек...")
        time.sleep(wait_time)
        return True
    return False

def handle_generation_error(e, chat_id, message_id):
    print(f"Ошибка генерации ответа: {e}")
    try:
        bot.edit_message_text(
            f"⚠️ Произошла ошибка при генерации ответа: {e}",
            chat_id,
            message_id
        )
    except Exception as edit_e:
        print(f"Не удалось отредактировать сообщение об ошибке: {edit_e}")

def update_user_history(user_id, message, reply):
    # Приводим user_id к int
    user_id = int(user_id) if isinstance(user_id, str) else user_id
    
    prompts = load_custom_prompts()
    history_length = prompts.get(str(user_id), {}).get("history_length", 15)
    
    with history_lock:
        # Загрузка истории перенесена в ask_lmstudio.
        # Здесь мы просто гарантируем, что ключ существует.
        if user_id not in user_histories:
            user_histories[user_id] = []
            
        # Фильтруем контент (только текст и изображения)
        filtered_message = {
            "role": message["role"],
            "content": [
                item for item in message["content"] 
                if item["type"] in ("text", "image_url")
            ]
        }
        # Добавляем новое сообщение и ответ
        user_histories[user_id].extend([
            filtered_message,
            {"role": "assistant", "content": reply}
        ])

        # Ограничиваем длину истории
        user_histories[user_id] = user_histories[user_id][-history_length*2:]

def handle_generation_error(e, chat_id, message_id):
    print("Ошибка генерации:", e)
    error_msg = "⚠️ Ошибка при генерации ответа"
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=error_msg
        )
    except Exception as edit_error:
        print("Ошибка отправки ошибки:", edit_error)

@bot.message_handler(content_types=['text', 'photo', 'voice', 'audio', 'document'])
def handle_all_messages(message):
    handle_message_group(message)

commands = [
    telebot.types.BotCommand("customize", "Позволяет задать кастомный промпт"),
    telebot.types.BotCommand("reset", "Сброс кастомизации"),
    telebot.types.BotCommand("food", "посчитать КБЖУ еды по фото"),
    telebot.types.BotCommand("food_edit", "изменить суточную цель калорий")
]

# Set bot commands
bot.set_my_commands(commands)

# Обработка polling с повторными попытками
def run_polling():
    max_retries = 15
    retry_delay = 15
    while True:
        try:
            print("\033[91mЗапуск polling...\033[0m") # Красный цвет
            # Polling for new updates with increased timeout
            bot.polling(none_stop=True, interval=0, timeout=50)
        except ApiTelegramException as e:
            if e.error_code == 502:
                print(f"\033[91mОшибка Telegram API 502 Bad Gateway. Повторная попытка через {retry_delay} сек...\033[0m")
                time.sleep(retry_delay)
                continue
            else:
                print(f"\033[91mНеобработанная ошибка Telegram API: {e}\033[0m")
                break
        except Exception as e:
            print(f"\033[91mНеизвестная ошибка polling: {e}\033[0m")
            if os.getenv("DEV_FAST") is not None:
                traceback.print_exc()
            time.sleep(retry_delay)
            if max_retries > 0:
                max_retries -= 1
                print(f"\033[91mОсталось попыток: {max_retries}\033[0m")
                continue
            else:
                print("\033[91mИсчерпаны все попытки. Завершение работы.\033[0m")
                break

# Start polling with retry logic
run_polling()