import base64
import datetime
import json

import requests
from telebot import types, TeleBot

def process_food_image(message: types.Message, ask_lmstudio, bot: TeleBot, token: str, pre_send, db, cursor):
    images = message.photo
    if images is None:
        return
    sent_message = pre_send(message.chat.id)
    images = images[:5]
    encoded_images = []
    for image in images:
        file_info = bot.get_file(image.file_id)
        file_url = f"https://api.telegram.org/file/bot{token}/{file_info.file_path}"
        image_data = requests.get(file_url).content
        base64_image = base64.b64encode(image_data).decode('utf-8')
        encoded_images.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}})
    message_content = {"role": "user", "content": encoded_images}
    reply_generator = ask_lmstudio(message.from_user.id, message_content, """Пользователь отправляет тебе фотографию еды, твоя задача: понять, что за блюдо, оценить массу, КБЖУ блюда, клетчатку и насколько полезно блюдо по шкале от 0 до 10. Отвечай только в формате JSON следующего вида, не указывай никаких дополнительных слов и меру измерения, название блюда пиши на русском языке: {"name": "Название блюда на русском", "mass": "Примерная масса блюда в граммах", "calories": "Количество калорий", "protein": "Масса белков в граммах", "fat": "Масса жиров в граммах", "carbs": "Масса углеводов в граммах", "fibes": "Масса клетчатки в граммах", "health": 10}
Если на фотографии не еда или невозможно определить, то отправляй следующее: {"error": "Not food"}
Форматирование Markdown для выделения JSON кода не используй.""", False)
    for response in reply_generator:
        if response.startswith("```"):
            response = response.replace("```", "").replace("json", "").strip()
        data = json.loads(response)
        if isinstance(data, list):
            data = data[0]

        if "error" in data.keys():
            bot.edit_message_text(chat_id=sent_message.chat.id, message_id=sent_message.message_id, text="На фото отсутствует еда")
            return

        cursor.execute("SELECT goal, current_cal, last_update FROM users WHERE tg_id = ?", (message.from_user.id,))
        user = cursor.fetchone()
        current_cal = int(user[1])
        if datetime.datetime.fromtimestamp(user[2]).date() != datetime.datetime.now().date():
            current_cal = 0
        current_cal += int(data["calories"])

        cursor.execute("UPDATE users SET current_cal = ?, last_update = ? WHERE tg_id = ?", (current_cal, datetime.datetime.now().timestamp(), message.from_user.id, ))
        db.commit()

        bot.edit_message_text(chat_id=sent_message.chat.id, message_id=sent_message.message_id, text=f"""Название блюда: {data['name']}
Масса: ~{data['mass']} г
Калорий: {data['calories']}
Белков: {data['protein']} г
Жиров: {data['fat']} г
Углеводов: {data['carbs']} г
Клетчатки: {data['fibes']} г
Полезность: {data['health']}/10

Количество калорий за день: {current_cal}/{user[0]}""")