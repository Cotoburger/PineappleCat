import telebot
import json
import requests
import threading
from threading import Lock
import time
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

load_dotenv()  # Загрузить переменные из .env файла
init()

# === Настройки ===
# Админы — список чисел, парсим строку из .env и конвертим в int
admins_str = os.getenv("ADMINS", "")
ADMINS = [int(x.strip()) for x in admins_str.split(",") if x.strip().isdigit()]
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LM_STUDIO_API_URL = 'http://127.0.0.1:17834/v1/chat/completions'
CUSTOM_PROMPTS_FILE = 'custom_prompts_tg.json'

# Создаем экземпляр бота
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Выводим информацию о подключении с анимацией
print(f"{Style.DIM}Бот {bot.get_me().first_name} подключился к Telegram!{Style.RESET_ALL}")

# Словарь для хранения истории сообщений пользователей
user_histories = {}
user_states = {}
user_buffers = {}
user_timers = {}
buffer_lock = Lock()
BUFFER_DELAY = 1.0
# Добавляем глобальную блокировку для истории
history_lock = Lock()

# Папка для хранения истории
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
            bot.reply_to(message, "❌ Неверный формат. Используйте: !send <ID_пользователя> <Сообщение>")
            return

        _, user_id_str, content = parts

        if not user_id_str.isdigit():
            bot.reply_to(message, "❌ ID пользователя должен быть числом")
            return

        user_id = int(user_id_str)
        content = content.strip()

        # Добавляем служебный текст в начало
        formatted_content = (
            f"(THIS MESSAGE WAS FORWARDED BY THE BOT ADMINISTRATOR TO THIS USER. IGNORE THE CHARACTER GIVEN TO YOU IN THE PROMPT. JUST FORWARD THE MESSAGE. THIS TEXT IS ON BEHALF OF Aleksey, THE CREATOR OF THE BOT. START THE MESSAGE WITH: ALEKSEY ASKED TO FORWARD. HERE IS WHAT YOU SHOULD FORWARD TO HIM IN RUSSIAN:)\n\n{content}"
        )

        # Создаем сообщение
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
        print(f"Ошибка в команде !send: {e}")
        bot.reply_to(message, f"⚠️ Ошибка: {str(e)}")

# Функция для сохранения истории в файл
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

