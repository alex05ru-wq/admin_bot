"""
Бот-администратор для групп Telegram.

Умеет:
— капча для новичков: кнопка «Я не бот», иначе удаление из группы
— приветствие с правилами, удаление служебных сообщений
— удаление ссылок и мата, предупреждения, мут после N предупреждений
— антифлуд: мут за слишком частые сообщения
— команды для админов (ответом на сообщение): /warn /mute /unmute /ban /unban, а также /rules

Боту нужны права администратора: «Удаление сообщений» и «Блокировка пользователей».
Запуск: python bot.py   (перед этим заполните .env)
"""
import os
import re
import threading
import time
from collections import defaultdict, deque
from html import escape

import telebot
from dotenv import load_dotenv
from telebot import types

import config as C

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise SystemExit('Не найден BOT_TOKEN. Создайте файл .env (см. .env.example)')

bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
lock = threading.Lock()
warns = defaultdict(int)                 # (chat_id, user_id) -> число предупреждений
recent = defaultdict(deque)              # (chat_id, user_id) -> время последних сообщений
captcha = {}                             # (chat_id, user_id) -> (message_id приветствия, таймер)
admin_cache = {}                         # (chat_id, user_id) -> (is_admin, время проверки)

URL_RE = re.compile(r'(https?://\S+|www\.\S+|\bt\.me/\S+|\b[\w-]+\.(?:ru|com|net|org|info|рф|io|me|su)\b\S*)', re.I)
WORD_RE = re.compile(r'[a-zа-яё]+', re.I)


# ---------- Вспомогательное ----------

def mention(user):
    return f'<a href="tg://user?id={user.id}">{escape(user.first_name or "участник")}</a>'


def is_admin(chat_id, user_id):
    """Админ ли пользователь. Ответ запоминаем на минуту, чтобы не спрашивать Telegram на каждое сообщение."""
    key = (chat_id, user_id)
    cached = admin_cache.get(key)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    try:
        status = bot.get_chat_member(chat_id, user_id).status
        result = status in ('administrator', 'creator')
    except Exception:
        result = False
    admin_cache[key] = (result, time.time())
    return result


def from_admin(message):
    # Анонимный админ пишет от имени самой группы
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True
    return is_admin(message.chat.id, message.from_user.id)


def say(chat_id, text, autodelete=True, **kw):
    """Отправить сообщение и (по настройке) удалить его через N секунд."""
    msg = bot.send_message(chat_id, text, **kw)
    if autodelete and C.AUTO_DELETE_BOT_MESSAGES:
        threading.Timer(C.AUTO_DELETE_BOT_MESSAGES, safe_delete, (chat_id, msg.message_id)).start()
    return msg


def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def mute(chat_id, user_id, minutes):
    until = int(time.time()) + minutes * 60
    bot.restrict_chat_member(chat_id, user_id, until_date=until,
                             permissions=types.ChatPermissions(can_send_messages=False))


def unmute(chat_id, user_id):
    # Возвращаем обычные права группы
    perms = bot.get_chat(chat_id).permissions or types.ChatPermissions(can_send_messages=True)
    bot.restrict_chat_member(chat_id, user_id, permissions=perms)


def kick(chat_id, user_id):
    """Удалить из группы, но разрешить вернуться позже."""
    bot.ban_chat_member(chat_id, user_id)
    bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


def minutes_text(m):
    if m % (24 * 60) == 0:
        d = m // (24 * 60)
        return f'{d} сут.'
    if m % 60 == 0:
        return f'{m // 60} ч.'
    return f'{m} мин.'


