import re


def replace_links(text: str, mapping: dict[str, str] | None = None) -> str:
    """Замена TG-ссылок на MAX-ссылки.

    mapping: {tg_username_lower → публичная MAX-ссылка из channels.max_channel_url}.

    Поведение:
    - Если mapping пустой/None — текст не трогаем вообще.
    - https://t.me/<u>, t.me/<u>, @<u> — заменяются на mapping[<u>], если запись есть.
      Если нет — оставляем как есть (никаких угадываний).
    - Ссылки на конкретные посты (t.me/<u>/123) — заменяем на mapping[<u>], если есть;
      иначе оставляем.
    """
    if not text or not mapping:
        return text or ""

    m = {k.lower(): v for k, v in mapping.items() if v}

    def _sub_channel(match: re.Match) -> str:
        username = match.group(1).lower()
        return m.get(username, match.group(0))

    def _sub_post(match: re.Match) -> str:
        username = match.group(1).lower()
        return m.get(username, match.group(0))

    # Сначала ссылки на посты (более длинный pattern), чтоб не съело общее правило
    text = re.sub(r"https?://t\.me/([a-zA-Z_][a-zA-Z0-9_]*)/\d+", _sub_post, text)
    text = re.sub(r"\bt\.me/([a-zA-Z_][a-zA-Z0-9_]*)/\d+", _sub_post, text)
    # Ссылки на канал
    text = re.sub(r"https?://t\.me/([a-zA-Z_][a-zA-Z0-9_]*)\b", _sub_channel, text)
    text = re.sub(r"\bt\.me/([a-zA-Z_][a-zA-Z0-9_]*)\b", _sub_channel, text)
    # @username — только если есть в mapping (иначе можно сломать обычный текст)
    def _sub_at(match: re.Match) -> str:
        username = match.group(1).lower()
        if username in m:
            return m[username]
        return match.group(0)
    text = re.sub(r"(?<![A-Za-z0-9_/])@([a-zA-Z_][a-zA-Z0-9_]{3,})\b", _sub_at, text)
    return text


def replace_max_links(text: str, mapping: dict[str, str] | None = None) -> str:
    """Замена MAX-ссылок на TG-ссылки (обратное направление).

    mapping: {max_url → tg_link}.
    """
    if not text or not mapping:
        return text or ""

    for max_url, tg_link in mapping.items():
        if max_url in text:
            text = text.replace(max_url, tg_link)

    return text
