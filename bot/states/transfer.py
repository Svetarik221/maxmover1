from aiogram.fsm.state import State, StatesGroup


class TransferStates(StatesGroup):
    waiting_tg_channel = State()       # Ожидаем ссылку на TG-канал
    waiting_verification = State()     # Ожидаем подтверждения кода в описании
    waiting_max_channel = State()      # Выбор MAX-канала из списка
    waiting_tariff_choice = State()    # Выбор тарифа
    transfer_in_progress = State()     # Идёт перенос
    waiting_max_link = State()         # Ожидаем публичную ссылку на MAX-канал
