import requests
import time
import os
import asyncio
import sqlite3
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# Состояния для ConversationHandler
ADDRESS, NAME, PERCENT, EDIT_ADDRESS, EDIT_PERCENT = range(5)

# Путь к базе данных SQLite
DB_PATH = "tokens.db"

# Хранилище токенов: загружается из и сохраняется в SQLite (глобальная переменная для совместимости с текущим кодом)
tracked_tokens = {}

# Временное хранилище данных во время добавления или редактирования токена
temp_data = {}

# Кэш для данных токенов: загружается из и сохраняется в SQLite
cache = {}
CACHE_TIMEOUT = 300  # 5 минут в секундах

# Лимит токенов на пользователя
MAX_TOKENS_PER_USER = 50

# ID администратора для уведомлений о сбоях (задайте через переменную окружения ADMIN_CHAT_ID)
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Добавьте в Railway переменную окружения

def format_number(value, is_price=False):
    """Форматирует большие числа в сокращённый вид только для Market Cap."""
    if is_price:  # Для цены всегда полный формат
        return f"${value:,.6f}"
    elif isinstance(value, float) and value >= 1000000:  # Для Market Cap сокращаем большие числа
        return f"${value / 1000000:.2f}M"
    return f"${value:,.2f}"

def init_db():
    """Инициализирует базу данных SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Таблица для отслеживаемых токенов
    cursor.execute('''CREATE TABLE IF NOT EXISTS tracked_tokens
                     (chat_id INTEGER, token_address TEXT, last_price REAL, percent REAL, 
                      last_market_cap REAL, name TEXT, timestamp REAL, PRIMARY KEY (chat_id, token_address))''')
    
    # Таблица для кэша данных токенов
    cursor.execute('''CREATE TABLE IF NOT EXISTS token_cache
                     (token_address TEXT, price REAL, market_cap REAL, price_change_24h REAL, 
                      timestamp REAL, PRIMARY KEY (token_address))''')
    
    conn.commit()
    conn.close()

def load_tracked_tokens():
    """Загружает отслеживаемые токены из базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, token_address, last_price, percent, last_market_cap, name, timestamp FROM tracked_tokens")
    rows = cursor.fetchall()
    tracked_tokens = {}
    for row in rows:
        chat_id, token_address, last_price, percent, last_market_cap, name, timestamp = row
        if chat_id not in tracked_tokens:
            tracked_tokens[chat_id] = {}
        tracked_tokens[chat_id][token_address] = {
            "last_price": last_price,
            "percent": percent,
            "last_market_cap": last_market_cap,
            "name": name
        }
    conn.close()
    return tracked_tokens

def save_tracked_tokens():
    """Сохраняет отслеживаемые токены в базу данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tracked_tokens")
    for chat_id, tokens in tracked_tokens.items():
        for token_address, data in tokens.items():
            cursor.execute(
                "INSERT INTO tracked_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, token_address, data["last_price"], data["percent"],
                 data["last_market_cap"], data["name"], time.time())
            )
    conn.commit()
    conn.close()

def load_cache():
    """Загружает кэш токенов из базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT token_address, price, market_cap, price_change_24h, timestamp FROM token_cache")
    rows = cursor.fetchall()
    cache = {}
    for row in rows:
        token_address, price, market_cap, price_change_24h, timestamp = row
        cache[token_address] = {
            "data": {"price": price, "market_cap": market_cap, "price_change_24h": price_change_24h},
            "timestamp": timestamp
        }
    conn.close()
    return cache