def add_warn(message, reason):
    chat_id, user = message.chat.id, message.from_user
    with lock:
        warns[(chat_id, user.id)] += 1
        n = warns[(chat_id, user.id)]
    if n >= C.WARNS_TO_MUTE:
        with lock:
            warns[(chat_id, user.id)] = 0
        try:
            mute(chat_id, user.id, C.WARN_MUTE_MINUTES)
            say(chat_id, f'🔇 {mention(user)} получает мут на {minutes_text(C.WARN_MUTE_MINUTES)}: '
                         f'{C.WARNS_TO_MUTE} предупреждения. Причина последнего: {reason}.')
        except Exception as e:
            print('Не удалось замутить:', e)
    else:
        say(chat_id, f'⚠️ {mention(user)}, предупреждение {n}/{C.WARNS_TO_MUTE}: {reason}.')


# ---------- Проверки сообщений ----------

def has_bad_link(message):
    text = message.text or message.caption or ''
    links = URL_RE.findall(text)
    for e in (message.entities or []) + (message.caption_entities or []):
        if e.type == 'text_link' and e.url:
            links.append(e.url)
    for link in links:
        if not any(domain in link.lower() for domain in C.ALLOWED_DOMAINS):
            return True
    return False


def has_bad_word(text):
    for word in WORD_RE.findall((text or '').lower().replace('ё', 'е')):
        for root in C.BAD_WORDS:
            root = root.replace('ё', 'е')
            # короткие корни ищем только в начале слова, чтобы не ловить «корабля», «рубля»
            if (len(root) >= 4 and root in word) or word.startswith(root):
                return True
    return False


def is_flood(chat_id, user_id):
    now = time.time()
    with lock:
        q = recent[(chat_id, user_id)]
        q.append(now)
        while q and now - q[0] > C.FLOOD_SECONDS:
            q.popleft()
        if len(q) > C.FLOOD_MESSAGES:
            q.clear()
            return True
    return False


# ---------- Новички и капча ----------

@bot.message_handler(content_types=['new_chat_members'])
def on_join(message):
    chat_id = message.chat.id
    if C.DELETE_SERVICE_MESSAGES:
        safe_delete(chat_id, message.message_id)
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        if not C.CAPTCHA_ENABLED:
            say(chat_id, f'👋 Добро пожаловать, {mention(user)}!\n\nПравила группы:\n{C.RULES}')
            continue
        try:
            bot.restrict_chat_member(chat_id, user.id,
                                     permissions=types.ChatPermissions(can_send_messages=False))
        except Exception as e:
            print('Нет прав ограничивать участников:', e)
            continue
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton('✅ Я не бот', callback_data=f'cap:{user.id}'))
        msg = say(chat_id, f'👋 {mention(user)}, добро пожаловать! Нажмите кнопку ниже в течение '
                           f'{C.CAPTCHA_SECONDS // 60} мин., чтобы писать в группе.',
                  autodelete=False, reply_markup=kb)
        timer = threading.Timer(C.CAPTCHA_SECONDS, captcha_timeout, (chat_id, user.id))
        with lock:
            captcha[(chat_id, user.id)] = (msg.message_id, timer)
        timer.start()


def captcha_timeout(chat_id, user_id):
    with lock:
        data = captcha.pop((chat_id, user_id), None)
    if not data:
        return
    safe_delete(chat_id, data[0])
    try:
        kick(chat_id, user_id)
        print(f'Капча не пройдена, пользователь {user_id} удалён')
    except Exception as e:
        print('Не удалось удалить участника:', e)


@bot.callback_query_handler(func=lambda c: c.data.startswith('cap:'))
def on_captcha(call):
    chat_id, user_id = call.message.chat.id, int(call.data.split(':')[1])
    if call.from_user.id != user_id:
        bot.answer_callback_query(call.id, 'Эта кнопка не для вас 🙂', show_alert=True)
        return
    with lock:
        data = captcha.pop((chat_id, user_id), None)
    if data:
        data[1].cancel()
    try:
        unmute(chat_id, user_id)
    except Exception as e:
        print('Не удалось снять ограничения:', e)
    bot.answer_callback_query(call.id, 'Спасибо! Теперь вы можете писать.')
    safe_delete(chat_id, call.message.message_id)
    say(chat_id, f'👋 Добро пожаловать, {mention(call.from_user)}!\n\nПравила группы:\n{C.RULES}')


