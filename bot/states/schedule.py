from aiogram.fsm.state import State, StatesGroup


class ScheduleStates(StatesGroup):
    waiting_timezone = State()   # Выбор часового пояса (первый раз)
    waiting_channel = State()    # Выбор канала при нескольких
    waiting_edit_content = State()  # Редактирование текста отложенного поста
    waiting_content = State()    # Ожидаем текст/медиа поста
    waiting_target = State()     # Куда публиковать: TG / MAX / оба
    waiting_datetime = State()   # Когда публиковать
    waiting_reschedule = State()  # Новое время для существующего поста