def save_cache():
    """Сохраняет кэш токенов в базу данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM token_cache")
    for token_address, data in cache.items():
        cursor.execute(
            "INSERT INTO token_cache VALUES (?, ?, ?, ?, ?)",
            (token_address, data["data"]["price"], data["data"]["market_cap"],
             data["data"]["price_change_24h"], data["timestamp"])
        )
    conn.commit()
    conn.close()

def get_token_price(token_address):
    """Получение данных о токене с кэшированием из базы данных."""
    current_time = time.time()
    if token_address in cache and (current_time - cache[token_address]["timestamp"]) < CACHE_TIMEOUT:
        return cache[token_address]["data"]
    
    session = requests.Session()
    retry_strategy = Retry(
        total=3,  # Количество попыток
        backoff_factor=1,  # Задержка между попытками (1 сек, 2 сек, 4 сек)
        status_forcelist=[429, 500, 502, 503, 504],  # Коды ошибок для повторных попыток
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        response = session.get(url, timeout=15)
        
        if response.status_code != 200:
            return {"error": f"Ошибка API: {response.status_code}"}
        
        data = response.json()
        
        # Проверка на None или некорректный формат данных
        if data is None or not isinstance(data, dict):
            return {"error": "Неверный формат ответа от API"}
        
        if "pairs" not in data or not data["pairs"] or len(data["pairs"]) == 0:
            return {"error": "Токен не найден на Dexscreener"}
        
        pair = data["pairs"][0]
        price_usd = float(pair["priceUsd"])
        market_cap = float(pair["fdv"])
        price_change_24h = pair.get("priceChange", {}).get("h24", "N/A")
        if price_change_24h != "N/A":
            price_change_24h = float(price_change_24h)
        result = {"price": price_usd, "market_cap": market_cap, "price_change_24h": price_change_24h}
        cache[token_address] = {"data": result, "timestamp": current_time}
        save_cache()
        return result
    
    except requests.exceptions.ReadTimeout:
        return {"error": "Тайм-аут соединения с API Dexscreener"}
    except (ValueError, KeyError, TypeError) as e:
        return {"error": f"Неверный адрес токена или ошибка данных: {str(e)}"}
    except Exception as e:
        return {"error": f"Ошибка: {str(e)}"}

async def async_get_token_price(token_address):
    """Асинхронная обёртка для get_token_price."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_token_price, token_address)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    await update.message.reply_text(
        "👋 <b>Привет!</b> Я бот для отслеживания цен токенов на Solana.\n"
        "\n"
        "<b>Команды:</b>\n"
        "<b>/add</b> <i>адрес_токена</i> — начать добавление токена\n"
        "<b>/remove</b> <i>адрес_токена</i> — убрать токен\n"
        "<b>/remove all</b> — очистить все отслеживаемые токены\n"
        "<b>/edit</b> <i>адрес_токена</i> — изменить процент отслеживания\n"
        "<b>/list</b> — показать список отслеживаемых токенов\n"
        "<b>/stats</b> — показать статистику токенов",
        parse_mode="HTML"
    )

async def add_token_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    if len(tracked_tokens[chat_id]) >= MAX_TOKENS_PER_USER:
        await update.message.reply_text(
            f"❌ Достигнут лимит в <b>{MAX_TOKENS_PER_USER}</b> токенов. Удалите существующие токены, чтобы добавить новые.",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Используйте: <b>/add</b> <i>адрес_токена</i>\n"
            "Пример: <b>/add</b> <i>7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU</i>",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    
    token_address = args[0]
    result = get_token_price(token_address)
    
    if "error" in result:
        if "Неверный адрес токена" in result["error"]:
            await update.message.reply_text(
                "❌ Неверный адрес токена. Убедитесь, что вы ввели корректный адрес токена на Solana и попробуйте снова.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"❌ Не удалось найти токен: <i>{result['error']}</i>",
                parse_mode="HTML"
            )
        if ADMIN_CHAT_ID and "Тайм-аут" in result["error"]:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Тайм-аут при запросе токена <code>{token_address}</code>",
                parse_mode="HTML"
            )
        return ConversationHandler.END
    
    temp_data["address"] = token_address
    temp_data["price"] = result["price"]
    temp_data["market_cap"] = result["market_cap"]
    temp_data["chat_id"] = chat_id
    
    await update.message.reply_text(
        f"✅ Токен с адресом <code>{token_address}</code> найден.\n"
        f"Текущая цена: <b>{format_number(result['price'], is_price=True)}</b>\n"
        f"Текущий Market Cap: <b>{format_number(result['market_cap'])}</b>\n"
        "Пожалуйста, введите <b>название токена</b>:",
        parse_mode="HTML"
    )
    return NAME

async def add_token_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_name = update.message.text.strip()
    temp_data["name"] = token_name
    
    await update.message.reply_text(
        f"✅ Название <b>{token_name}</b> принято.\n"
        "Пожалуйста, введите <b>процент изменения цены</b> (от 1 до 1000):",
        parse_mode="HTML"
    )
    return PERCENT

async def add_token_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    percent_str = update.message.text.strip()
    try:
        percent = float(percent_str)
        if not (1 <= percent <= 1000):
            await update.message.reply_text(
                "❌ Процент должен быть от <b>1</b> до <b>1000</b>. Попробуйте снова:",
                parse_mode="HTML"
            )
            return PERCENT
    except ValueError:
        await update.message.reply_text(
            "❌ Процент должен быть <b>числом</b>. Попробуйте снова:",
            parse_mode="HTML"
        )
        return PERCENT
    
    token_address = temp_data["address"]
    chat_id = temp_data["chat_id"]
    tracked_tokens[chat_id][token_address] = {
        "last_price": temp_data["price"],
        "percent": percent,
        "last_market_cap": temp_data["market_cap"],
        "name": temp_data["name"]
    }
    save_tracked_tokens()
    
    await update.message.reply_text(
        f"✅ Токен <b>{temp_data['name']}</b> (<code>{token_address}</code>) добавлен.\n"
        f"Оповещение при изменении на <b>{percent}%</b>",
        parse_mode="HTML"
    )
    temp_data.clear()
    return ConversationHandler.END

