from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def verify_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Проверить", callback_data="verify_channel")],
    ])


def verify_max_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Проверить права в MAX", callback_data="verify_max")],
    ])


def tariff_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🆓 Бесплатно — 30 постов", callback_data="tariff_free"
        )],
        [InlineKeyboardButton(
            text="⭐ Старт — 499 ₽/мес за канал", callback_data="tariff_start"
        )],
        [InlineKeyboardButton(
            text="🚀 Про — 799 ₽/мес, все каналы", callback_data="tariff_pro"
        )],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])
