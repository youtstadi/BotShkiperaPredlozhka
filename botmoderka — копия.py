import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import Message

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = '7841092270:AAFBONLecIcIxbRj2HA70mXpw-d7-t0P7YQ'
MODERATORS_CHAT_ID = -1003306963703
MAIN_GROUP_ID = -1002985913442
MAIN_GROUP_THREAD_ID = 17
MODERATORS = {7741825772, 5141491311}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pending_posts = {}

# ================== КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ==================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🚢 <b>Добро пожаловать в предложки Shkiper_online!</b>\n\n"
        "Присылай сюда:\n"
        "• Мемы (фото, GIF)\n"
        "• Видео/кружки\n"
        "• Идеи для стримов\n\n"
        "Модераторы всё увидят <b>анонимно</b> и лучшее опубликуют в группе!\n"
        "Используй /help для подробностей.",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📋 <b>Правила и как это работает:</b>\n\n"
        "1. Просто отправь сюда контент (не команду).\n"
        "2. Бот <b>скрывает твоё имя</b> и передаёт модераторам.\n"
        "3. Модераторы видят твою подпись, но не твой аккаунт.\n"
        "4. Если предложка одобрена — она появится в теме «❶ Мемы подписчиков».\n"
        "5. Рассмотрение занимает до 24 часов.\n\n"
        "❌ <b>Не приветствуется:</b> спам, NSFW, нарушения авторских прав.",
        parse_mode="HTML"
    )

# ================== ПРИЁМ КОНТЕНТА (АНОНИМНО) ==================
@dp.message(F.chat.type == 'private')
async def handle_user_content(message: Message):
    if message.text and message.text.startswith('/'):
        return

    await message.reply("✅ Принято! Модераторы рассмотрят твою предложку.")

    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="✅ Одобрить", callback_data=f"approve_{message.message_id}")
    keyboard.button(text="❌ Отклонить", callback_data=f"reject_{message.message_id}")
    keyboard.adjust(2)

    # Используем HTML для безопасного экранирования
    mod_caption = (
        f"📨 <b>Новая предложка</b>\n"
        f"ID отправителя: <code>{message.from_user.id}</code>\n"
        f"Юзернейм: @{message.from_user.username if message.from_user.username else 'нет'}\n"
        f"Тип: {message.content_type}"
    )

    try:
        sent_msg = None
        # Отправляем КОПИЮ контента с безопасной HTML-разметкой
        if message.photo:
            sent_msg = await bot.send_photo(
                chat_id=MODERATORS_CHAT_ID,
                photo=message.photo[-1].file_id,
                caption=mod_caption,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup()
            )
        elif message.video:
            sent_msg = await bot.send_video(
                chat_id=MODERATORS_CHAT_ID,
                video=message.video.file_id,
                caption=mod_caption,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup()
            )
        elif message.animation:
            sent_msg = await bot.send_animation(
                chat_id=MODERATORS_CHAT_ID,
                animation=message.animation.file_id,
                caption=mod_caption,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup()
            )
        elif message.document:
            sent_msg = await bot.send_document(
                chat_id=MODERATORS_CHAT_ID,
                document=message.document.file_id,
                caption=mod_caption,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup()
            )
        elif message.audio:
            sent_msg = await bot.send_audio(
                chat_id=MODERATORS_CHAT_ID,
                audio=message.audio.file_id,
                caption=mod_caption,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup()
            )
        elif message.text:
            # Для текста объединяем всё в одно сообщение
            full_text = f"{mod_caption}\n\n---\n{message.text}"
            sent_msg = await bot.send_message(
                chat_id=MODERATORS_CHAT_ID,
                text=full_text,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup()
            )
        else:
            await message.reply("⚠️ Этот тип контента пока не поддерживается.")
            return

        if sent_msg:
            pending_posts[message.message_id] = {
                'user_id': message.from_user.id,
                'original_message': message,
                'moderator_msg_id': sent_msg.message_id,
                'content_type': message.content_type
            }
            logging.info(f"Создана новая предложка. Ключ: {message.message_id}")

    except Exception as e:
        logging.error(f"Ошибка при отправке в чат модерации: {e}")
        await message.reply("⚠️ Произошла техническая ошибка. Попробуй позже.")

