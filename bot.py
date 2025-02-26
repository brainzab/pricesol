import requests
import time
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

# Состояния для ConversationHandler
ADDRESS, NAME, PERCENT = range(3)

# Хранилище токенов: {chat_id: {token_address: {"last_price": float, "percent": float, "last_market_cap": float, "name": str}}}
tracked_tokens = {}

# Временное хранилище данных во время добавления токена
temp_data = {}

def get_token_price(token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        response = requests.get(url, timeout=10)
        
        if response.status_code != 200:
            return {"error": f"Ошибка API: {response.status_code}"}
        
        data = response.json()
        
        if "pairs" in data and len(data["pairs"]) > 0:
            price_usd = float(data["pairs"][0]["priceUsd"])
            market_cap = float(data["pairs"][0]["fdv"])
            return {"price": price_usd, "market_cap": market_cap}
        else:
            return {"error": "Токен не найден на Dexscreener"}
    
    except Exception as e:
        return {"error": f"Ошибка: {str(e)}"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}  # Новый пользователь начинает с пустого словаря
    await update.message.reply_text(
        "Привет! Я бот для отслеживания цен токенов на Solana.\n"
        "Команды:\n"
        "/add <адрес_токена> - начать добавление токена\n"
        "/remove <адрес_токена> - убрать токен\n"
        "/list - показать список отслеживаемых токенов"
    )

async def add_token_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}  # Инициализируем пустой словарь для нового пользователя
    
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Используйте: /add <адрес_токена>\nПример: /add 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")
        return ConversationHandler.END
    
    token_address = args[0]
    result = get_token_price(token_address)
    
    if "error" in result:
        await update.message.reply_text(f"Не удалось найти токен: {result['error']}")
        return ConversationHandler.END
    
    temp_data["address"] = token_address
    temp_data["price"] = result["price"]
    temp_data["market_cap"] = result["market_cap"]
    temp_data["chat_id"] = chat_id  # Сохраняем chat_id для текущего пользователя
    
    await update.message.reply_text(
        f"Токен с адресом {token_address} найден.\n"
        f"Текущая цена: ${result['price']:.6f}\n"
        f"Текущий Market Cap: ${result['market_cap']:,.2f}\n"
        "Пожалуйста, введите название токена:"
    )
    return NAME

async def add_token_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_name = update.message.text.strip()
    temp_data["name"] = token_name
    
    await update.message.reply_text(f"Название '{token_name}' принято.\nПожалуйста, введите процент изменения цены (от 1 до 1000):")
    return PERCENT

async def add_token_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    percent_str = update.message.text.strip()
    try:
        percent = float(percent_str)
        if not (1 <= percent <= 1000):
            await update.message.reply_text("Процент должен быть от 1 до 1000. Попробуйте снова:")
            return PERCENT
    except ValueError:
        await update.message.reply_text("Процент должен быть числом. Попробуйте снова:")
        return PERCENT
    
    token_address = temp_data["address"]
    chat_id = temp_data["chat_id"]
    tracked_tokens[chat_id][token_address] = {
        "last_price": temp_data["price"],
        "percent": percent,
        "last_market_cap": temp_data["market_cap"],
        "name": temp_data["name"]
    }
    
    await update.message.reply_text(
        f"Токен '{temp_data['name']}' ({token_address}) добавлен.\n"
        f"Оповещение при изменении на {percent}%"
    )
    temp_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добавление токена отменено.")
    temp_data.clear()
    return ConversationHandler.END

async def remove_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Используйте: /remove <адрес_токена>")
        return
    
    token_address = args[0]
    if token_address in tracked_tokens[chat_id]:
        token_name = tracked_tokens[chat_id][token_address]["name"]
        del tracked_tokens[chat_id][token_address]
        await update.message.reply_text(f"Токен '{token_name}' ({token_address}) удалён из отслеживания")
    else:
        await update.message.reply_text("Токен не найден в вашем списке отслеживания")

async def list_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    if not tracked_tokens[chat_id]:
        await update.message.reply_text("Ваш список токенов пуст")
        return
    
    response = "Ваши отслеживаемые токены:\n"
    for token, data in tracked_tokens[chat_id].items():
        response += f"'{data['name']}' ({token}) - {data['percent']}%\n"
    await update.message.reply_text(response)

async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    for chat_id in tracked_tokens:
        for token_address, data in list(tracked_tokens[chat_id].items()):
            result = get_token_price(token_address)
            if "error" in result:
                await context.bot.send_message(chat_id=chat_id, text=f"Ошибка для '{data['name']}' ({token_address}): {result['error']}")
                continue
            
            current_price = result["price"]
            current_market_cap = result["market_cap"]
            last_price = data["last_price"]
            percent_change = abs((current_price - last_price) / last_price * 100)
            
            if percent_change >= data["percent"]:
                direction = "выросла" if current_price > last_price else "упала"
                emoji = "🟢" if current_price > last_price else "🟥"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{emoji} Цена токена '{data['name']}' {direction} на *{percent_change:.2f}*%!\n"
                         f"Цена: ${current_price:.6f}\n"
                         f"Market Cap: ${current_market_cap:,.2f}"
                )
                tracked_tokens[chat_id][token_address]["last_price"] = current_price
                tracked_tokens[chat_id][token_address]["last_market_cap"] = current_market_cap

def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
    
    application = Application.builder().token(bot_token).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_token_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_token_name)],
            PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_token_percent)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("remove", remove_token))
    application.add_handler(CommandHandler("list", list_tokens))
    
    application.job_queue.run_repeating(check_prices, interval=60, first=10)
    
    application.run_polling()

if __name__ == "__main__":
    main()
