"""
Щоденний запуск бота для всіх курсів одразу.

Читає courses.json — список курсів (рівень, розклад, чати, матеріали).
Для кожного курсу перевіряє:
  - чи курс уже стартував (course_start_date),
  - чи сьогодні день заняття саме для нього (lesson_days),
  - чи є в репозиторії потрібні файли (words_csv, аудіо),
  - чи задані потрібні секрети (chat_env_vars).
Якщо все ОК — надсилає "слово дня" (аудіо) і нагадування про урок
у чати цього курсу. Якщо чогось бракує (курс ще не готовий, не
сьогодні його день, немає матеріалів) — курс просто тихо
пропускається, без помилки, і робота триває для інших курсів.

Змінна середовища FORCE_SEND=true дозволяє протестувати відправку
негайно, ігноруючи дату старту курсу та день тижня (для ручного
запуску "Run workflow" перед стартом курсу).
"""

import csv
import datetime
import json
import os
from zoneinfo import ZoneInfo

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COURSES_FILE = os.path.join(BASE_DIR, "courses.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
FORCE_SEND = os.environ.get("FORCE_SEND", "").strip().lower() in ("1", "true", "yes")

# Час за Києвом береться з часової зони Europe/Kyiv, тож перехід на
# літній/зимовий час відбувається автоматично — нічого міняти вручну не треба.
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# О цій годині за Києвом надсилається повний ранковий блок
# (слово дня + нагадування про урок). В інший час скрипт лише
# перевіряє, чи не настав момент для короткого нагадування "за годину".
MORNING_HOUR = 10

# Повні назви уроків — колонка "les" у words*.csv містить лише "Les 1" / "Les 2",
# тут вони розгортаються в повну назву для повідомлення.
# Додай новий рядок сюди, коли з'являтимуться нові уроки (для будь-якого рівня).
LESSON_TITLES = {
    "Les 1": "Les 1 — Kennismaken",
    "Les 2": "Les 2 — Wonen",
}
def load_courses():
    with open(COURSES_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_words(csv_path):
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_state(state_path):
    if not os.path.exists(state_path):
        return {"next_index": 0}
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state_path, state):
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_caption(word_row, lesson_section=None):
    lesson_key = word_row["les"].strip()
    lesson_title = LESSON_TITLES.get(lesson_key, lesson_key)
    section_line = f"📖 Розділ: {lesson_section}\n" if lesson_section else ""
    return (
        f"🌷 Woord van de dag\n\n"
        f"🇳🇱 Слово:\n{word_row['woord']}\n\n"
        f"🇺🇦 Переклад:\n{word_row['vertaling']}\n\n"
        f"💬 Приклад:\n{word_row['voorbeeld']}\n\n"
        f"📚 Урок: {lesson_title}\n"
        f"{section_line}\n"
        f"🔊 Вимова — слухай аудіо вище 👆\n\n"
        f"💡 Спробуй сьогодні використати це слово в чаті "
        f"або придумати власне речення."
    )


def resolved_lesson_time(course, today):
    """Час уроку на конкретну дату — з урахуванням особливих винятків
    (special_lesson_times), якщо для сьогодні такий заданий."""
    return course.get("special_lesson_times", {}).get(
        today.isoformat(), course["lesson_time"]
    )


def lesson_start_hour(course, today):
    """Година початку уроку (ціле число), напр. з '19:00–20:00' → 19."""
    time_str = resolved_lesson_time(course, today)
    start_part = time_str.split("–")[0].strip()
    return int(start_part.split(":")[0])


def lesson_reminder_text(course, today):
    time_str = resolved_lesson_time(course, today)
    meet_link = course.get("meet_link")
    link_line = f"\n🔗 Посилання на урок:\n{meet_link}\n" if meet_link else ""
    return (
        f"🇳🇱 Сьогодні урок!\n\n"
        f"🕖 {time_str}\n"
        f"Час зустрічатися та практикувати нідерландську 💬\n"
        f"{link_line}\n"
        f"Tot straks! 👋"
    )


def hour_before_reminder_text(course, today):
    time_str = resolved_lesson_time(course, today)
    start_time = time_str.split("–")[0].strip()
    dutch_time = start_time.replace(":", ".")  # 19:00 → 19.00 (нідерл. формат)
    meet_link = course.get("meet_link")
    link_line = f"\n🔗 {meet_link}" if meet_link else ""
    return (
        f"Hallo 👋\n"
        f"de les begint om {dutch_time} uur\n"
        f"Tot straks 👋"
        f"{link_line}"
    )


def chat_ids_for(course):
    return [
        os.environ[name]
        for name in course["chat_env_vars"]
        if os.environ.get(name)
    ]


def send_audio(chat_ids, caption, audio_path):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    audio_filename = os.path.basename(audio_path)

    errors = []
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"audio": (audio_filename, audio_bytes)},
                timeout=30,
            )
            if not resp.ok:
                raise RuntimeError(f"{resp.status_code} {resp.text}")
            result = resp.json()
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API помилка: {result}")
            print(f"    → аудіо надіслано в chat_id={chat_id}")
        except Exception as exc:
            print(f"    → ПОМИЛКА (аудіо) для chat_id={chat_id}: {exc}")
            errors.append((chat_id, exc))

    if errors and len(errors) == len(chat_ids):
        raise RuntimeError(f"Не вдалося надіслати аудіо жодному чату: {errors}")