@bot.message_handler(content_types=['left_chat_member'])
def on_left(message):
    if C.DELETE_SERVICE_MESSAGES:
        safe_delete(message.chat.id, message.message_id)


# ---------- Команды ----------

@bot.message_handler(commands=['start', 'help'])
def cmd_help(message):
    bot.reply_to(message,
                 'Я бот-администратор группы.\n\n'
                 'Для админов (ответом на сообщение нарушителя):\n'
                 '/warn — предупреждение\n/mute 30 — мут на 30 минут (без числа — на час)\n'
                 '/unmute — снять мут\n/ban — заблокировать\n/unban — разблокировать\n\n'
                 'Для всех: /rules — правила группы')


@bot.message_handler(commands=['rules'])
def cmd_rules(message):
    say(message.chat.id, f'📋 Правила группы:\n{C.RULES}')


@bot.message_handler(commands=['warn', 'mute', 'unmute', 'ban', 'unban'])
def cmd_admin(message):
    chat_id = message.chat.id
    if message.chat.type == 'private':
        bot.reply_to(message, 'Эти команды работают в группе.')
        return
    safe_delete(chat_id, message.message_id)
    if not from_admin(message):
        return
    target_msg = message.reply_to_message
    if not target_msg or not target_msg.from_user:
        say(chat_id, 'Ответьте командой на сообщение нарушителя.')
        return
    target = target_msg.from_user
    if is_admin(chat_id, target.id):
        say(chat_id, 'Это администратор — его нельзя наказать.')
        return
    cmd = message.text.split()[0].split('@')[0][1:]
    try:
        if cmd == 'warn':
            add_warn(target_msg, 'по решению администратора')
        elif cmd == 'mute':
            parts = message.text.split()
            minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 60
            mute(chat_id, target.id, minutes)
            say(chat_id, f'🔇 {mention(target)} — мут на {minutes_text(minutes)}')
        elif cmd == 'unmute':
            unmute(chat_id, target.id)
            with lock:
                warns[(chat_id, target.id)] = 0
            say(chat_id, f'🔊 {mention(target)} снова может писать')
        elif cmd == 'ban':
            bot.ban_chat_member(chat_id, target.id)
            safe_delete(chat_id, target_msg.message_id)
            say(chat_id, f'⛔ {mention(target)} заблокирован')
        elif cmd == 'unban':
            bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
            say(chat_id, f'✅ {mention(target)} разблокирован')
    except Exception as e:
        say(chat_id, 'Не получилось: проверьте, что у бота есть права администратора.')
        print('Ошибка команды', cmd, e)


# ---------- Все остальные сообщения ----------

@bot.message_handler(func=lambda m: m.chat.type in ('group', 'supergroup'),
                     content_types=['text', 'photo', 'video', 'document', 'audio', 'voice', 'sticker', 'animation'])
def on_message(message):
    if message.sender_chat or from_admin(message):
        return                                          # админов и каналы не проверяем
    chat_id, user_id = message.chat.id, message.from_user.id
    text = message.text or message.caption or ''

    if is_flood(chat_id, user_id):
        safe_delete(chat_id, message.message_id)
        try:
            mute(chat_id, user_id, C.FLOOD_MUTE_MINUTES)
            say(chat_id, f'🔇 {mention(message.from_user)} — мут на {minutes_text(C.FLOOD_MUTE_MINUTES)} за флуд')
        except Exception as e:
            print('Не удалось замутить за флуд:', e)
        return

    if C.DELETE_LINKS and has_bad_link(message):
        safe_delete(chat_id, message.message_id)
        add_warn(message, 'ссылки запрещены')
        return

    if C.FILTER_BAD_WORDS and has_bad_word(text):
        safe_delete(chat_id, message.message_id)
        add_warn(message, 'нецензурная лексика')


if __name__ == '__main__':
    print('Бот-администратор запущен. Остановить: Ctrl+C')
    bot.infinity_polling(skip_pending=True, allowed_updates=['message', 'callback_query'])
