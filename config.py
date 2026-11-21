import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    d = {}
    p = os.path.join(BASE_DIR, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


_E = _load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN") or _E.get("BOT_TOKEN", "")
TONCENTER_KEY = os.getenv("TONCENTER_KEY") or _E.get("TONCENTER_KEY", "")
DB_PATH = os.path.join(BASE_DIR, "zayberman.db")

ADMIN_ID = 7308147004
ADMIN_USERNAME = "rzabeyda"

# Расписание задаётся в ТВОЁМ часовом поясе, клиенты видят время по МСК
TZ_ADMIN = "Europe/Tallinn"     # переедешь в Хургаду -> "Africa/Cairo"
TZ_ADMIN_LABEL = "Таллин"
TZ_CLIENT = "Europe/Moscow"
TZ_CLIENT_LABEL = "МСК"
DAY_START = 8 * 60          # первое начало 08:00 (в минутах)
LAST_START = 18 * 60        # последнее начало 18:00
SLOT_STEP = 30              # шаг сетки начала
DURATION = 60               # длительность консультации
BREAK = 30                  # перерыв между консультациями
WORK_HOURS = list(range(8, 19))  # часы, которые можно закрывать в админке (08..18)
MIN_LEAD_MIN = 120          # записаться можно не раньше чем за 2 часа
HORIZON_DAYS = 60           # на сколько дней вперёд открыта запись
HOLD_MIN = 120              # сколько держим бронь без оплаты

# Цена
PRICE_RUB = 2000

# Реквизиты
PAYPAL = "https://paypal.me/zabeyda"
DALINK = "https://dalink.to/zabeyda"
REVOLUT_NAME = "Roman Zabeida"
REVOLUT_IBAN = "LT923250043907657767"
TON_ADDR = "UQAgX7QqhUKcEfW-7C4e2Z9x_A6iIvsDZJ9zI05H96-wvmPK"
TRIBUTE = "https://web.tribute.tg/p/L9Q"

# Автопроверка крипты
TONCENTER = "https://toncenter.com/api/v3"
USDT_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
USDT_MASTER_RAW = "0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE"

WELCOME = (
    "👋 Привет! Здесь можно записаться на личную консультацию.\n\n"
    "⏱ 60 минут, звонок в Telegram\n"
    "💰 {price}\n"
    "🕗 Время указано по Москве (МСК)\n\n"
    "Выберите действие кнопками ниже 👇"
)