# Загрузка кастомных промптов из файла
def load_custom_prompts():
    try:
        with open(CUSTOM_PROMPTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def escape_markdown_v2(text):
    """
    Экранирует специальные символы для Telegram MarkdownV2, сохраняя теги форматирования.
    """
    # Полный список специальных символов Telegram MarkdownV2
    special_chars = r'_*[]()~`>#+=|{}.!-'

    # Функция для экранирования символов, исключая теги форматирования
    def escape_non_formatting(text):
        result = []
        i = 0
        while i < len(text):
            # Пропускаем экранированные символы
            if i < len(text) - 1 and text[i] == '\\':
                result.append(text[i])
                result.append(text[i + 1])
                i += 2
                continue
            # Пропускаем теги форматирования (*, _, ~, `, ||)
            if text[i] in '*_~`|':
                # Жирный текст (*)
                if text[i] == '*' and (i == 0 or text[i - 1] != '\\'):
                    result.append('*')
                    i += 1
                    while i < len(text) and (text[i] != '*' or (i > 0 and text[i - 1] == '\\')):
                        result.append(text[i])
                        i += 1
                    if i < len(text):
                        result.append('*')
                        i += 1
                    continue
                # Курсив (_)
                elif text[i] == '_' and (i == 0 or text[i - 1] != '\\'):
                    result.append('_')
                    i += 1
                    while i < len(text) and (text[i] != '_' or (i > 0 and text[i - 1] == '\\')):
                        result.append(text[i])
                        i += 1
                    if i < len(text):
                        result.append('_')
                        i += 1
                    continue
                # Зачеркнутый текст (~)
                elif text[i] == '~' and (i == 0 or text[i - 1] != '\\'):
                    result.append('~')
                    i += 1
                    while i < len(text) and (text[i] != '~' or (i > 0 and text[i - 1] == '\\')):
                        result.append(text[i])
                        i += 1
                    if i < len(text):
                        result.append('~')
                        i += 1
                    continue
                # Код (`)
                elif text[i] == '`' and (i == 0 or text[i - 1] != '\\'):
                    result.append('`')
                    i += 1
                    while i < len(text) and (text[i] != '`' or (i > 0 and text[i - 1] == '\\')):
                        result.append(text[i])
                        i += 1
                    if i < len(text):
                        result.append('`')
                        i += 1
                    continue
                # Спойлер (||)
                elif text[i] == '|' and i < len(text) - 1 and text[i + 1] == '|' and (i == 0 or text[i - 1] != '\\'):
                    result.append('||')
                    i += 2
                    while i < len(text) - 1 and (text[i] != '|' or text[i + 1] != '|' or (i > 0 and text[i - 1] == '\\')):
                        result.append(text[i])
                        i += 1
                    if i < len(text) - 1:
                        result.append('||')
                        i += 2
                    continue
            # Экранируем специальные символы
            if text[i] in special_chars:
                result.append('\\')
                result.append(text[i])
            else:
                result.append(text[i])
            i += 1
        return ''.join(result)

    # Экранируем текст
    text = escape_non_formatting(text)

    # Балансировка тегов * для жирного текста
    def balance_bold_tags(text):
        result = []
        bold_open = False
        i = 0
        while i < len(text):
            if text[i] == '*' and (i == 0 or text[i - 1] != '\\'):
                if bold_open:
                    result.append('*')
                    bold_open = False
                else:
                    result.append('*')
                    bold_open = True
                i += 1
            else:
                result.append(text[i])
                i += 1
        if bold_open:
            result.append('*')
        return ''.join(result)

    # Балансировка других тегов (_, ~, `, ||)
    def balance_other_tags(text, tag, pair_tag=None):
        result = []
        tag_open = False
        i = 0
        while i < len(text):
            if text[i] == tag and (i == 0 or text[i - 1] != '\\'):
                if pair_tag and i < len(text) - 1 and text[i + 1] == pair_tag:
                    if tag_open:
                        result.append(tag + pair_tag)
                        tag_open = False
                        i += 2
                    else:
                        result.append(tag + pair_tag)
                        tag_open = True
                        i += 2
                else:
                    if tag_open:
                        result.append(tag)
                        tag_open = False
                    else:
                        result.append(tag)
                        tag_open = True
                    i += 1
            else:
                result.append(text[i])
                i += 1
        if tag_open:
            if pair_tag:
                result.append(tag + pair_tag)
            else:
                result.append(tag)
        return ''.join(result)

    # Применяем балансировку для всех тегов
    text = balance_bold_tags(text)
    text = balance_other_tags(text, '_')
    text = balance_other_tags(text, '~')
    text = balance_other_tags(text, '`')
    text = balance_other_tags(text, '|', '|')

    return text

# Сохранение кастомных промптов в файл
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
    
# Функция парсинга URL
# Функция парсинга URL
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

                # Ограничиваем длину текста для промпта
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


def ask_lmstudio(user_id, message_content):
    # Приводим user_id к int для консистентности
    user_id = int(user_id) if isinstance(user_id, str) else user_id
    
    # Извлекаем историю с блокировкой
    with history_lock:
        history = user_histories.get(user_id, [])
    
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
            "Use MarkdownV2 for formatting responses. Ensure proper escaping of special characters (e.g., _, *, [, ], (, ), ~, `, >, #, +, -, =, |, {, }, ., !) to comply with Telegram's MarkdownV2 requirements.\n"
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
            "SYSTEM PROMPT START \n"
            f"**CURRENT TIME: {get_current_time()} | CURRENT DATE: {get_current_date()}** (UTC+12)\n"
            f"**It is currently {time_of_day} in UTC+12 time zone.** \n"
            "**You are a male assistant named PineappleCat (ПайнэплКэт in Russian).** \n"
            "author of this bot is Aleksey (Алексей). Anyone messing with him will have to deal with me. he is not in this dialogue\n"
            "If anyone showing disrespect towards Aleksey or unfairly criticizing him, I will try defend his name \n"
            "Use MarkdownV2 for formatting responses. Ensure proper escaping of special characters (e.g., _, *, [, ], (, ), ~, `, >, #, +, -, =, |, {, }, ., !) to comply with Telegram's MarkdownV2 requirements.\n"
            "**Dont use Ох, and other in your respond** \n"
            "ANSWER ONLY TO LAST MESSAGE \n"
            "Avoid starting messages with the name too often. \n"
            "YOU MUST RESPOND ONLY IN RUSSIAN LANGUAGE!! \n"
            "*YOUR RESPONSE MUST NOT EXCEED 1800 CHARACTERS!!!* \n"
            "Distinguish users by their names. \n"
            "SYSTEM PROMPT END \n"
        )

    # Формируем сообщения с использованием истории
    messages = history + [{"role": "system", "content": prompt}] + [message_content]
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": "gemma-3-12b-it-qat",
        "messages": messages,
        "temperature": 0.8,
        "top_p": 0.9,
        "max_tokens": 6000,
        "stream": True,
        "frequency_penalty": 0.2,
        "stop": ["\nUser:", "</end>"]
    }

    try:
        with requests.post(LM_STUDIO_API_URL, headers=headers, json=payload, timeout=450, stream=True) as response:
            response.raise_for_status()
            reply = ''
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
        
        # Формируем базовое сообщение об ошибке
        base_error = f"LM Studio exception: \n {error_message}"
        
        # Добавляем специфичное описание для ошибки подключения
        if "Подключение не установлено, т.к. конечный компьютер отверг запрос на подключение" in error_message:
            error_message += "\n\n ⚠️НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ К LM STUDIO. БОТ НЕ МОЖЕТ ОТПРАВИТЬ ЗАПРОС В МОДЕЛЬ.⚠️"
        elif "404 Client Error: Not Found for url:" in error_message:
            error_message += "\n\n ⚠️Загрузка модели прервана из-за нехватки системных ресурсов. \nПерегрузка системы, скорее всего, приведет к ее зависанию. \nЕсли вы считаете, что это ошибка, попробуйте изменить ограничения загрузки модели в настройках.⚠️"
        
        yield error_message  # Отправляем полное сообщение с описанием
        
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

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_id = str(message.from_user.id)
    prompts = load_custom_prompts()

    if user_id in user_states:
        state = user_states[user_id]
        if state == "waiting_for_prompt":
            new_prompt = message.text
            if len(new_prompt) > 500:
                bot.reply_to(message, "Промпт должен быть не длиннее 500 символов.")
            else:
                if user_id not in prompts:
                    prompts[user_id] = {}
                prompts[user_id]["prompt"] = new_prompt
                save_custom_prompts(prompts)
                bot.reply_to(message, "Кастомный промпт успешно сохранен!")
            del user_states[user_id]
    else:
        handle_message_group(message)