async def edit_token_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Используйте: <b>/edit</b> <i>адрес_токена</i>\n"
            "Пример: <b>/edit</b> <i>7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU</i>",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    
    token_address = args[0]
    if token_address not in tracked_tokens[chat_id]:
        await update.message.reply_text(
            f"❌ Токен с адресом <code>{token_address}</code> не найден в вашем списке отслеживания",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    
    temp_data["address"] = token_address
    temp_data["chat_id"] = chat_id
    current_percent = tracked_tokens[chat_id][token_address]["percent"]
    token_name = tracked_tokens[chat_id][token_address]["name"]
    
    await update.message.reply_text(
        f"✅ Токен <b>{token_name}</b> (<code>{token_address}</code>) найден.\n"
        f"Текущий процент отслеживания: <b>{current_percent}%</b>\n"
        "На какой процент изменить (от 1 до 1000)?",
        parse_mode="HTML"
    )
    return EDIT_PERCENT

async def edit_token_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    percent_str = update.message.text.strip()
    try:
        percent = float(percent_str)
        if not (1 <= percent <= 1000):
            await update.message.reply_text(
                "❌ Процент должен быть от <b>1</b> до <b>1000</b>. Попробуйте снова:",
                parse_mode="HTML"
            )
            return EDIT_PERCENT
    except ValueError:
        await update.message.reply_text(
            "❌ Процент должен быть <b>числом</b>. Попробуйте снова:",
            parse_mode="HTML"
        )
        return EDIT_PERCENT
    
    token_address = temp_data["address"]
    chat_id = temp_data["chat_id"]
    token_name = tracked_tokens[chat_id][token_address]["name"]
    tracked_tokens[chat_id][token_address]["percent"] = percent
    save_tracked_tokens()
    
    await update.message.reply_text(
        f"✅ Процент отслеживания для токена <b>{token_name}</b> (<code>{token_address}</code>) изменён на <b>{percent}%</b>",
        parse_mode="HTML"
    )
    temp_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Добавление или редактирование токена <b>отменено</b>.", parse_mode="HTML")
    temp_data.clear()
    return ConversationHandler.END

async def remove_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Используйте: <b>/remove</b> <i>адрес_токена</i> или <b>/remove all</b> для очистки всех токенов",
            parse_mode="HTML"
        )
        return
    
    token_address = args[0]
    if token_address.lower() == "all":
        if tracked_tokens[chat_id]:
            tracked_tokens[chat_id].clear()
            save_tracked_tokens()
            await update.message.reply_text(
                "✅ Все отслеживаемые токены удалены.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "❌ У вас нет отслеживаемых токенов для очистки.",
                parse_mode="HTML"
            )
    elif token_address in tracked_tokens[chat_id]:
        token_name = tracked_tokens[chat_id][token_address]["name"]
        del tracked_tokens[chat_id][token_address]
        save_tracked_tokens()
        await update.message.reply_text(
            f"✅ Токен <b>{token_name}</b> (<code>{token_address}</code>) удалён из отслеживания",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "❌ Токен не найден в вашем списке отслеживания",
            parse_mode="HTML"
        )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    if not tracked_tokens[chat_id]:
        await update.message.reply_text(
            "📊 <b>Статистика:</b>\n"
            "У вас нет отслеживаемых токенов.",
            parse_mode="HTML"
        )
        return
    
    token_count = len(tracked_tokens[chat_id])
    total_change_24h = 0
    valid_tokens = 0
    
    # Асинхронно запрашиваем данные для всех токенов
    tasks = [async_get_token_price(token) for token in tracked_tokens[chat_id]]
    results = await asyncio.gather(*tasks)
    
    for result in results:
        if "error" not in result and result["price_change_24h"] != "N/A":
            total_change_24h += result["price_change_24h"]
            valid_tokens += 1
    
    avg_change_24h = total_change_24h / valid_tokens if valid_tokens > 0 else 0
    emoji_avg = "🟢" if avg_change_24h > 0 else "🔴" if avg_change_24h < 0 else ""
    
    await update.message.reply_text(
        f"📊 <b>Статистика:</b>\n"
        f"Токенов отслеживается: <b>{token_count}</b>\n"
        f"Среднее изменение за 24ч: {emoji_avg} <b>{avg_change_24h:.2f}%</b>",
        parse_mode="HTML"
    )

