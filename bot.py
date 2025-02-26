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
            price_change_24h = float(data["pairs"][0]["priceChange"]["h24"])
            return {"price": price_usd, "market_cap": market_cap, "price_change_24h": price_change_24h}
        else:
            return {"error": "Токен не найден на Dexscreener"}
    
    except Exception as e:
        return {"error": f"Ошибка: {str(e)}"}

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
        "<b>/list</b> — показать список отслеживаемых токенов",
        parse_mode="HTML"
    )

async def add_token_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Используйте: <b>/add</b> <i>адрес_токена</i>\n"  # Убран <code>, команды жирные, адрес_токена курсивом
            "Пример: <b>/add</b> <i>7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU</i>",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    
    token_address = args[0]
    result = get_token_price(token_address)
    
    if "error" in result:
        await update.message.reply_text(f"❌ Не удалось найти токен: <i>{result['error']}</i>", parse_mode="HTML")
        return ConversationHandler.END
    
    temp_data["address"] = token_address
    temp_data["price"] = result["price"]
    temp_data["market_cap"] = result["market_cap"]
    temp_data["chat_id"] = chat_id
    
    await update.message.reply_text(
        f"✅ Токен с адресом <code>{token_address}</code> найден.\n"
        f"Текущая цена: <b>${result['price']:.6f}</b>\n"
        f"Текущий Market Cap: <b>${result['market_cap']:,.2f}</b>\n"
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
    
    await update.message.reply_text(
        f"✅ Токен <b>{temp_data['name']}</b> (<code>{token_address}</code>) добавлен.\n"
        f"Оповещение при изменении на <b>{percent}%</b>",
        parse_mode="HTML"
    )
    temp_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Добавление токена <b>отменено</b>.", parse_mode="HTML")
    temp_data.clear()
    return ConversationHandler.END

async def remove_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in tracked_tokens:
        tracked_tokens[chat_id] = {}
    
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Используйте: <b>/remove</b> <i>адрес_токена</i>",  # Убран <code>, команда жирная, адрес_токена курсивом
            parse_mode="HTML"
        )
        return
    
    token_address = args[0]
    if token_address in tracked_tokens[chat_id]:
        token_name = tracked_tokens[chat_id][token_address]["name"]
        del tracked_tokens[chat_id][token_address]
        await update.message.reply_text(
            f"✅ Токен <b>{token_name}</b> (<code>{token_address}</code>) удалён из отслеживания",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "❌ Токен не найден в вашем списке отслеживания",
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
    for token, data in tracked_tokens[chat_id].items():
        result = get_token_price(token)
        if "error" in result:
            price_change_24h = "N/A"
            emoji_24h = ""
        else:
            price_change_24h = result["price_change_24h"]
            emoji_24h = "🟢" if price_change_24h > 0 else "🔴" if price_change_24h < 0 else ""
        
        dexscreener_url = f"https://dexscreener.com/solana/{token}"
        response += (f"<b>{data['name']}</b> (<code>{token}</code>)\n"
                     f"Оповещение: <b>{data['percent']}%</b>\n"
                     f"Изменение за 24ч: {emoji_24h} <b>{price_change_24h}%</b>\n"
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
                         f"Цена: <b>${current_price:.6f}</b>\n"
                         f"Market Cap: <b>${current_market_cap:,.2f}</b>\n\n"
                         f"<a href='{dexscreener_url}'><i>Чарт на Dexscreener</i></a>",
                    parse_mode="HTML",
                    disable_web_page_preview=True
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