# ================== ОБРАБОТКА ОДОБРЕНИЯ ==================
@dp.callback_query(F.data.startswith("approve_"))
async def approve_post(callback: types.CallbackQuery):
    if callback.from_user.id not in MODERATORS:
        await callback.answer("❌ Ты не модератор!", show_alert=True)
        return

    original_msg_id = int(callback.data.split("_")[1])
    post_data = pending_posts.get(original_msg_id)

    if not post_data:
        await callback.answer("Предложка уже обработана или устарела.")
        return

    # Обновляем сообщение у модераторов
    try:
        await bot.edit_message_reply_markup(
            chat_id=MODERATORS_CHAT_ID,
            message_id=post_data['moderator_msg_id'],
            reply_markup=None
        )
        # Добавляем пометку об одобрении
        new_caption = (
            f"{callback.message.text or callback.message.caption or ''}\n\n"
            f"✅ <b>Одобрено</b> @{callback.from_user.username}"
        )
        if callback.message.content_type == 'text':
            await bot.edit_message_text(
                chat_id=MODERATORS_CHAT_ID,
                message_id=post_data['moderator_msg_id'],
                text=new_caption,
                parse_mode="HTML"
            )
        else:
            await bot.edit_message_caption(
                chat_id=MODERATORS_CHAT_ID,
                message_id=post_data['moderator_msg_id'],
                caption=new_caption,
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения модерации: {e}")

    # Публикуем в группе (анонимно)
    try:
        msg = post_data['original_message']
        caption = f"✅ <b>Одобрено модерацией!</b>\nПредложил(а): аноним"
        
        if msg.photo:
            await bot.send_photo(
                chat_id=MAIN_GROUP_ID,
                message_thread_id=MAIN_GROUP_THREAD_ID,
                photo=msg.photo[-1].file_id,
                caption=caption,
                parse_mode="HTML"
            )
        elif msg.video:
            await bot.send_video(
                chat_id=MAIN_GROUP_ID,
                message_thread_id=MAIN_GROUP_THREAD_ID,
                video=msg.video.file_id,
                caption=caption,
                parse_mode="HTML"
            )
        elif msg.animation:
            await bot.send_animation(
                chat_id=MAIN_GROUP_ID,
                message_thread_id=MAIN_GROUP_THREAD_ID,
                animation=msg.animation.file_id,
                caption=caption,
                parse_mode="HTML"
            )
        elif msg.document:
            await bot.send_document(
                chat_id=MAIN_GROUP_ID,
                message_thread_id=MAIN_GROUP_THREAD_ID,
                document=msg.document.file_id,
                caption=caption,
                parse_mode="HTML"
            )
        elif msg.audio:
            await bot.send_audio(
                chat_id=MAIN_GROUP_ID,
                message_thread_id=MAIN_GROUP_THREAD_ID,
                audio=msg.audio.file_id,
                caption=caption,
                parse_mode="HTML"
            )
        elif msg.text:
            await bot.send_message(
                chat_id=MAIN_GROUP_ID,
                message_thread_id=MAIN_GROUP_THREAD_ID,
                text=f"✅ <b>Одобрено модерацией!</b>\n\n{msg.text}\n\nПредложил(а): аноним",
                parse_mode="HTML"
            )
        
        await callback.answer("✅ Предложка одобрена и опубликована!")
        logging.info(f"Предложка {original_msg_id} опубликована.")

    except Exception as e:
        logging.error(f"Ошибка при публикации в группу: {e}")
        await callback.answer("⚠️ Ошибка при публикации в группу.", show_alert=True)

    # Удаляем из ожидающих
    if original_msg_id in pending_posts:
        del pending_posts[original_msg_id]

# ================== ОБРАБОТКА ОТКЛОНЕНИЯ ==================
@dp.callback_query(F.data.startswith("reject_"))
async def reject_post(callback: types.CallbackQuery):
    if callback.from_user.id not in MODERATORS:
        await callback.answer("❌ Ты не модератор!", show_alert=True)
        return

    original_msg_id = int(callback.data.split("_")[1])
    post_data = pending_posts.get(original_msg_id)

    if not post_data:
        await callback.answer("Предложка уже обработана.")
        return

    # Обновляем сообщение у модераторов
    try:
        await bot.edit_message_reply_markup(
            chat_id=MODERATORS_CHAT_ID,
            message_id=post_data['moderator_msg_id'],
            reply_markup=None
        )
        
        new_caption = (
            f"{callback.message.text or callback.message.caption or ''}\n\n"
            f"❌ <b>Отклонено</b> @{callback.from_user.username}"
        )
        
        if callback.message.content_type == 'text':
            await bot.edit_message_text(
                chat_id=MODERATORS_CHAT_ID,
                message_id=post_data['moderator_msg_id'],
                text=new_caption,
                parse_mode="HTML"
            )
        else:
            await bot.edit_message_caption(
                chat_id=MODERATORS_CHAT_ID,
                message_id=post_data['moderator_msg_id'],
                caption=new_caption,
                parse_mode="HTML"
            )
        
        await callback.answer("❌ Предложка отклонена.")
        logging.info(f"Предложка {original_msg_id} отклонена.")

    except Exception as e:
        logging.error(f"Ошибка при отклонении: {e}")
        await callback.answer("⚠️ Ошибка при обработке.", show_alert=True)

    # Удаляем из ожидающих
    if original_msg_id in pending_posts:
        del pending_posts[original_msg_id]

# ================== ЗАПУСК ==================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    print("🤖 Бот запущен и готов к работе!")
    print(f"ID чата модерации: {MODERATORS_CHAT_ID}")
    print(f"ID основной группы: {MAIN_GROUP_ID}")
    print(f"ID темы: {MAIN_GROUP_THREAD_ID}")
    print(f"Модераторы: {MODERATORS}")
    print("=" * 50)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())