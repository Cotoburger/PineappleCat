from telethon.sync import TelegramClient
from telethon.tl.functions.messages import GetHistoryRequest
import re
import json
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta

load_dotenv()  # Загрузить переменные из .env файла

# Telegram API
api_id = int(os.getenv("TELEGRAM_API_ID"))
api_hash = os.getenv("TELEGRAM_API_HASH")
channel_username = os.getenv("TELEGRAM_CHANNEL_USERNAME")

# Discord
DISCORD_CHANNEL_ID = 1341736506804539393
MAG_THRESHOLD = 4.0

# Файл для хранения ID последнего обработанного сообщения
LAST_ID_FILE = "last_telegram_id.json"

def extract_info(message):
    time_match = re.search(r"Время UTC:\s*(.+)", message)
    coords_match = re.search(r"Координаты:\s*([0-9\., ]+)", message)
    dist_match = re.search(r"Расстояние от ПК:\s*(\d+)", message)
    depth_match = re.search(r"Глубина \(КМ\):\s*([0-9.]+)", message)
    mag_match = re.search(r"Магнитуда:\s*([0-9.]+)", message)
    intensity_match = re.search(r"Интенсивность в ПК.*:\s*(\d+)", message)

    # Преобразование времени UTC в UTC+12
    if time_match:
        try:
            utc_time_str = time_match.group(1).strip()
            dt = datetime.strptime(utc_time_str, "%d %b %Y  %H:%M:%S")
            dt_local = dt + timedelta(hours=12)
            local_time_str = dt_local.strftime("%d %b %Y %H:%M:%S")
        except Exception:
            local_time_str = "—"
    else:
        local_time_str = "—"

    coords = coords_match.group(1).strip() if coords_match else "—"
    dist = dist_match.group(1) if dist_match else "—"
    depth = depth_match.group(1) if depth_match else "—"
    mag = float(mag_match.group(1)) if mag_match else None
    intensity = intensity_match.group(1) if intensity_match else None

    return local_time_str, coords, dist, depth, mag, intensity

def load_last_message_id():
    if os.path.exists(LAST_ID_FILE):
        with open(LAST_ID_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f).get("last_id", 0)
            except json.JSONDecodeError:
                return 0
    return 0

def save_last_message_id(message_id):
    with open(LAST_ID_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": message_id}, f, indent=2)

async def check_earthquakes(discord_bot):
    try:
        async with TelegramClient('eqk_monitor', api_id, api_hash) as client:
            result = await client(GetHistoryRequest(
                peer=channel_username,
                limit=1,
                offset_date=None,
                offset_id=0,
                max_id=0,
                min_id=0,
                add_offset=0,
                hash=0
            ))

            if result.messages:
                message_obj = result.messages[0]
                message_id = message_obj.id
                msg_text = getattr(message_obj, "message", "") or ""

                last_id = load_last_message_id()
                if message_id <= last_id:
                    return  # Уже обработано, пропускаем

                time_str, coords, dist, depth, mag, intensity = extract_info(msg_text)

                if mag and mag >= MAG_THRESHOLD:
                    channel = discord_bot.get_channel(DISCORD_CHANNEL_ID)
                    if channel:
                        formatted = (
                            f"‼️ **Землетрясение!**\n"
                            f"🕒 Время (UTC+12): {time_str}\n"
                            f"📍 Координаты: {coords}\n"
                            f"📏 Расстояние от ПК: {dist} км\n"
                            f"🌋 Глубина: {depth} км\n"
                            f"💥 Магнитуда: {mag}"
                        )
                        if intensity:
                            formatted += f"\n📊 Интенсивность (предварительная): {intensity}"
                        await channel.send(formatted)
                    else:
                        print("❌ Не удалось найти Discord-канал")
                else:
                    print(f"[DEBUG] Магнитуда {mag} ниже порога {MAG_THRESHOLD}")

                save_last_message_id(message_id)
            else:
                print("❌ Сообщения в Telegram-канале не найдены")

    except Exception as e:
        print(f"⚠️ Ошибка в check_earthquakes: {e}")