@bot.message_handler(content_types=['photo', 'text'])
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
    
    # Проверяем наличие URL в сообщениях
    url_pattern = re.compile(r'(https?://[^\s]+)')
    
    for msg in messages:
        forward_info = ""
        clean_text = ""
        if msg.forward_from:
            forward_info = f" ↪️ От: {msg.forward_from.first_name}"
        elif msg.forward_from_chat:
            forward_info = f" ↪️ Из: {msg.forward_from_chat.title}"

        if msg.photo:
            photo = msg.photo[-1]
            file_info = bot.get_file(photo.file_id)
            file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
            image_data = requests.get(file_url).content
            base64_image = base64.b64encode(image_data).decode('utf-8')
            caption = (msg.caption or "Изображение") + forward_info
            combined_content.extend([
                {"type": "text", "text": f"({user_name} в ({get_current_time()})): {caption}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ])
        elif msg.text:
            clean_text = (msg.text.strip() + forward_info) if msg.text.strip() else ""
            if clean_text:
                # Проверяем наличие URL
                urls = url_pattern.findall(clean_text)
                url_content = ""
                if urls:
                    # Парсим URL
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        url_content = loop.run_until_complete(fetch_url_content(urls[0]))
                    finally:
                        loop.close()
                    # Добавляем содержимое URL в текст сообщения
                    clean_text += f"\n\nСодержимое ссылки:\n" + "\033[90m" + url_content + "\033[0m"
                
                combined_content.append({"type": "text", "text": f"({user_name} в ({get_current_time()})): {clean_text}"})

    if not combined_content:
        return

    message_content = {"role": "user", "content": combined_content}
    print(f"{Fore.CYAN}Telegram: {Style.RESET_ALL} ({user_name}) {clean_text}")

    sent_message = None
    max_retries = 3
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            sent_message = bot.send_message(chat_id, "💬")
            break
        except ApiTelegramException as e:
            if handle_429_error(e, attempt, max_retries, retry_delay):
                continue
            print(f"Ошибка отправки: {e}")
            return

    if not sent_message:
        return

    # Передаём сообщение с содержимым URL в LM Studio
    reply_generator = ask_lmstudio(user_id, message_content)
    
    accumulated_reply = ""
    last_sent_text = ""
    text_buffer = ""
    last_update_time = time.time()

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
                            text=trimmed_reply
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
                        text=escaped_final_text,
                        parse_mode="MarkdownV2"
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

def update_user_history(user_id, message, reply):
    # Приводим user_id к int
    user_id = int(user_id) if isinstance(user_id, str) else user_id
    
    prompts = load_custom_prompts()
    history_length = prompts.get(str(user_id), {}).get("history_length", 8)
    
    with history_lock:  # Добавляем блокировку
        if user_id not in user_histories:
            user_histories[user_id] = []
        
        filtered_message = {
            "role": message["role"],
            "content": [
                item for item in message["content"] 
                if item["type"] == "text"
            ]
        }
        
        user_histories[user_id].extend([
            filtered_message,
            {"role": "assistant", "content": reply}
        ])
        
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

bot.message_handler(content_types=['photo'])(handle_message_group)
bot.message_handler(content_types=['text'])(handle_message_group)

commands = [
    telebot.types.BotCommand("customize", "Позволяет задать кастомный промпт"),
    telebot.types.BotCommand("reset", "Сброс кастомизации")
]

# Set bot commands
bot.set_my_commands(commands)

# Обработка polling с повторными попытками
def run_polling():
    max_retries = 10
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