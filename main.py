# -*- coding: utf-8 -*-
# VK-бот расписания: 1–2 дня × 1–2 интервала. Хранение в state.json (+ Gist).
# «Расписание» -> кратко + подпункт «Подробно» (доступно всем).
# Админ-меню без «Выбрать» и «Мои записи».
#
# ДОБАВЛЕНО:
# /get   — показать текущие DAY1/DAY2/TIME1/TIME2/CAPACITY/MAX_SLOTS_PER_USER + режим.
# /set <d1> <d2> <t1> <t2> [cap] [max]
#      — обновить даты/время и (опц.) лимиты, пересоздать расписание, очистить записи.
#        ЕСЛИ d2 = "-" → используется один день (только d1).
#        ЕСЛИ t2 = "-" → используется одно время (только t1).

import os
import json
import time
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

import vk_api
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.exceptions import ApiError

# ───────────────── для Gist-персистентности ─────────────────
import urllib.request
import urllib.error
import json as _json  # отдельный alias для сетевых JSON

load_dotenv()

# ───────────────── Health-check HTTP server for Render ─────────────────
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Render health check expects 200 on /
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

def _start_health_server():
    try:
        port = int(os.environ.get("PORT", "10000"))
        srv = HTTPServer(("", port), _HealthHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Health server listening on :{port}")
    except Exception as e:
        print("Health server failed:", e)

_start_health_server()


# ───────────────── env ─────────────────
COMMUNITY_TOKEN = os.getenv("VK_TOKEN")
GROUP_ID       = int(os.getenv("GROUP_ID", "0"))
USER_TOKEN     = os.getenv("USER_TOKEN")
MASTER_ID_ENV  = os.getenv("ADMIN_USER_ID")  # ваш VK user_id (число)

if not COMMUNITY_TOKEN or not GROUP_ID:
    raise RuntimeError("Нет VK_TOKEN или GROUP_ID в .env")

# ───────────────── VK ─────────────────
vk_session  = vk_api.VkApi(token=COMMUNITY_TOKEN)
session_api = vk_session.get_api()
longpoll    = VkLongPoll(vk_session)

user_api = None
if USER_TOKEN:
    try:
        user_session = vk_api.VkApi(token=USER_TOKEN)
        user_api = user_session.get_api()
        info2 = user_api.groups.getById(group_id=GROUP_ID)
        print("OK: USER_TOKEN видит группу:", info2[0]["name"])
    except Exception as e:
        print("Проблема с USER_TOKEN:", e)
else:
    print("ВНИМАНИЕ: USER_TOKEN не найден в .env (админ-команды ограничены)")

# ───────────────── неделя (дефолты из .env) ─────────────────
DAY1  = os.getenv("DAY1",  "05.11").strip()
DAY2  = os.getenv("DAY2",  "06.11").strip()   # может быть "-" для режима 1 день
TIME1 = os.getenv("TIME1", "16:00-18:00").strip()
TIME2 = os.getenv("TIME2", "18:00-20:00").strip()  # может быть "-" для режима 1 время
TIMES = [TIME1, TIME2]

CAPACITY           = int(os.getenv("CAPACITY", "13"))
MAX_SLOTS_PER_USER = int(os.getenv("MAX_SLOTS_PER_USER", "1"))

# ───────────────── Gist (переживает redeploy) ─────────────────
# Создай приватный Gist с файлами: state.json и config_state.json (оба: {}).
# В Render → Environment добавь:
#  GIST_TOKEN = <GitHub PAT с правом gist>
#  GIST_ID    = <идентификатор твоего Gist (кусок из URL)>
GIST_TOKEN = os.getenv("GIST_TOKEN")
GIST_ID    = os.getenv("GIST_ID")

def _gist_headers():
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "vk-bot-schedule"
    }

def gist_load(name: str):
    if not (GIST_TOKEN and GIST_ID):
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers()
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read().decode("utf-8"))
        files = data.get("files", {})
        if name in files and "content" in files[name]:
            content = files[name]["content"] or "{}"
            return _json.loads(content)
    except Exception as e:
        print("Gist load error:", e)
    return None

