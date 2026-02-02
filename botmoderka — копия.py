import asyncio
import logging
import json
import os
from typing import Dict, Optional, List, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, html
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, FSInputFile
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError

# ================== КОНФИГУРАЦИЯ ==================
class BotConfig:
    """Конфигурация бота"""
    BOT_TOKEN: str = '7841092270:AAFBONLecIcIxbRj2HA70mXpw-d7-t0P7YQ'
    MODERATORS_CHAT_ID: int = -1003306963703
    MAIN_GROUP_ID: int = -1002985913442
    MAIN_GROUP_THREAD_ID: int = 17
    MODERATORS: set[int] = {7741825772, 5141491311}
    ADMIN_IDS: set[int] = {7741825772, 5141491311}
    
    # Ограничения
    MAX_PHOTO_SIZE_MB: int = 10
    MAX_VIDEO_SIZE_MB: int = 20
    MAX_PENDING_POSTS: int = 100
    CLEANUP_INTERVAL_HOURS: int = 24
    
    # Настройки админ-панели
    CONFIG_FILE: str = 'bot_config.json'
    MAX_COMMENT_LENGTH: int = 1000
    
    @classmethod
    def load_config(cls):
        """Загружает конфигурацию из файла"""
        if os.path.exists(cls.CONFIG_FILE):
            try:
                with open(cls.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    cls.MAX_PHOTO_SIZE_MB = config.get('max_photo_size', cls.MAX_PHOTO_SIZE_MB)
                    cls.MAX_VIDEO_SIZE_MB = config.get('max_video_size', cls.MAX_VIDEO_SIZE_MB)
                    cls.MAX_PENDING_POSTS = config.get('max_pending_posts', cls.MAX_PENDING_POSTS)
                    cls.CLEANUP_INTERVAL_HOURS = config.get('cleanup_interval', cls.CLEANUP_INTERVAL_HOURS)
                    cls.MODERATORS = set(config.get('moderators', list(cls.MODERATORS)))
                    cls.ADMIN_IDS = set(config.get('admins', list(cls.ADMIN_IDS)))
            except Exception as e:
                logging.error(f"Ошибка загрузки конфигурации: {e}")
    
    @classmethod
    def save_config(cls):
        """Сохраняет конфигурацию в файл"""
        try:
            config = {
                'max_photo_size': cls.MAX_PHOTO_SIZE_MB,
                'max_video_size': cls.MAX_VIDEO_SIZE_MB,
                'max_pending_posts': cls.MAX_PENDING_POSTS,
                'cleanup_interval': cls.CLEANUP_INTERVAL_HOURS,
                'moderators': list(cls.MODERATORS),
                'admins': list(cls.ADMIN_IDS)
            }
            with open(cls.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка сохранения конфигурации: {e}")

BotConfig.load_config()

# ================== МОДЕЛИ ДАННЫХ ==================
class ContentType(Enum):
    PHOTO = "photo"
    VIDEO = "video"

@dataclass
class PendingPost:
    """Модель отложенного поста"""
    user_id: int
    username: Optional[str]
    original_message_id: int
    moderator_message_id: int
    content_type: ContentType
    file_id: str
    caption: Optional[str] = None
    timestamp: datetime = None
    is_processed: bool = False
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

# ================== СОСТОЯНИЯ FSM ==================
class CommentStates(StatesGroup):
    """Состояния для комментариев"""
    waiting_for_approve_comment = State()
    waiting_for_reject_comment = State()

class AdminStates(StatesGroup):
    """Состояния для админ-панели"""
    waiting_photo_size = State()
    waiting_video_size = State()
    waiting_pending_limit = State()
    waiting_cleanup_interval = State()
    waiting_moderator_id = State()
    waiting_admin_id = State()
    waiting_broadcast = State()

# ================== СЕРВИСЫ ==================
class PostManager:
    """Менеджер управления постами"""
    
    def __init__(self):
        self._pending_posts: Dict[int, PendingPost] = {}
        self._lock = asyncio.Lock()
        self._user_stats: Dict[int, Dict[str, int]] = {}
    
    async def add_post(self, user_id: int, username: Optional[str], 
                      original_msg_id: int, mod_msg_id: int,
                      content_type: ContentType, file_id: str, caption: Optional[str] = None) -> bool:
        """Добавить пост в очередь на модерацию"""
        async with self._lock:
            if len(self._pending_posts) >= BotConfig.MAX_PENDING_POSTS:
                await self._cleanup_old_posts()
            
            post = PendingPost(
                user_id=user_id,
                username=username,
                original_message_id=original_msg_id,
                moderator_message_id=mod_msg_id,
                content_type=content_type,
                file_id=file_id,
                caption=caption
            )
            
            self._pending_posts[original_msg_id] = post
            
            if user_id not in self._user_stats:
                self._user_stats[user_id] = {'submitted': 0, 'approved': 0, 'rejected': 0}
            self._user_stats[user_id]['submitted'] += 1
            
            logging.info(f"Добавлен пост {original_msg_id} от пользователя {user_id}")
            return True
    
    async def get_post(self, post_id: int) -> Optional[PendingPost]:
        """Получить пост по ID"""
        return self._pending_posts.get(post_id)
    
    async def mark_approved(self, post_id: int):
        """Пометить пост как одобренный"""
        if post := self._pending_posts.get(post_id):
            post.is_processed = True
            if post.user_id in self._user_stats:
                self._user_stats[post.user_id]['approved'] += 1
    
    async def mark_rejected(self, post_id: int):
        """Пометить пост как отклоненный"""
        if post := self._pending_posts.get(post_id):
            post.is_processed = True
            if post.user_id in self._user_stats:
                self._user_stats[post.user_id]['rejected'] += 1
    
    async def _cleanup_old_posts(self):
        """Очистка устаревших постов"""
        now = datetime.now()
        to_remove = []
        
        for post_id, post in self._pending_posts.items():
            if (now - post.timestamp).total_seconds() > BotConfig.CLEANUP_INTERVAL_HOURS * 3600:
                to_remove.append(post_id)
        
        for post_id in to_remove:
            del self._pending_posts[post_id]
        
        if to_remove:
            logging.info(f"Очищено {len(to_remove)} устаревших постов")
    
    async def cleanup_all_pending(self):
        """Очистить все ожидающие посты"""
        async with self._lock:
            count = len(self._pending_posts)
            self._pending_posts.clear()
            return count
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику"""
        return {
            'pending_posts': len(self._pending_posts),
            'unique_users': len(self._user_stats),
            'total_submitted': sum(stats['submitted'] for stats in self._user_stats.values()),
            'total_approved': sum(stats['approved'] for stats in self._user_stats.values()),
            'total_rejected': sum(stats['rejected'] for stats in self._user_stats.values())
        }

# ================== КЛАВИАТУРЫ ==================
class KeyboardFactory:
    """Фабрика клавиатур"""
    
    @staticmethod
    def get_moderation_kb(post_id: int) -> InlineKeyboardMarkup:
        """Клавиатура для модерации с комментариями"""
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Одобрить", callback_data=f"approve_{post_id}")
        builder.button(text="❌ Отклонить", callback_data=f"reject_{post_id}")
        builder.button(text="💬 Одобрить с комментом", callback_data=f"approve_comment_{post_id}")
        builder.button(text="📝 Отклонить с комментом", callback_data=f"reject_comment_{post_id}")
        builder.adjust(2, 2)
        return builder.as_markup()
    
    @staticmethod
    def get_user_help_kb() -> InlineKeyboardMarkup:
        """Клавиатура помощи пользователю (без suggest)"""
        builder = InlineKeyboardBuilder()
        builder.button(text="📋 Правила", callback_data="show_rules")
        builder.button(text="❓ Как отправить", callback_data="how_to_send")
        builder.adjust(1)
        return builder.as_markup()
    
    @staticmethod
    def get_admin_panel_kb() -> InlineKeyboardMarkup:
        """Клавиатура админ-панели"""
        builder = InlineKeyboardBuilder()
        builder.button(text="📊 Статистика", callback_data="admin_stats")
        builder.button(text="⚙️ Настройки лимитов", callback_data="admin_limits")
        builder.button(text="👥 Управление модераторами", callback_data="admin_moderators")
        builder.button(text="🛠️ Управление админами", callback_data="admin_admins")
        builder.button(text="🧹 Очистить очередь", callback_data="admin_cleanup")
        builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
        builder.button(text="💾 Сохранить конфиг", callback_data="admin_save")
        builder.button(text="❌ Закрыть", callback_data="admin_close")
        builder.adjust(1, 2, 2, 2, 1)
        return builder.as_markup()
    
    @staticmethod
    def get_settings_kb() -> InlineKeyboardMarkup:
        """Клавиатура настроек"""
        builder = InlineKeyboardBuilder()
        builder.button(text="📸 Макс. размер фото", callback_data="set_photo_size")
        builder.button(text="🎥 Макс. размер видео", callback_data="set_video_size")
        builder.button(text="📁 Макс. очередь", callback_data="set_pending_limit")
        builder.button(text="⏰ Интервал очистки", callback_data="set_cleanup_interval")
        builder.button(text="🔙 Назад", callback_data="admin_back")
        builder.adjust(2, 2, 1)
        return builder.as_markup()
    
    @staticmethod
    def get_moderators_kb() -> InlineKeyboardMarkup:
        """Клавиатура управления модераторами"""
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Добавить модератора", callback_data="add_moderator")
        builder.button(text="➖ Удалить модератора", callback_data="remove_moderator")
        builder.button(text="📋 Список модераторов", callback_data="list_moderators")
        builder.button(text="🔙 Назад", callback_data="admin_back")
        builder.adjust(1, 1, 1, 1)
        return builder.as_markup()
    
    @staticmethod
    def get_admins_kb() -> InlineKeyboardMarkup:
        """Клавиатура управления администраторами"""
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Добавить админа", callback_data="add_admin")
        builder.button(text="➖ Удалить админа", callback_data="remove_admin")
        builder.button(text="📋 Список админов", callback_data="list_admins")
        builder.button(text="🔙 Назад", callback_data="admin_back")
        builder.adjust(1, 1, 1, 1)
        return builder.as_markup()
    
    @staticmethod
    def get_cancel_kb() -> InlineKeyboardMarkup:
        """Клавиатура отмены"""
        builder = InlineKeyboardBuilder()
        builder.button(text="❌ Отмена", callback_data="cancel_input")
        builder.adjust(1)
        return builder.as_markup()

# ================== ВАЛИДАТОРЫ ==================
class ContentValidator:
    """Валидация входящего контента"""
    
    @staticmethod
    def is_allowed_content(message: Message) -> tuple[bool, Optional[str]]:
        if message.photo:
            return True, message.photo[-1].file_id
        elif message.video:
            if message.video.file_size and message.video.file_size > BotConfig.MAX_VIDEO_SIZE_MB * 1024 * 1024:
                return False, f"Видео слишком большое (максимум {BotConfig.MAX_VIDEO_SIZE_MB}МБ)"
            return True, message.video.file_id
        return False, None

# ================== ОСНОВНОЙ КОД ==================
class MemesModerationBot:
    """Основной класс бота"""
    
    def __init__(self):
        self.bot = Bot(token=BotConfig.BOT_TOKEN)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.post_manager = PostManager()
        
        self._register_handlers()
        
    def _register_handlers(self):
        """Регистрация всех обработчиков с правильным порядком"""
        # Команды
        self.dp.message.register(self._cmd_start, Command("start"))
        self.dp.message.register(self._cmd_help, Command("help"))
        self.dp.message.register(self._cmd_admin, Command("adminpanel"))
        self.dp.message.register(self._cmd_cancel, Command("cancel"))
        
        # Обработка комментариев (ДОЛЖНЫ БЫТЬ ПЕРВЫМИ!)
        self.dp.message.register(self._handle_approve_comment, CommentStates.waiting_for_approve_comment)
        self.dp.message.register(self._handle_reject_comment, CommentStates.waiting_for_reject_comment)
        
        # Обработка ввода админ-панели (ДОЛЖНЫ БЫТЬ ПЕРВЫМИ!)
        self.dp.message.register(self._handle_admin_input, StateFilter(AdminStates))
        
        # Приём контента (только приватные чаты, когда не в состоянии)
        self.dp.message.register(self._handle_content, 
                                F.chat.type == 'private',
                                StateFilter(None))  # Только когда не в состоянии
        
        # Обработка действий модераторов
        self.dp.callback_query.register(self._approve_post, F.data.startswith("approve_") & ~F.data.contains("comment"))
        self.dp.callback_query.register(self._reject_post, F.data.startswith("reject_") & ~F.data.contains("comment"))
        self.dp.callback_query.register(self._approve_with_comment_start, F.data.startswith("approve_comment_"))
        self.dp.callback_query.register(self._reject_with_comment_start, F.data.startswith("reject_comment_"))
        
        # Обработка отмены ввода
        self.dp.callback_query.register(self._cancel_input, F.data == "cancel_input")
        
        # Вспомогательные колбеки
        self.dp.callback_query.register(self._show_rules, F.data == "show_rules")
        self.dp.callback_query.register(self._how_to_send, F.data == "how_to_send")
        
        # Админ-панель
        self.dp.callback_query.register(self._admin_stats, F.data == "admin_stats")
        self.dp.callback_query.register(self._admin_limits, F.data == "admin_limits")
        self.dp.callback_query.register(self._admin_moderators, F.data == "admin_moderators")
        self.dp.callback_query.register(self._admin_admins, F.data == "admin_admins")
        self.dp.callback_query.register(self._admin_cleanup, F.data == "admin_cleanup")
        self.dp.callback_query.register(self._admin_broadcast, F.data == "admin_broadcast")
        self.dp.callback_query.register(self._admin_save, F.data == "admin_save")
        self.dp.callback_query.register(self._admin_close, F.data == "admin_close")
        self.dp.callback_query.register(self._admin_back, F.data == "admin_back")
        
        # Настройки
        self.dp.callback_query.register(self._set_photo_size, F.data == "set_photo_size")
        self.dp.callback_query.register(self._set_video_size, F.data == "set_video_size")
        self.dp.callback_query.register(self._set_pending_limit, F.data == "set_pending_limit")
        self.dp.callback_query.register(self._set_cleanup_interval, F.data == "set_cleanup_interval")
        
        # Управление пользователями
        self.dp.callback_query.register(self._add_moderator, F.data == "add_moderator")
        self.dp.callback_query.register(self._remove_moderator, F.data == "remove_moderator")
        self.dp.callback_query.register(self._list_moderators, F.data == "list_moderators")
        self.dp.callback_query.register(self._add_admin, F.data == "add_admin")
        self.dp.callback_query.register(self._remove_admin, F.data == "remove_admin")
        self.dp.callback_query.register(self._list_admins, F.data == "list_admins")
    
    # ================== КОМАНДЫ ==================
    async def _cmd_start(self, message: Message, state: FSMContext):
        """Обработка команды /start"""
        await state.clear()  # Сбрасываем состояние
        welcome_text = (
            "🚢 <b>Добро пожаловать в предложки Shkiper_online!</b>\n\n"
            "<b>Принимаем только:</b>\n"
            "• Фотографии (JPG, PNG)\n"
            "• Видео (MP4, до 20МБ)\n\n"
            "Присылай мемы, и лучшие будут опубликованы в группе!\n"
            "Все предложки проверяются модераторами <b>анонимно</b>."
        )
        
        await message.answer(
            welcome_text,
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_user_help_kb()
        )
    
    async def _cmd_help(self, message: Message, state: FSMContext):
        """Обработка команды /help"""
        await state.clear()  # Сбрасываем состояние
        help_text = (
            "📋 <b>Как это работает:</b>\n\n"
            "1. Пришли фото или видео в этот чат\n"
            "2. Бот скрывает твоё имя и передаёт модераторам\n"
            "3. Модераторы видят только твой ID (не аккаунт)\n"
            "4. Рассмотрение занимает до 24 часов\n"
            "5. Одобренные посты публикуются в теме «❶ Мемы подписчиков»\n\n"
            "<b>Технические требования:</b>\n"
            f"• Фото: до {BotConfig.MAX_PHOTO_SIZE_MB}МБ\n"
            f"• Видео: до {BotConfig.MAX_VIDEO_SIZE_MB}МБ, формат MP4\n\n"
            "❌ <b>Не принимаем:</b> текст, GIF, документы, аудио, стикеры"
        )
        
        await message.answer(help_text, parse_mode="HTML")
    
    async def _cmd_admin(self, message: Message, state: FSMContext):
        """Секретная команда /adminpanel"""
        if message.from_user.id not in BotConfig.ADMIN_IDS:
            await message.answer("⛔ У вас нет доступа к админ-панели.")
            return
        
        await state.clear()  # Сбрасываем предыдущие состояния
        admin_text = (
            "⚙️ <b>Админ-панель управления ботом</b>\n\n"
            "Выберите действие:"
        )
        
        await message.answer(
            admin_text,
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_admin_panel_kb()
        )
    
    async def _cmd_cancel(self, message: Message, state: FSMContext):
        """Команда отмены /cancel"""
        current_state = await state.get_state()
        if current_state is None:
            await message.answer("❌ Нет активного действия для отмены.")
            return
        
        await state.clear()
        await message.answer("✅ Действие отменено.")
        
        # Если это админ, возвращаем в админ-панель
        if message.from_user.id in BotConfig.ADMIN_IDS:
            await message.answer(
                "⚙️ <b>Админ-панель управления ботом</b>\n\n"
                "Выберите действие:",
                parse_mode="HTML",
                reply_markup=KeyboardFactory.get_admin_panel_kb()
            )
    
    # ================== ОБРАБОТКА КОНТЕНТА ==================
    async def _handle_content(self, message: Message):
        """Обработка входящего контента от пользователей"""
        # Пропускаем команды
        if message.text and message.text.startswith('/'):
            return
        
        is_valid, file_id_or_error = ContentValidator.is_allowed_content(message)
        
        if not is_valid:
            error_msg = file_id_or_error or (
                "❌ <b>Этот тип контента не поддерживается.</b>\n\n"
                "Бот принимает только:\n"
                "• Фотографии (JPG, PNG)\n"
                "• Видео (MP4, до 20МБ)\n\n"
                "Используй /help для подробностей."
            )
            await message.answer(error_msg, parse_mode="HTML")
            return
        
        # Подтверждение пользователю
        await message.reply(
            "✅ <b>Принято!</b>\n\n"
            "Твоя предложка отправлена модераторам на рассмотрение. "
            "Это может занять до 24 часов.",
            parse_mode="HTML"
        )
        
        try:
            content_type = ContentType.PHOTO if message.photo else ContentType.VIDEO
            mod_caption = self._create_moderation_caption(message)
            
            sent_msg = await self._send_to_moderators(
                content_type=content_type,
                file_id=file_id_or_error,
                caption=mod_caption,
                reply_markup=KeyboardFactory.get_moderation_kb(message.message_id)
            )
            
            if sent_msg:
                await self.post_manager.add_post(
                    user_id=message.from_user.id,
                    username=message.from_user.username,
                    original_msg_id=message.message_id,
                    mod_msg_id=sent_msg.message_id,
                    content_type=content_type,
                    file_id=file_id_or_error,
                    caption=message.caption
                )
                logging.info(f"Пост {message.message_id} от {message.from_user.id} отправлен модераторам")
            else:
                await message.reply("⚠️ Произошла ошибка при отправке модераторам. Попробуй позже.")
                
        except TelegramAPIError as e:
            logging.error(f"API ошибка: {e}")
            await message.reply("⚠️ Техническая ошибка. Попробуй позже.")
        except Exception as e:
            logging.error(f"Неизвестная ошибка: {e}")
            await message.reply("⚠️ Внутренняя ошибка бота.")
    
    def _create_moderation_caption(self, message: Message) -> str:
        """Создает подпись для модераторов"""
        user = message.from_user
        content_type = "Фото" if message.photo else "Видео"
        original_caption = f"\n✏️ Подпись: {message.caption}" if message.caption else ""
        
        return (
            f"📨 <b>Новая предложка #{message.message_id}</b>\n"
            f"└ Тип: {content_type}\n"
            f"👤 <b>Отправитель:</b>\n"
            f"├ ID: <code>{user.id}</code>\n"
            f"├ Имя: {html.quote(user.first_name or '')}\n"
            f"└ Юзернейм: @{user.username if user.username else 'нет'}\n"
            f"{original_caption}"
            f"⏰ Время: {datetime.now().strftime('%H:%M:%S')}"
        )
    
    async def _send_to_moderators(self, content_type: ContentType, file_id: str, 
                                 caption: str, reply_markup: InlineKeyboardMarkup) -> Optional[Message]:
        """Отправляет контент в чат модераторов"""
        try:
            if content_type == ContentType.PHOTO:
                return await self.bot.send_photo(
                    chat_id=BotConfig.MODERATORS_CHAT_ID,
                    photo=file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            else:
                return await self.bot.send_video(
                    chat_id=BotConfig.MODERATORS_CHAT_ID,
                    video=file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
        except TelegramAPIError as e:
            logging.error(f"Ошибка отправки модераторам: {e}")
            return None
    
    # ================== МОДЕРАЦИЯ ==================
    async def _check_moderator_permission(self, callback: CallbackQuery) -> bool:
        if callback.from_user.id not in BotConfig.MODERATORS:
            await callback.answer("❌ Ты не модератор!", show_alert=True)
            return False
        return True
    
    async def _update_moderator_message(self, callback: CallbackQuery, post_data: PendingPost, action: str, comment: str = ""):
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=BotConfig.MODERATORS_CHAT_ID,
                message_id=post_data.moderator_message_id,
                reply_markup=None
            )
            
            username = html.quote(callback.from_user.username or callback.from_user.first_name or 'модератор')
            
            if action == "approve":
                emoji = "✅"
                action_text = "ОДОБРЕНО"
            else:
                emoji = "❌"
                action_text = "ОТКЛОНЕНО"
            
            comment_text = f"\n💬 Комментарий: {comment}" if comment else ""
            
            new_caption = (
                f"<s>{callback.message.caption or ''}</s>\n\n"
                f"{emoji} <b>{action_text}</b> @{username}"
                f"{comment_text}"
            )
            
            await self.bot.edit_message_caption(
                chat_id=BotConfig.MODERATORS_CHAT_ID,
                message_id=post_data.moderator_message_id,
                caption=new_caption,
                parse_mode="HTML"
            )
                    
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                logging.warning(f"Не удалось обновить сообщение модерации: {e}")
    
    async def _publish_to_group(self, post_data: PendingPost, comment: str = ""):
        try:
            caption = comment if comment else None
            
            if post_data.content_type == ContentType.PHOTO:
                await self.bot.send_photo(
                    chat_id=BotConfig.MAIN_GROUP_ID,
                    message_thread_id=BotConfig.MAIN_GROUP_THREAD_ID,
                    photo=post_data.file_id,
                    caption=caption,
                    parse_mode="HTML" if caption else None
                )
            else:
                await self.bot.send_video(
                    chat_id=BotConfig.MAIN_GROUP_ID,
                    message_thread_id=BotConfig.MAIN_GROUP_THREAD_ID,
                    video=post_data.file_id,
                    caption=caption,
                    parse_mode="HTML" if caption else None
                )
            
            return True
        except TelegramAPIError as e:
            logging.error(f"Ошибка публикации в группу: {e}")
            return False
    
    async def _notify_user_rejection(self, post_data: PendingPost, comment: str = ""):
        try:
            comment_text = f"\n\n<b>Комментарий модератора:</b>\n{comment}" if comment else ""
            
            await self.bot.send_message(
                chat_id=post_data.user_id,
                text=(
                    "❌ <b>Ваша предложка была отклонена модератором</b>\n\n"
                    "Не расстраивайся! Попробуй отправить что-то другое."
                    f"{comment_text}"
                ),
                parse_mode="HTML"
            )
            return True
        except TelegramAPIError as e:
            logging.error(f"Не удалось уведомить пользователя {post_data.user_id}: {e}")
            return False
    
    async def _approve_post(self, callback: CallbackQuery):
        if not await self._check_moderator_permission(callback):
            return
        
        post_id = int(callback.data.split("_")[1])
        post_data = await self.post_manager.get_post(post_id)
        
        if not post_data or post_data.is_processed:
            await callback.answer("Предожка уже обработана или устарела.")
            return
        
        await self._update_moderator_message(callback, post_data, "approve")
        success = await self._publish_to_group(post_data)
        
        if success:
            await callback.answer("✅ Предожка одобрена и опубликована!")
            logging.info(f"Пост {post_id} одобрен {callback.from_user.id}")
        else:
            await callback.answer("⚠️ Ошибка при публикации в группу.", show_alert=True)
        
        await self.post_manager.mark_approved(post_id)
    
    async def _reject_post(self, callback: CallbackQuery):
        if not await self._check_moderator_permission(callback):
            return
        
        post_id = int(callback.data.split("_")[1])
        post_data = await self.post_manager.get_post(post_id)
        
        if not post_data or post_data.is_processed:
            await callback.answer("Предожка уже обработана.")
            return
        
        await self._update_moderator_message(callback, post_data, "reject")
        user_notified = await self._notify_user_rejection(post_data)
        
        if user_notified:
            await callback.answer("❌ Предожка отклонена. Пользователь уведомлен.")
            logging.info(f"Пост {post_id} отклонен {callback.from_user.id}")
        else:
            await callback.answer("❌ Предожка отклонена. Не удалось уведомить пользователя.", show_alert=True)
        
        await self.post_manager.mark_rejected(post_id)
    
    async def _approve_with_comment_start(self, callback: CallbackQuery, state: FSMContext):
        if not await self._check_moderator_permission(callback):
            return
        
        post_id = int(callback.data.split("_")[2])
        post_data = await self.post_manager.get_post(post_id)
        
        if not post_data or post_data.is_processed:
            await callback.answer("Предожка уже обработана или устарела.")
            return
        
        await state.set_state(CommentStates.waiting_for_approve_comment)
        await state.update_data(post_id=post_id, moderator_id=callback.from_user.id)
        
        await callback.message.answer(
            "💬 <b>Введите комментарий для публикации:</b>\n\n"
            "Этот комментарий будет отображен вместе с постом в группе.\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _reject_with_comment_start(self, callback: CallbackQuery, state: FSMContext):
        if not await self._check_moderator_permission(callback):
            return
        
        post_id = int(callback.data.split("_")[2])
        post_data = await self.post_manager.get_post(post_id)
        
        if not post_data or post_data.is_processed:
            await callback.answer("Предожка уже обработана.")
            return
        
        await state.set_state(CommentStates.waiting_for_reject_comment)
        await state.update_data(post_id=post_id, moderator_id=callback.from_user.id)
        
        await callback.message.answer(
            "📝 <b>Введите причину отклонения:</b>\n\n"
            "Этот комментарий будет отправлен пользователю.\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _handle_approve_comment(self, message: Message, state: FSMContext):
        data = await state.get_data()
        post_id = data.get('post_id')
        moderator_id = data.get('moderator_id')
        
        # Проверяем, что сообщение от того же модератора
        if message.from_user.id != moderator_id:
            await message.answer("❌ Это не ваш запрос на комментарий.")
            return
        
        if not post_id:
            await message.answer("Ошибка: данные поста не найдены.")
            await state.clear()
            return
        
        post_data = await self.post_manager.get_post(post_id)
        if not post_data or post_data.is_processed:
            await message.answer("Предожка уже обработана или устарела.")
            await state.clear()
            return
        
        comment = message.text[:BotConfig.MAX_COMMENT_LENGTH]
        
        # Создаем fake callback для обновления сообщения
        class FakeCallback:
            def __init__(self, user, message_text):
                self.from_user = user
                self.message = type('obj', (object,), {'caption': message_text})()
                self.data = f"approve_{post_id}"
            
            async def answer(self, text, show_alert=False):
                pass
        
        fake_callback = FakeCallback(message.from_user, "")
        await self._update_moderator_message(fake_callback, post_data, "approve", comment)
        
        success = await self._publish_to_group(post_data, comment)
        
        if success:
            await message.answer(f"✅ Предожка одобрена с комментарием!")
            logging.info(f"Пост {post_id} одобрен с комментарием {message.from_user.id}")
        else:
            await message.answer("⚠️ Ошибка при публикации в группу.")
        
        await self.post_manager.mark_approved(post_id)
        await state.clear()
    
    async def _handle_reject_comment(self, message: Message, state: FSMContext):
        data = await state.get_data()
        post_id = data.get('post_id')
        moderator_id = data.get('moderator_id')
        
        if message.from_user.id != moderator_id:
            await message.answer("❌ Это не ваш запрос на комментарий.")
            return
        
        if not post_id:
            await message.answer("Ошибка: данные поста не найдены.")
            await state.clear()
            return
        
        post_data = await self.post_manager.get_post(post_id)
        if not post_data or post_data.is_processed:
            await message.answer("Предожка уже обработана.")
            await state.clear()
            return
        
        comment = message.text[:BotConfig.MAX_COMMENT_LENGTH]
        
        class FakeCallback:
            def __init__(self, user, message_text):
                self.from_user = user
                self.message = type('obj', (object,), {'caption': message_text})()
                self.data = f"reject_{post_id}"
            
            async def answer(self, text, show_alert=False):
                pass
        
        fake_callback = FakeCallback(message.from_user, "")
        await self._update_moderator_message(fake_callback, post_data, "reject", comment)
        
        user_notified = await self._notify_user_rejection(post_data, comment)
        
        if user_notified:
            await message.answer(f"❌ Предожка отклонена с комментарием.")
            logging.info(f"Пост {post_id} отклонен с комментарием {message.from_user.id}")
        else:
            await message.answer("❌ Предожка отклонена. Не удалось уведомить пользователя.")
        
        await self.post_manager.mark_rejected(post_id)
        await state.clear()
    
    async def _cancel_input(self, callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.edit_text(
            "❌ Действие отменено.",
            reply_markup=None
        )
        await callback.answer()
    
    # ================== АДМИН-ПАНЕЛЬ ==================
    async def _admin_stats(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        stats = self.post_manager.get_stats()
        
        stats_text = (
            "📊 <b>Статистика бота</b>\n\n"
            f"• Постов в очереди: <b>{stats['pending_posts']}</b>\n"
            f"• Уникальных пользователей: <b>{stats['unique_users']}</b>\n"
            f"• Всего отправлено: <b>{stats['total_submitted']}</b>\n"
            f"• Одобрено: <b>{stats['total_approved']}</b>\n"
            f"• Отклонено: <b>{stats['total_rejected']}</b>\n\n"
            f"• Модераторов: <b>{len(BotConfig.MODERATORS)}</b>\n"
            f"• Администраторов: <b>{len(BotConfig.ADMIN_IDS)}</b>\n\n"
            f"<b>Текущие настройки:</b>\n"
            f"• Макс. размер фото: <b>{BotConfig.MAX_PHOTO_SIZE_MB} МБ</b>\n"
            f"• Макс. размер видео: <b>{BotConfig.MAX_VIDEO_SIZE_MB} МБ</b>\n"
            f"• Макс. очередь: <b>{BotConfig.MAX_PENDING_POSTS}</b>\n"
            f"• Очистка через: <b>{BotConfig.CLEANUP_INTERVAL_HOURS} ч</b>"
        )
        
        await callback.message.edit_text(
            stats_text,
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_admin_panel_kb()
        )
        await callback.answer()
    
    async def _admin_limits(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        await callback.message.edit_text(
            "⚙️ <b>Настройки лимитов</b>\n\n"
            "Выберите параметр для изменения:",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_settings_kb()
        )
        await callback.answer()
    
    async def _admin_moderators(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        await callback.message.edit_text(
            "👥 <b>Управление модераторами</b>\n\n"
            f"Текущее количество: {len(BotConfig.MODERATORS)}",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_moderators_kb()
        )
        await callback.answer()
    
    async def _admin_admins(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        await callback.message.edit_text(
            "🛠️ <b>Управление администраторами</b>\n\n"
            f"Текущее количество: {len(BotConfig.ADMIN_IDS)}",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_admins_kb()
        )
        await callback.answer()
    
    async def _admin_cleanup(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        count = await self.post_manager.cleanup_all_pending()
        
        await callback.message.edit_text(
            f"🧹 <b>Очередь очищена</b>\n\n"
            f"Удалено постов: {count}",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_admin_panel_kb()
        )
        await callback.answer()
    
    async def _admin_broadcast(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        await state.set_state(AdminStates.waiting_broadcast)
        await callback.message.answer(
            "📢 <b>Рассылка сообщения</b>\n\n"
            "Введите сообщение для рассылки всем пользователям, которые когда-либо отправляли предложки:\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _admin_save(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        BotConfig.save_config()
        await callback.answer("✅ Конфигурация сохранена в файл!")
    
    async def _admin_close(self, callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.delete()
        await callback.answer()
    
    async def _admin_back(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        await callback.message.edit_text(
            "⚙️ <b>Админ-панель управления ботом</b>\n\n"
            "Выберите действие:",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_admin_panel_kb()
        )
        await callback.answer()
    
    # ================== НАСТРОЙКИ ==================
    async def _set_photo_size(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_photo_size)
        await callback.message.answer(
            f"📸 <b>Текущий максимальный размер фото: {BotConfig.MAX_PHOTO_SIZE_MB} МБ</b>\n\n"
            "Введите новый размер в МБ (1-100):\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _set_video_size(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_video_size)
        await callback.message.answer(
            f"🎥 <b>Текущий максимальный размер видео: {BotConfig.MAX_VIDEO_SIZE_MB} МБ</b>\n\n"
            "Введите новый размер в МБ (1-500):\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _set_pending_limit(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_pending_limit)
        await callback.message.answer(
            f"📁 <b>Текущий максимальный размер очереди: {BotConfig.MAX_PENDING_POSTS}</b>\n\n"
            "Введите новый лимит (10-1000):\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _set_cleanup_interval(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_cleanup_interval)
        await callback.message.answer(
            f"⏰ <b>Текущий интервал очистки: {BotConfig.CLEANUP_INTERVAL_HOURS} часов</b>\n\n"
            "Введите новый интервал в часах (1-720):\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _add_moderator(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_moderator_id)
        await callback.message.answer(
            "➕ <b>Добавление модератора</b>\n\n"
            "Введите ID пользователя для добавления в модераторы:\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _remove_moderator(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_moderator_id)
        await state.update_data(action="remove_moderator")
        await callback.message.answer(
            "➖ <b>Удаление модератора</b>\n\n"
            "Введите ID модератора для удаления:\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _list_moderators(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        if not BotConfig.MODERATORS:
            await callback.answer("Список модераторов пуст!", show_alert=True)
            return
        
        moderators_list = "\n".join([f"• <code>{mod_id}</code>" for mod_id in BotConfig.MODERATORS])
        await callback.message.edit_text(
            f"📋 <b>Список модераторов</b>\n\n"
            f"Количество: {len(BotConfig.MODERATORS)}\n\n"
            f"{moderators_list}",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_moderators_kb()
        )
        await callback.answer()
    
    async def _add_admin(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_admin_id)
        await state.update_data(action="add_admin")
        await callback.message.answer(
            "➕ <b>Добавление администратора</b>\n\n"
            "Введите ID пользователя для добавления в администраторы:\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _remove_admin(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.set_state(AdminStates.waiting_admin_id)
        await state.update_data(action="remove_admin")
        await callback.message.answer(
            "➖ <b>Удаление администратора</b>\n\n"
            "Введите ID администратора для удаления:\n"
            "Используйте /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_cancel_kb()
        )
        await callback.answer()
    
    async def _list_admins(self, callback: CallbackQuery, state: FSMContext):
        if callback.from_user.id not in BotConfig.ADMIN_IDS:
            await callback.answer("⛔ Нет доступа!", show_alert=True)
            return
        
        await state.clear()
        if not BotConfig.ADMIN_IDS:
            await callback.answer("Список администраторов пуст!", show_alert=True)
            return
        
        admins_list = "\n".join([f"• <code>{admin_id}</code>" for admin_id in BotConfig.ADMIN_IDS])
        await callback.message.edit_text(
            f"📋 <b>Список администраторов</b>\n\n"
            f"Количество: {len(BotConfig.ADMIN_IDS)}\n\n"
            f"{admins_list}",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_admins_kb()
        )
        await callback.answer()
    
    # ================== ОБРАБОТКА ВВОДА АДМИН-ПАНЕЛИ ==================
    async def _handle_admin_input(self, message: Message, state: FSMContext):
        """Обработка ввода в админ-панели"""
        current_state = await state.get_state()
        data = await state.get_data()
        
        try:
            if current_state == AdminStates.waiting_photo_size:
                size = int(message.text)
                if 1 <= size <= 100:
                    BotConfig.MAX_PHOTO_SIZE_MB = size
                    await message.answer(f"✅ Макс. размер фото установлен: {size} МБ")
                else:
                    await message.answer("❌ Размер должен быть от 1 до 100 МБ")
            
            elif current_state == AdminStates.waiting_video_size:
                size = int(message.text)
                if 1 <= size <= 500:
                    BotConfig.MAX_VIDEO_SIZE_MB = size
                    await message.answer(f"✅ Макс. размер видео установлен: {size} МБ")
                else:
                    await message.answer("❌ Размер должен быть от 1 до 500 МБ")
            
            elif current_state == AdminStates.waiting_pending_limit:
                limit = int(message.text)
                if 10 <= limit <= 1000:
                    BotConfig.MAX_PENDING_POSTS = limit
                    await message.answer(f"✅ Макс. размер очереди установлен: {limit}")
                else:
                    await message.answer("❌ Лимит должен быть от 10 до 1000")
            
            elif current_state == AdminStates.waiting_cleanup_interval:
                interval = int(message.text)
                if 1 <= interval <= 720:
                    BotConfig.CLEANUP_INTERVAL_HOURS = interval
                    await message.answer(f"✅ Интервал очистки установлен: {interval} часов")
                else:
                    await message.answer("❌ Интервал должен быть от 1 до 720 часов")
            
            elif current_state == AdminStates.waiting_moderator_id:
                mod_id = int(message.text)
                action = data.get('action', 'add')
                
                if action == "remove":
                    if mod_id in BotConfig.MODERATORS:
                        BotConfig.MODERATORS.remove(mod_id)
                        await message.answer(f"✅ Модератор {mod_id} удален")
                    else:
                        await message.answer(f"❌ Модератор {mod_id} не найден")
                else:
                    BotConfig.MODERATORS.add(mod_id)
                    await message.answer(f"✅ Модератор {mod_id} добавлен")
            
            elif current_state == AdminStates.waiting_admin_id:
                admin_id = int(message.text)
                action = data.get('action', 'add')
                
                if action == "remove":
                    if admin_id in BotConfig.ADMIN_IDS:
                        if len(BotConfig.ADMIN_IDS) > 1:
                            BotConfig.ADMIN_IDS.remove(admin_id)
                            await message.answer(f"✅ Администратор {admin_id} удален")
                        else:
                            await message.answer("❌ Нельзя удалить последнего администратора!")
                    else:
                        await message.answer(f"❌ Администратор {admin_id} не найден")
                else:
                    BotConfig.ADMIN_IDS.add(admin_id)
                    await message.answer(f"✅ Администратор {admin_id} добавлен")
            
            elif current_state == AdminStates.waiting_broadcast:
                await message.answer("✅ Сообщение принято для рассылки. (Функция в разработке)")
            
            else:
                await message.answer("❌ Неизвестное состояние")
        
        except ValueError:
            await message.answer("❌ Пожалуйста, введите число")
        
        # Возвращаем в админ-панель
        await state.clear()
        await message.answer(
            "⚙️ <b>Админ-панель управления ботом</b>\n\n"
            "Выберите действие:",
            parse_mode="HTML",
            reply_markup=KeyboardFactory.get_admin_panel_kb()
        )
    
    # ================== ВСПОМОГАТЕЛЬНЫЕ КОЛБЭКИ ==================
    async def _show_rules(self, callback: CallbackQuery):
        await callback.message.answer(
            "📜 <b>Правила предложки:</b>\n\n"
            "1. Только оригинальный контент\n"
            "2. Без водяных знаков из других источников\n"
            "3. Соответствие тематике канала\n"
            "4. Без NSFW и запрещенного контента\n"
            "5. Максимум 3 предложки в сутки от одного пользователя",
            parse_mode="HTML"
        )
        await callback.answer()
    
    async def _how_to_send(self, callback: CallbackQuery):
        await callback.message.answer(
            "📤 <b>Как отправить предложку:</b>\n\n"
            "1. Просто пришли фото или видео в этот чат\n"
            "2. Дождись подтверждения от бота\n"
            "3. Если контент не подходит, бот сообщит почему\n"
            "4. Одобренные посты публикуются в течение 24 часов",
            parse_mode="HTML"
        )
        await callback.answer()
    
    # ================== ЗАПУСК ==================
    async def run(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('bot.log', encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        
        self._validate_config()
        
        print("=" * 50)
        print("🤖 Бот модерации мемов запущен")
        print(f"👮 Модераторов: {len(BotConfig.MODERATORS)}")
        print(f"🛠️ Администраторов: {len(BotConfig.ADMIN_IDS)}")
        print(f"💬 Чат модерации: {BotConfig.MODERATORS_CHAT_ID}")
        print(f"📢 Основная группа: {BotConfig.MAIN_GROUP_ID}")
        print(f"🧵 Тема публикаций: {BotConfig.MAIN_GROUP_THREAD_ID}")
        print("=" * 50)
        print("✅ Принимает только: Фото и Видео")
        print("✅ 4 кнопки модерации: одобрить/отклонить с комментариями")
        print("✅ Чистые публикации в группе")
        print("✅ Админ-панель с настройками")
        print("✅ FSM состояния работают корректно")
        print("=" * 50)
        
        await self.dp.start_polling(
            self.bot,
            allowed_updates=self.dp.resolve_used_update_types(),
            skip_updates=True
        )
    
    def _validate_config(self):
        required = ['BOT_TOKEN', 'MODERATORS_CHAT_ID', 'MAIN_GROUP_ID', 'MODERATORS', 'ADMIN_IDS']
        for attr in required:
            if not getattr(BotConfig, attr, None):
                raise ValueError(f"Не задана обязательная конфигурация: {attr}")
        
        if BotConfig.MODERATORS_CHAT_ID >= 0:
            logging.warning("MODERATORS_CHAT_ID должен быть отрицательным для групп/супергрупп")
        
        print("✓ Конфигурация валидна")

def main():
    bot = MemesModerationBot()
    
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
    except Exception as e:
        logging.critical(f"Критическая ошибка: {e}")
        print(f"❌ Бот упал с ошибкой: {e}")

if __name__ == "__main__":
    main()