def send_message(chat_ids, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    errors = []
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "text": text},
                timeout=30,
            )
            if not resp.ok:
                raise RuntimeError(f"{resp.status_code} {resp.text}")
            result = resp.json()
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API помилка: {result}")
            print(f"    → нагадування надіслано в chat_id={chat_id}")
        except Exception as exc:
            print(f"    → ПОМИЛКА (нагадування) для chat_id={chat_id}: {exc}")
            errors.append((chat_id, exc))

    if errors and len(errors) == len(chat_ids):
        raise RuntimeError(f"Не вдалося надіслати нагадування жодному чату: {errors}")


def process_course_morning(course, today):
    label = course.get("label", course["id"])
    print(f"— Курс: {label} (ранковий блок)")

    start_date_str = course.get("course_start_date")
    if not start_date_str:
        print(f"  дату старту ще не задано в courses.json — пропускаю.")
        return

    start_date = datetime.date.fromisoformat(start_date_str)
    if not FORCE_SEND and today < start_date:
        print(f"  курс ще не почався (старт {start_date.isoformat()}) — пропускаю.")
        return

    if not FORCE_SEND and today.isoweekday() not in course["lesson_days"]:
        print(f"  сьогодні не день заняття цього курсу — пропускаю.")
        return

    chat_ids = chat_ids_for(course)
    if not chat_ids:
        print(f"  немає жодного налаштованого chat_id (секрети) — пропускаю.")
        return

    words_csv = os.path.join(BASE_DIR, course["words_csv"])
    audio_dir = os.path.join(BASE_DIR, course["audio_dir"])
    state_path = os.path.join(BASE_DIR, course["state_file"])

    if not os.path.exists(words_csv):
        print(f"  файл {course['words_csv']} ще не додано — пропускаю слово дня.")
    else:
        words = load_words(words_csv)
        if not words:
            print(f"  {course['words_csv']} порожній — пропускаю слово дня.")
        else:
            state = load_state(state_path)
            idx = state.get("next_index", 0) % len(words)
            word_row = words[idx]
            audio_path = os.path.join(audio_dir, word_row["audio_bestand"])

            caption = build_caption(word_row, course.get("lesson_section"))
            if os.path.exists(audio_path):
                send_audio(chat_ids, caption, audio_path)
            else:
                # Аудіо для слова немає — надсилаємо слово текстом, без рядка про вимову
                print(f"  аудіофайл не знайдено: {audio_path} — надсилаю слово текстом.")
                send_message(chat_ids, caption.replace("🔊 Вимова — слухай аудіо вище 👆\n\n", ""))
            print(f"  надіслано слово #{idx + 1}: {word_row['woord']}")
            state["next_index"] = (idx + 1) % len(words)
            save_state(state_path, state)

    send_message(chat_ids, lesson_reminder_text(course, today))
    print(f"  надіслано нагадування про урок.")


def process_course_hour_before(course, today, now_kyiv_hour):
    label = course.get("label", course["id"])

    start_date_str = course.get("course_start_date")
    if not start_date_str:
        return
    start_date = datetime.date.fromisoformat(start_date_str)
    if today < start_date:
        return

    if today.isoweekday() not in course["lesson_days"]:
        return

    target_hour = lesson_start_hour(course, today) - 1
    if now_kyiv_hour != target_hour and not FORCE_SEND:
        return

    print(f"— Курс: {label} (нагадування за годину)")

    chat_ids = chat_ids_for(course)
    if not chat_ids:
        print(f"  немає жодного налаштованого chat_id (секрети) — пропускаю.")
        return

    send_message(chat_ids, hour_before_reminder_text(course, today))
    print(f"  надіслано нагадування за годину до уроку.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Немає TELEGRAM_BOT_TOKEN у змінних середовища.")

    now_kyiv = datetime.datetime.now(KYIV_TZ)
    today = now_kyiv.date()

    is_morning_run = FORCE_SEND or now_kyiv.hour == MORNING_HOUR
    mode = "ранковий блок" if is_morning_run else "нагадування за годину"
    print(
        f"Дата запуску: {today.isoformat()}, {now_kyiv.hour:02d}:00 за Києвом "
        f"— режим: {mode}" + (" (FORCE_SEND увімкнено)" if FORCE_SEND else "")
    )

    courses = load_courses()
    failed = []
    for course in courses:
        try:
            if is_morning_run:
                process_course_morning(course, today)
            else:
                process_course_hour_before(course, today, now_kyiv.hour)
        except Exception as exc:
            label = course.get("label", course["id"])
            print(f"  ПОМИЛКА в курсі {label}: {exc}")
            failed.append(label)

    if failed:
        raise RuntimeError(
            f"Не вдалося обробити курс(и): {', '.join(failed)}. "
            f"Інші курси при цьому оброблені нормально (див. лог вище)."
        )


if __name__ == "__main__":
    main()