async def list_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    if not tracked_tokens[chat_id]:
        await update.message.reply_text(
            "📋 Ваш список токенов <b>пуст</b>",
            parse_mode="HTML"
        )
        return
    
    response = "📋 <b>Ваши отслеживаемые токены:</b>\n\n"
    
    # Асинхронно запрашиваем данные для всех токенов
    tasks = [async_get_token_price(token) for token in tracked_tokens[chat_id]]
    results = await asyncio.gather(*tasks)
    
    for token, data, result in zip(tracked_tokens[chat_id].keys(), tracked_tokens[chat_id].values(), results):
        if "error" in result:
            price_change_24h = "N/A"
            emoji_24h = ""
            price = "N/A"
            market_cap = "N/A"
            if ADMIN_CHAT_ID and "Тайм-аут" in result["error"]:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"⚠️ Тайм-аут при запросе токена <code>{token}</code> в /list",
                    parse_mode="HTML"
                )
        else:
            price_change_24h = result["price_change_24h"]
            emoji_24h = "🟢" if price_change_24h > 0 else "🔴" if price_change_24h < 0 else ""
            price = result["price"]
            market_cap = result["market_cap"]
        
        dexscreener_url = f"https://dexscreener.com/solana/{token}"
        response += (f"<b>{data['name']}</b> (<code>{token}</code>)\n"
                     f"Оповещение: <b>{data['percent']}%</b>\n"
                     f"Изменение за 24ч: {emoji_24h} <b>{price_change_24h}%</b>\n"
                     f"Цена: <b>{format_number(price, is_price=True)}</b> | Market Cap: <b>{format_number(market_cap)}</b>\n"
                     f"<a href='{dexscreener_url}'><i>Чарт на Dexscreener</i></a>\n\n")
    await update.message.reply_text(response, parse_mode="HTML", disable_web_page_preview=True)

async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in tracked_tokens:
        for token_address, data in list(tracked_tokens[chat_id].items()):
            result = get_token_price(token_address)
            if "error" in result:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Ошибка для <b>{data['name']}</b> (<code>{token_address}</code>): <i>{result['error']}</i>",
                    parse_mode="HTML"
                )
                if ADMIN_CHAT_ID and "Тайм-аут" in result["error"]:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=f"⚠️ Тайм-аут при проверке токена <code>{token_address}</code>",
                        parse_mode="HTML"
                    )
                continue
            
            current_price = result["price"]
            current_market_cap = result["market_cap"]
            last_price = data["last_price"]
            percent_change = abs((current_price - last_price) / last_price * 100)
            
            if percent_change >= data["percent"]:
                direction = "выросла" if current_price > last_price else "упала"
                emoji = "🟢" if current_price > last_price else "🔴"
                dexscreener_url = f"https://dexscreener.com/solana/{token_address}"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{emoji} Цена токена <b>{data['name']}</b> {direction} на <b>{percent_change:.2f}%</b>!\n"
                         f"Цена: <b>{format_number(current_price, is_price=True)}</b>\n"
                         f"Market Cap: <b>{format_number(current_market_cap)}</b>\n\n"
                         f"<a href='{dexscreener_url}'><i>Чарт на Dexscreener</i></a>",
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                tracked_tokens[chat_id][token_address]["last_price"] = current_price
                tracked_tokens[chat_id][token_address]["last_market_cap"] = current_market_cap
                save_tracked_tokens()

def main():
    # Инициализация и загрузка данных из базы данных
    init_db()
    global tracked_tokens, cache
    tracked_tokens = load_tracked_tokens()
    cache = load_cache()
    
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
    
    application = Application.builder().token(bot_token).build()
    
    # Обработчик для добавления токенов
    add_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_token_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_token_name)],
            PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_token_percent)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    # Обработчик для редактирования процентов
    edit_handler = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_token_start)],
        states={
            EDIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_token_percent)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    application.add_handler(add_handler)
    application.add_handler(edit_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remove", remove_token))
    application.add_handler(CommandHandler("list", list_tokens))
    application.add_handler(CommandHandler("stats", stats))
    
    application.job_queue.run_repeating(check_prices, interval=60, first=10)
    
    # Сохраняем данные при завершении работы
    try:
        application.run_polling()
    finally:
        save_tracked_tokens()
        save_cache()

if __name__ == "__main__":
    main()