def gist_save(name: str, obj: dict):
    if not (GIST_TOKEN and GIST_ID):
        return
    try:
        body = _json.dumps({
            "files": {name: {"content": _json.dumps(obj, ensure_ascii=False, indent=2)}}
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            data=body, method="PATCH", headers=_gist_headers()
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print("Gist save error:", e)

# ───────────────── вспомогательные функции режима ─────────────────
def _has_second_day() -> bool:
    return bool(DAY2 and DAY2 != "-")

def _has_second_time() -> bool:
    return bool(TIME2 and TIME2 != "-")

def _active_days() -> List[str]:
    """Список активных дней (1 или 2)."""
    if _has_second_day():
        return [DAY1, DAY2]
    return [DAY1]

def _active_times() -> List[str]:
    """Список активных временных интервалов (1 или 2)."""
    times: List[str] = [TIME1]
    if _has_second_time():
        times.append(TIME2)
    return times

def _mode_str() -> str:
    d2 = _has_second_day()
    t2 = _has_second_time()
    if d2 and t2:
        return "2 дня × 2 времени"
    if d2 and not t2:
        return "2 дня × 1 время"
    if not d2 and t2:
        return "1 день × 2 времени"
    return "1 день × 1 время"

# ───────────────── persist config to survive restarts ─────────────────
CONFIG_FILE = "config_state.json"

def save_globals():
    data = {
        "DAY1": DAY1,
        "DAY2": DAY2,
        "TIME1": TIME1,
        "TIME2": TIME2,
        "CAPACITY": CAPACITY,
        "MAX_SLOTS_PER_USER": MAX_SLOTS_PER_USER
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("⚠️ Не удалось сохранить config_state.json:", e)
    gist_save("config_state.json", data)

def load_globals():
    global DAY1, DAY2, TIME1, TIME2, TIMES, CAPACITY, MAX_SLOTS_PER_USER
    # Сначала пробуем Gist
    try:
        gcfg = gist_load("config_state.json")
        if gcfg:
            DAY1  = gcfg.get("DAY1", DAY1)
            DAY2  = gcfg.get("DAY2", DAY2)
            TIME1 = gcfg.get("TIME1", TIME1)
            TIME2 = gcfg.get("TIME2", TIME2)
            CAPACITY = int(gcfg.get("CAPACITY", CAPACITY))
            MAX_SLOTS_PER_USER = int(gcfg.get("MAX_SLOTS_PER_USER", MAX_SLOTS_PER_USER))
            TIMES[:] = _active_times()
            print("✓ Загружены настройки из Gist")
            return
    except Exception as e:
        print("⚠️ Ошибка чтения config из Gist:", e)

    # Иначе — локальный файл
    if not os.path.exists(CONFIG_FILE):
        TIMES[:] = _active_times()
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        DAY1 = data.get("DAY1", DAY1)
        DAY2 = data.get("DAY2", DAY2)
        TIME1 = data.get("TIME1", TIME1)
        TIME2 = data.get("TIME2", TIME2)
        CAPACITY = int(data.get("CAPACITY", CAPACITY))
        MAX_SLOTS_PER_USER = int(data.get("MAX_SLOTS_PER_USER", MAX_SLOTS_PER_USER))
        TIMES[:] = _active_times()
        print("✓ Загружены сохранённые настройки из config_state.json")
    except Exception as e:
        print("⚠️ Ошибка чтения config_state.json:", e)
        TIMES[:] = _active_times()

# подгружаем сохранённые значения (если есть)
load_globals()

# ───────────────── описание слотов ─────────────────
def make_slots_map(d1: str, d2: str) -> Dict[str, Dict]:
    """
    Генерирует слоты в зависимости от режима:
      D1T1 — всегда (DAY1 + TIME1)
      D1T2 — если есть TIME2
      D2T1 — если есть DAY2
      D2T2 — если есть DAY2 и TIME2
    """
    result: Dict[str, Dict] = {
        "D1T1": {"title": f"{d1} {TIME1}", "users": []},
    }
    if _has_second_time():
        result["D1T2"] = {"title": f"{d1} {TIME2}", "users": []}
    if _has_second_day():
        result["D2T1"] = {"title": f"{d2} {TIME1}", "users": []}
        if _has_second_time():
            result["D2T2"] = {"title": f"{d2} {TIME2}", "users": []}
    return result

def slot_order() -> List[str]:
    """Порядок слотов для вывода расписания."""
    codes: List[str] = ["D1T1"]
    if _has_second_time():
        codes.append("D1T2")
    if _has_second_day():
        codes.append("D2T1")
        if _has_second_time():
            codes.append("D2T2")
    return codes

def slot_code_by(date_str: str, time_str: str) -> str:
    if date_str == DAY1 and time_str == TIME1:
        return "D1T1"
    if date_str == DAY1 and _has_second_time() and time_str == TIME2:
        return "D1T2"
    if _has_second_day():
        if date_str == DAY2 and time_str == TIME1:
            return "D2T1"
        if _has_second_time() and date_str == DAY2 and time_str == TIME2:
            return "D2T2"
    return ""

# ───────────────── state.json ─────────────────
STATE_FILE = "state.json"

def default_state() -> Dict:
    return {"slots": make_slots_map(DAY1, DAY2)}

def load_state() -> Dict:
    # Сначала пробуем Gist
    try:
        g = gist_load("state.json")
        if g and "slots" in g:
            expected = set(make_slots_map(DAY1, DAY2).keys())
            if set(g.get("slots", {}).keys()) == expected:
                print("✓ Загружено состояние из Gist")
                return g
    except Exception as e:
        print("⚠️ Ошибка чтения state из Gist:", e)

    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        expected = set(make_slots_map(DAY1, DAY2).keys())
        if set(data.get("slots", {}).keys()) != expected:
            return default_state()
        return data
    except Exception:
        return default_state()

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    gist_save("state.json", state)

state = load_state()
slots: Dict[str, Dict] = state["slots"]

# ───────────────── админы/ученики ─────────────────
MASTER_ID: Optional[int] = int(MASTER_ID_ENV) if (MASTER_ID_ENV and MASTER_ID_ENV.isdigit()) else None
ADMINS: List[int] = [aid for aid in {MASTER_ID, 1080975674, 20158141} if isinstance(aid, int)]
members_cache: List[Tuple[int, str]] = []   # (user_id, "Имя Фамилия") без админов

# ───────────────── клавиатуры ─────────────────
def user_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Выбрать", VkKeyboardColor.POSITIVE)
    kb.add_button("Расписание", VkKeyboardColor.SECONDARY)
    kb.add_button("Мои записи", VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button("Инструкция", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Перезапись", VkKeyboardColor.PRIMARY)
    return kb

def admin_root_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Расписание", VkKeyboardColor.SECONDARY)
    kb.add_button("Инструкция", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Ученики", VkKeyboardColor.SECONDARY)
    kb.add_button("Админы", VkKeyboardColor.SECONDARY)
    kb.add_button("Незаписавшиеся ученики", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Редактировать", VkKeyboardColor.PRIMARY)
    return kb

def date_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button(DAY1, VkKeyboardColor.SECONDARY)
    if _has_second_day():
        kb.add_button(DAY2, VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    return kb

def time_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button(TIME1, VkKeyboardColor.SECONDARY)
    if _has_second_time():
        kb.add_button(TIME2, VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    return kb

def schedule_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Подробно", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Назад", VkKeyboardColor.PRIMARY)
    return kb

def admin_edit_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Записать", VkKeyboardColor.POSITIVE)
    kb.add_button("Удалить", VkKeyboardColor.NEGATIVE)
    kb.add_line()
    kb.add_button("Назад", VkKeyboardColor.PRIMARY)
    return kb

def admin_choose_day_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button(DAY1, VkKeyboardColor.SECONDARY)
    if _has_second_day():
        kb.add_button(DAY2, VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Назад", VkKeyboardColor.PRIMARY)
    return kb

def admin_choose_time_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button(TIME1, VkKeyboardColor.SECONDARY)
    if _has_second_time():
        kb.add_button(TIME2, VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Назад", VkKeyboardColor.PRIMARY)
    return kb

# ───────────────── помощники ─────────────────
def send_msg(user_id: int, text: str, kb: Optional[VkKeyboard] = None, admin_view: bool = False):
    payload = {"user_id": user_id, "message": text, "random_id": 0}
    payload["keyboard"] = (admin_root_keyboard() if admin_view else user_keyboard()).get_keyboard() if kb is None else kb.get_keyboard()
    session_api.messages.send(**payload)

def users_get_names(uids: List[int]) -> List[str]:
    if not uids:
        return []
    try:
        api = user_api or session_api
        chunks = [uids[i:i+900] for i in range(0, len(uids), 900)]
        names: List[str] = []
        for ch in chunks:
            res = api.users.get(user_ids=",".join(map(str, ch)))
            for u in res:
                names.append(f"{u.get('first_name','')} {u.get('last_name','')}".strip())
        return names
    except Exception:
        return [str(u) for u in uids]

def fetch_admin_ids() -> List[int]:
    ids = set(ADMINS)
    if user_api:
        try:
            off = 0
            total = None
            while True:
                data = user_api.groups.getMembers(group_id=GROUP_ID, filter="managers", fields="id", count=200, offset=off)
                if total is None:
                    total = data.get("count", 0)
                items = data.get("items", [])
                for it in items:
                    ids.add(it["id"])
                off += len(items)
                if off >= total or not items:
                    break
        except Exception:
            pass
    return sorted(ids)

def fetch_members_excluding_admins() -> List[Tuple[int, str]]:
    if not user_api:
        raise Exception("USER_TOKEN недоступен")
    admin_ids = set(fetch_admin_ids())
    out: List[Tuple[int, str]] = []
    off, total = 0, None
    while True:
        data = user_api.groups.getMembers(group_id=GROUP_ID, fields="first_name,last_name,id", count=1000, offset=off)
        if total is None:
            total = data.get("count", 0)
        items = data.get("items", [])
        for it in items:
            uid = it.get("id")
            if uid in admin_ids:
                continue
            first = it.get("first_name") or ""
            last  = it.get("last_name") or ""
            out.append((uid, f"{first} {last}".strip()))
        off += len(items)
        if off >= total or not items:
            break
    return out

def already_booked_count(fullname: str) -> int:
    return sum(1 for sc in slots.values() if fullname in sc["users"])

def remove_user_from_all_slots(fullname: str):
    for sc in slots.values():
        if fullname in sc["users"]:
            sc["users"].remove(fullname)

def roster_with_numbers(users: List[str]) -> str:
    if not users:
        return "—"
    return "\n".join(f"{i+1}. {u}" for i, u in enumerate(users))

def summarize_schedule_for_button() -> str:
    lines = []
    for code in slot_order():
        sl = slots.get(code)
        if not sl:
            continue
        taken = len(sl["users"])
        left  = max(CAPACITY - taken, 0)
        lines.append(f"{sl['title']} | занято: {taken}/{CAPACITY} | свободно: {left}\n\n")
    lines.append("")
    lines.append("Нужно подробнее со списками? Нажми «Подробно».")
    return "\n".join(lines).strip()

def full_schedule_text(show_lists: bool = True) -> str:
    lines = []
    for code in slot_order():
        sl = slots.get(code)
        if not sl:
            continue
        users = sl["users"]
        taken = len(users)
        left  = max(CAPACITY - taken, 0)
        lines.append(f"{sl['title']} | занято: {taken}/{CAPACITY} | свободно: {left}\n\n")
        if show_lists:
            lines.append(roster_with_numbers(users))
        lines.append("")
    return "\n".join(lines).strip()

def find_candidates_by_query(query: str) -> List[Tuple[int, str]]:
    q = query.strip().lower()
    res: List[Tuple[int, str]] = []
    for uid, name in members_cache:
        if q.isdigit() and int(q) == uid:
            res.append((uid, name))
        elif q.startswith("id") and q[2:].isdigit() and int(q[2:]) == uid:
            res.append((uid, name))
        elif q in name.lower():
            res.append((uid, name))
    return res

def name_by_id(uid: int) -> str:
    try:
        info = session_api.users.get(user_ids=uid)[0]
        return f"{info.get('first_name','')} {info.get('last_name','')}".strip()
    except Exception:
        return str(uid)

# ───────────────── состояния ─────────────────
admin_states: Dict[int, Dict] = {}
pending_date: Dict[int, str] = {}  # для обычных пользователей (выбор даты перед временем)

# ───────────────── проверка токенов ─────────────────
try:
    gi = session_api.groups.getById(group_id=GROUP_ID)
    print("OK: доступ к группе есть:", gi[0]["name"])
except ApiError as e:
    print("Проблема с доступом к группе:", e)

print("Бот запущен. Нажми Ctrl+C для остановки.")

# ───────────────── основной цикл ─────────────────
try:
    while True:
        try:
            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW or not event.to_me:
                    continue

                raw = (event.text or "").strip()
                msg = raw
                mlow = raw.lower()
                user_id = event.user_id

                me = session_api.users.get(user_ids=user_id, fields="first_name,last_name")[0]
                fullname_me = f"{me.get('first_name','')} {me.get('last_name','')}".strip()

                is_admin = user_id in fetch_admin_ids()

                # ───── текстовые команды админа /get /set /debug_fs ─────
                if is_admin and raw.startswith("/"):
                    parts = raw.strip().split()
                    cmd = parts[0].lower()

                    if cmd == "/get":
                        info = (
                            "Текущие параметры:\n"
                            f"DAY1={DAY1}\nDAY2={DAY2}\n"
                            f"TIME1={TIME1}\nTIME2={TIME2}\n"
                            f"CAPACITY={CAPACITY}\nMAX_SLOTS_PER_USER={MAX_SLOTS_PER_USER}\n"
                            f"Режим слотов: {_mode_str()}"
                        )
                        send_msg(user_id, info, kb=admin_root_keyboard(), admin_view=True)
                        continue

                    if cmd == "/set" and len(parts) >= 5:
                        try:
                            d1, d2, t1, t2 = parts[1:5]
                            cap = None
                            mx  = None
                            if len(parts) >= 6:
                                try:
                                    cap = int(parts[5])
                                except ValueError:
                                    pass
                            if len(parts) >= 7:
                                try:
                                    mx = int(parts[6])
                                except ValueError:
                                    pass

                            # d2 или t2 могут быть "-" → отключение второго дня/времени
                            DAY1, DAY2, TIME1, TIME2 = d1, d2, t1, t2
                            TIMES[:] = _active_times()
                            if cap is not None:
                                CAPACITY = cap
                            if mx is not None:
                                MAX_SLOTS_PER_USER = mx

                            # пересоздаём пустые слоты (очищаем записи)
                            state.clear()
                            state.update({"slots": make_slots_map(DAY1, DAY2)})
                            slots.clear()
                            slots.update(state["slots"])
                            save_state()
                            save_globals()

                            msg_ok = (
                                "✅ Обновлено расписание и лимиты (если переданы):\n"
                                f"DAY1={DAY1}, DAY2={DAY2}\n"
                                f"TIME1={TIME1}, TIME2={TIME2}\n"
                                f"CAPACITY={CAPACITY}, MAX_SLOTS_PER_USER={MAX_SLOTS_PER_USER}\n"
                                f"Режим слотов: {_mode_str()}\n"
                                "Все записи очищены."
                            )
                            send_msg(user_id, msg_ok, kb=admin_root_keyboard(), admin_view=True)
                        except Exception as e:
                            send_msg(user_id, f"Ошибка при /set: {e}", kb=admin_root_keyboard(), admin_view=True)
                        continue

                                        # ----- расширенная команда /setp (2 независимых дня и 2 независимых времени) -----
                    if cmd == "/setp" and len(parts) >= 7:
                        try:
                            # схема:
                            # /setp d1 t1 d2 t2 cap max
                            d1 = parts[1]
                            t1 = parts[2]
                            d2 = parts[3]
                            t2 = parts[4]

                            cap = int(parts[5])
                            mx  = int(parts[6])

                            global DAY1, DAY2, TIME1, TIME2, CAPACITY, MAX_SLOTS_PER_USER

                            DAY1 = d1
                            TIME1 = t1
                            DAY2 = d2
                            TIME2 = t2

                            CAPACITY = cap
                            MAX_SLOTS_PER_USER = mx

                            TIMES[:] = _active_times()

                            # полная очистка + создание новых слотов
                            state.clear()
                            state.update({"slots": make_slots_map(DAY1, DAY2)})
                            slots.clear()
                            slots.update(state["slots"])

                            save_state()
                            save_globals()

                            msg_ok = (
                                "✅ Обновлено расширенное расписание (/setp):\n"
                                f"Слот 1: {DAY1} {TIME1}\n"
                                f"Слот 2: {DAY2} {TIME2}\n\n"
                                f"Вместимость: {CAPACITY}\n"
                                f"Макс. слотов на ученика: {MAX_SLOTS_PER_USER}\n\n"
                                "Все предыдущие записи очищены."
                            )
                            send_msg(user_id, msg_ok, kb=admin_root_keyboard(), admin_view=True)

                        except Exception as e:
                            send_msg(user_id, f"Ошибка при /setp: {e}", kb=admin_root_keyboard(), admin_view=True)
                        continue


                    if cmd == "/debug_fs":
                        here = os.getcwd()
                        has_state = os.path.exists(STATE_FILE)
                        has_cfg   = os.path.exists(CONFIG_FILE)
                        send_msg(
                            user_id,
                            f"CWD: {here}\n"
                            f"state.json: {'есть' if has_state else 'нет'}\n"
                            f"config_state.json: {'есть' if has_cfg else 'нет'}\n"
                            f"Gist: {'настроен' if (GIST_TOKEN and GIST_ID) else 'не настроен'}",
                            kb=admin_root_keyboard(),
                            admin_view=True
                        )
                        continue

                # ───── общая «Назад» ─────
                if msg == "Назад":
                    admin_states.pop(user_id, None)
                    pending_date.pop(user_id, None)
                    send_msg(
                        user_id,
                        "Меню администратора:" if is_admin else "Меню:",
                        kb=(admin_root_keyboard() if is_admin else user_keyboard()),
                        admin_view=is_admin
                    )
                    continue

                # ───── меню ученика ─────
                if not is_admin:
                    if mlow in {"старт", "start", "привет", "меню"}:
                        send_msg(user_id, "Меню:", kb=user_keyboard())
                        continue

                    if msg == "Инструкция":
                        help_text = (
                            "🧾 Инструкция\n\n"
                            "• Кнопка «Выбрать» → Записаться на слот. Выберите день, затем время.\n"
                            f"  Сейчас режим: {_mode_str()}, за неделю можно записаться только на один слот.\n\n"
                            "• Кнопка «Перезапись» → очистит ваши записи, затем можно заново записаться.\n\n"
                            "• Кнопка «Расписание» → краткая сводка; внутри кнопка «Подробно» покажет списки.\n\n"
                            "• Кнопка «Мои записи» → покажет ваши текущие записи."
                        )
                        send_msg(user_id, help_text, kb=user_keyboard())
                        continue

                    if msg == "Выбрать":
                        send_msg(user_id, "Выберите дату:", kb=date_keyboard())
                        continue

                    if msg in _active_days():
                        pending_date[user_id] = msg
                        send_msg(user_id, f"Дата {msg} выбрана. Теперь выберите время:", kb=time_keyboard())
                        continue

                    if msg in _active_times():
                        date_str = pending_date.get(user_id)
                        if not date_str:
                            send_msg(user_id, "Сначала выберите дату («Выбрать»).", kb=user_keyboard())
                            continue
                        code = slot_code_by(date_str, msg)
                        if not code:
                            send_msg(user_id, "Не удалось определить слот. Попробуйте ещё раз.", kb=user_keyboard())
                            continue
                        users = slots[code]["users"]
                        if fullname_me in users:
                            send_msg(user_id, "Вы уже записаны в этот слот.", kb=user_keyboard())
                            continue
                        if already_booked_count(fullname_me) >= MAX_SLOTS_PER_USER:
                            send_msg(user_id, "У вас уже есть запись. Используйте «Перезапись».", kb=user_keyboard())
                            continue
                        if len(users) >= CAPACITY:
                            send_msg(user_id, f"Лимит слота ({CAPACITY}) исчерпан.", kb=user_keyboard())
                            continue
                        users.append(fullname_me)
                        save_state()
                        pending_date.pop(user_id, None)
                        send_msg(user_id, f"✅ Записаны: {slots[code]['title']}", kb=user_keyboard())
                        continue

                    if msg == "Перезапись":
                        remove_user_from_all_slots(fullname_me)
                        save_state()
                        send_msg(user_id, "Ваши записи очищены. Нажмите «Выбрать», чтобы записаться заново.", kb=user_keyboard())
                        continue

                    if msg == "Мои записи":
                        my = ["• " + sc["title"] for sc in slots.values() if fullname_me in sc["users"]]
                        send_msg(
                            user_id,
                            "Вы никуда не записаны." if not my else "Ваши записи:\n" + "\n".join(my),
                            kb=user_keyboard()
                        )
                        continue

                    if msg == "Расписание":
                        send_msg(user_id, summarize_schedule_for_button(), kb=schedule_keyboard())
                        continue

                    if msg == "Подробно":
                        send_msg(user_id, full_schedule_text(show_lists=True), kb=schedule_keyboard())
                        continue

                    send_msg(user_id, "Меню:", kb=user_keyboard())
                    continue

                # ───── меню администратора ─────
                if is_admin:
                    if mlow in {"старт", "start", "привет", "меню"}:
                        send_msg(user_id, "Меню администратора:", kb=admin_root_keyboard(), admin_view=True)
                        continue

                    if msg == "Инструкция":
                        txt = (
                            "📘 Инструкция администратора\n\n"
                            "Режим слотов сейчас: " + _mode_str() + "\n\n"
                            "• «Расписание» — кратко; кнопка «Подробно» — со списками.\n\n"
                            "• «Ученики» → список всех участников (без админов).\n\n"
                            "• «Админы» → список администраторов.\n\n"
                            "• «Незаписавшиеся ученики» → ученики без записей.\n\n"
                            "• «Редактировать» → Записать/Удалить ученика вручную "
                            "(по порядковому номеру списка/ФИО/id).\n\n"
                            "Команды:\n"
                            "/get — просмотр актуальных параметров даты и времени.\n\n"
                            "/set d1 d2 t1 t2 [cap] [max]\n"
                            "  d2 = '-' → использовать только один день (d1).\n"
                            "  t2 = '-' → использовать только одно время (t1).\n\n"
                            "Примеры:\n"
                            "• 2 дня × 2 времени:\n"
                            "  /set 15.11 16.11 16:00-18:00 18:00-20:00 13 1\n\n"
                            "• 2 дня × 1 время:\n"
                            "  /set 15.11 16.11 16:00-18:00 - 13 1\n\n"
                            "• 1 день × 2 времени:\n"
                            "  /set 15.11 - 16:00-18:00 18:00-20:00 13 1\n\n"
                            "• 1 день × 1 время:\n"
                            "  /set 15.11 - 16:00-18:00 - 13 1\n\n"
                            "/debug_fs — проверить наличие файлов/Gist."
                        )
                        send_msg(user_id, txt, kb=admin_root_keyboard(), admin_view=True)
                        continue

                    if msg == "Расписание":
                        send_msg(user_id, summarize_schedule_for_button(), kb=schedule_keyboard(), admin_view=True)
                        continue

                    if msg == "Подробно":
                        send_msg(user_id, full_schedule_text(show_lists=True), kb=schedule_keyboard(), admin_view=True)
                        continue

                    if msg == "Ученики":
                        if not user_api:
                            send_msg(user_id, "USER_TOKEN недоступен.", kb=admin_root_keyboard(), admin_view=True)
                            continue
                        try:
                            members_cache = fetch_members_excluding_admins()
                            names = [name for (_uid, name) in sorted(members_cache, key=lambda x: x[1].lower())]
                            total = len(names)
                            lst = "\n".join(f"{i+1}. {name}" for i, name in enumerate(names)) or "—"
                            send_msg(user_id, f"👥 Ученики. Всего: {total}\n\n{lst}", kb=admin_root_keyboard(), admin_view=True)
                        except Exception as e:
                            send_msg(user_id, f"Ошибка: {e}", kb=admin_root_keyboard(), admin_view=True)
                        continue

                    if msg == "Админы":
                        ids = fetch_admin_ids()
                        names = users_get_names(ids)
                        total = len(names)
                        lst = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names)) or "—"
                        send_msg(user_id, f"🛡 Администраторы. Всего: {total}\n\n{lst}", kb=admin_root_keyboard(), admin_view=True)
                        continue

                    if msg == "Незаписавшиеся ученики":
                        if not user_api:
                            send_msg(user_id, "USER_TOKEN недоступен.", kb=admin_root_keyboard(), admin_view=True)
                            continue
                        try:
                            members_cache = fetch_members_excluding_admins()
                            booked_names = set()
                            for sc in slots.values():
                                booked_names.update(sc["users"])
                            not_booked = sorted(
                                [name for (_uid, name) in members_cache if name not in booked_names],
                                key=str.lower
                            )
                            lst = "\n".join(f"{i+1}. {nm}" for i, nm in enumerate(not_booked)) or "—"
                            send_msg(
                                user_id,
                                f"📋 Незаписавшиеся ученики ({len(not_booked)}):\n\n{lst}",
                                kb=admin_root_keyboard(),
                                admin_view=True
                            )
                        except Exception as e:
                            send_msg(user_id, f"Ошибка: {e}", kb=admin_root_keyboard(), admin_view=True)
                        continue

                    # РЕДАКТИРОВАНИЕ
                    st = admin_states.get(user_id) or {
                        "mode": None,
                        "candidates": [],
                        "pending_user": None,
                        "pending_day": None
                    }
                    admin_states[user_id] = st

                    if msg == "Редактировать":
                        send_msg(
                            user_id,
                            "Режим редактирования. Выберите действие:",
                            kb=admin_edit_keyboard(),
                            admin_view=True
                        )
                        continue

                    if msg == "Записать":
                        if not user_api:
                            send_msg(user_id, "USER_TOKEN недоступен.", kb=admin_root_keyboard(), admin_view=True)
                            continue
                        try:
                            members_cache = fetch_members_excluding_admins()
                            st["mode"] = "add"
                            st["candidates"] = sorted(members_cache, key=lambda x: x[1].lower())
                            st["pending_user"] = None
                            st["pending_day"] = None
                            booked = set()
                            for sc in slots.values():
                                booked.update(sc["users"])
                            unbooked = [(uid, name) for (uid, name) in st["candidates"] if name not in booked]
                            if not unbooked:
                                send_msg(
                                    user_id,
                                    "Все уже записаны.\n\nМожно искать по ФИО/id.\n"
                                    "Пришлите фамилию, ФИО, id или номер из списка.",
                                    kb=admin_edit_keyboard(),
                                    admin_view=True
                                )
                            else:
                                text = (
                                    "Незаписанные ученики (введите порядковый номер из списка/ФИО/id):\n\n" +
                                    "\n".join(f"{i+1}. {nm}" for i, (_uid, nm) in enumerate(unbooked[:50]))
                                )
                                st["candidates"] = unbooked
                                send_msg(user_id, text, kb=admin_edit_keyboard(), admin_view=True)
                        except Exception as e:
                            send_msg(user_id, f"Ошибка: {e}", kb=admin_root_keyboard(), admin_view=True)
                        continue

                    if msg == "Удалить":
                        st["mode"] = "remove"
                        st["pending_user"] = None
                        st["pending_day"] = None
                        booked_all = sorted({u for sc in slots.values() for u in sc["users"]}, key=str.lower)
                        if not booked_all:
                            send_msg(user_id, "Никто не записан.", kb=admin_edit_keyboard(), admin_view=True)
                        else:
                            text = (
                                "Записанные ученики (введите порядковый номер из списка/ФИО/id):\n\n" +
                                "\n".join(f"{i+1}. {nm}" for i, nm in enumerate(booked_all[:50]))
                            )
                            st["candidates"] = [(0, nm) for nm in booked_all]
                            send_msg(user_id, text, kb=admin_edit_keyboard(), admin_view=True)
                        continue

                    # ввод текста в режиме редактирования
                    if st["mode"] in {"add", "remove"}:
                        q = msg.strip()

                        if st["mode"] == "add" and st.get("pending_user") and q in _active_days():
                            st["pending_day"] = q
                            send_msg(
                                user_id,
                                f"День {q} выбран. Теперь выберите время:",
                                kb=admin_choose_time_keyboard(),
                                admin_view=True
                            )
                            continue

                        if st["mode"] == "add" and st.get("pending_user") and q in _active_times():
                            date_str = st.get("pending_day")
                            if not date_str:
                                send_msg(
                                    user_id,
                                    "Сначала выберите день.",
                                    kb=admin_choose_day_keyboard(),
                                    admin_view=True
                                )
                                continue
                            code = slot_code_by(date_str, q)
                            if not code:
                                send_msg(
                                    user_id,
                                    "Не удалось определить слот.",
                                    kb=admin_edit_keyboard(),
                                    admin_view=True
                                )
                                continue
                            uid, nm = st["pending_user"]
                            if already_booked_count(nm) >= MAX_SLOTS_PER_USER:
                                send_msg(
                                    user_id,
                                    f"У {nm} уже есть запись. Сначала удалите.",
                                    kb=admin_edit_keyboard(),
                                    admin_view=True
                                )
                                continue
                            if nm in slots[code]["users"]:
                                send_msg(
                                    user_id,
                                    f"{nm} уже записан в этот слот.",
                                    kb=admin_edit_keyboard(),
                                    admin_view=True
                                )
                                continue
                            if len(slots[code]["users"]) >= CAPACITY:
                                send_msg(
                                    user_id,
                                    "Слот заполнен.",
                                    kb=admin_edit_keyboard(),
                                    admin_view=True
                                )
                                continue
                            slots[code]["users"].append(nm)
                            save_state()
                            send_msg(
                                user_id,
                                f"✅ Записал: {nm} → {slots[code]['title']}",
                                kb=admin_root_keyboard(),
                                admin_view=True
                            )
                            admin_states.pop(user_id, None)
                            continue

                        chosen: Optional[Tuple[int, str]] = None
                        if q.isdigit() and st.get("candidates"):
                            idx = int(q) - 1
                            cand = st["candidates"]
                            if 0 <= idx < len(cand):
                                chosen = cand[idx]

                        if not chosen:
                            if st["mode"] == "add":
                                if not members_cache:
                                    try:
                                        members_cache = fetch_members_excluding_admins()
                                    except Exception as e:
                                        send_msg(
                                            user_id,
                                            f"Ошибка получения учеников: {e}",
                                            kb=admin_root_keyboard(),
                                            admin_view=True
                                        )
                                        continue
                                found = find_candidates_by_query(q)
                                if len(found) == 1:
                                    chosen = found[0]
                                elif len(found) > 1:
                                    text = (
                                        "Найдено несколько. Введите номер:\n" +
                                        "\n".join(
                                            f"{i+1}. {nm} (id{uid})"
                                            for i, (uid, nm) in enumerate(found[:50])
                                        )
                                    )
                                    st["candidates"] = found
                                    send_msg(user_id, text, kb=admin_edit_keyboard(), admin_view=True)
                                    continue
                            else:
                                booked_set = {u for sc in slots.values() for u in sc["users"]}
                                if q in booked_set:
                                    chosen = (0, q)
                                else:
                                    many = [u for u in sorted(booked_set) if q.lower() in u.lower()]
                                    if len(many) == 1:
                                        chosen = (0, many[0])
                                    elif len(many) > 1:
                                        text = (
                                            "Найдено несколько. Введите номер:\n" +
                                            "\n".join(f"{i+1}. {nm}" for i, nm in enumerate(many[:50]))
                                        )
                                        st["candidates"] = [(0, nm) for nm in many]
                                        send_msg(user_id, text, kb=admin_edit_keyboard(), admin_view=True)
                                        continue

                        if not chosen:
                            send_msg(
                                user_id,
                                "Не понял. Введите порядковый номер из списка/ФИО/id или используйте «Назад».",
                                kb=admin_edit_keyboard(),
                                admin_view=True
                            )
                            continue

                        uid, nm = chosen
                        if st["mode"] == "add":
                            st["pending_user"] = (uid, nm)
                            send_msg(
                                user_id,
                                f"Выбрали: {nm}\nТеперь выберите день:",
                                kb=admin_choose_day_keyboard(),
                                admin_view=True
                            )
                            continue

                        if st["mode"] == "remove":
                            existed = False
                            for sc in slots.values():
                                if nm in sc["users"]:
                                    sc["users"].remove(nm)
                                    existed = True
                            if existed:
                                save_state()
                                send_msg(user_id, f"🗑 Удалил: {nm}", kb=admin_root_keyboard(), admin_view=True)
                            else:
                                send_msg(
                                    user_id,
                                    f"{nm} нигде не найден в записях.",
                                    kb=admin_edit_keyboard(),
                                    admin_view=True
                                )
                            admin_states.pop(user_id, None)
                            continue

                    send_msg(user_id, "Меню администратора:", kb=admin_root_keyboard(), admin_view=True)
                    continue

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"⚠️ Сетевая ошибка: {e}. Повтор через 5 сек...")
            time.sleep(5)

except KeyboardInterrupt:
    print("\n🛑 Бот остановлен пользователем (Ctrl+C). До встречи!")

# ПАМЯТКА:
# 1) В Render добавь переменные GIST_TOKEN и GIST_ID.
# 2) В приватном Gist создай файлы state.json и config_state.json с содержимым {}.
# 3) /set обновляет даты/время и очищает записи, всё сразу пишется в локальные файлы и Gist.
# 4) При перезапуске/редеплое сперва читаем из Gist, затем из локальных файлов.
# 5) d2 = "-" → режим на один день; t2 = "-" → режим на одно время.
