"""Private, durable queue for the existing HA renderer (no HA dependencies).

This is a compatibility queue for prepared V7 scenes, not the V8 stay database.
Keep the complete submitted payload. Only hashes, never guest names, form paths.
All database methods must run in HA's executor. One renderer owns this database.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time

MODES = ("full", "presentation", "smart", "rules")


def require_current_stay(profile, entries, current=None):
    """Check version/status against HA's saved journal; old clients stay unbound."""
    stay_id = profile.get("stayId")
    if stay_id is None:
        return
    candidates = [item for item in entries if isinstance(item, dict) and item.get("id") == stay_id]
    if isinstance(current, dict) and current.get("id") == stay_id:
        candidates.append(current)
    if not candidates:
        raise ValueError("Заезд не найден: обновите карточку")
    stay = max(candidates, key=lambda item: int(item.get("version") or 1))
    if profile.get("mediaFingerprint"):
        validate_bound_profile(profile, stay)
        return
    if type(profile.get("stayVersion")) is not int or profile["stayVersion"] != int(stay.get("version") or 1):
        raise ValueError("Карточка заезда изменилась: сохраните актуальный профиль и повторите сборку")
    if str(stay.get("status", "")).lower() in {"completed", "archived", "cancelled", "canceled", "deleted"}:
        raise ValueError("Заезд завершён: персональные ролики больше не формируются")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def job_identity(payload):
    mode = payload.get("mode", "full")
    if mode not in MODES:
        raise ValueError("Invalid video mode")
    profile = json.loads(payload.get("source_signature", ""))
    if not isinstance(profile, dict):
        raise ValueError("Invalid video profile")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 48:
        raise ValueError("Expected 1..48 scenes")
    # Both the profile and prepared scenes contribute: a new voice/audio or
    # artwork must not overwrite a previously issued private playback URL.
    value = {"mode": mode, "profile": profile, "scenes": scenes}
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class TvRenderQueue:
    def __init__(self, root):
        self.root = Path(root)
        self.db_path = self.root / "render_queue.sqlite3"

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as c:
            c.execute("PRAGMA journal_mode=WAL")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported render queue schema")
            c.execute("""CREATE TABLE IF NOT EXISTS render_jobs (
                id TEXT PRIMARY KEY CHECK(length(id)=64),
                stay_id TEXT, stay_version INTEGER, mode TEXT NOT NULL,
                payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                state TEXT NOT NULL CHECK(state IN ('queued','building','ready','error')),
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS render_jobs_state ON render_jobs(state,created_at,id)")
            c.execute("CREATE INDEX IF NOT EXISTS render_jobs_stay ON render_jobs(stay_id,stay_version,mode)")
            c.execute("PRAGMA user_version=1")
            result = c.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("Render queue integrity check failed")
        self.db_path.chmod(0o600)

    def paths(self, payload):
        job_id = job_identity(payload)
        directory = self.root / "jobs" / job_id
        mode = payload.get("mode", "full")
        name = "welcome.mp4" if mode == "full" else mode + ".mp4"
        return directory / name, directory / "status.json"

    def enqueue(self, payload):
        job_id = job_identity(payload)
        profile = json.loads(payload["source_signature"])
        stay_id = profile.get("stayId")
        stay_version = profile.get("stayVersion")
        if stay_id is not None and (not isinstance(stay_id, str) or len(stay_id) > 255):
            raise ValueError("Invalid stay ID")
        if stay_version is not None and (type(stay_version) is not int or stay_version < 1):
            raise ValueError("Invalid stay version")
        encoded = canonical(payload)
        output, status_file = self.paths(payload)
        status_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT state FROM render_jobs WHERE id=?", (job_id,)).fetchone()
            if row and row["state"] in ("queued", "building"):
                return job_id, row["state"]
            if row and row["state"] == "ready" and output.is_file() and output.stat().st_size > 0:
                try:
                    status = json.loads(status_file.read_text("utf-8"))
                    if status.get("state") == "ready" and status.get("render_job_id") == job_id:
                        return job_id, "ready"
                except (OSError, ValueError, AttributeError):
                    pass
            c.execute("""INSERT INTO render_jobs
                (id,stay_id,stay_version,mode,payload_json,state,created_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?) ON CONFLICT(id) DO UPDATE SET
                state='queued',error=NULL,updated_at=excluded.updated_at""",
                (job_id, stay_id, stay_version, payload.get("mode", "full"), encoded, now, now))
        return job_id, "queued"

    def recover(self):
        """Only call once during process startup, before starting the worker."""
        with self.connection() as c:
            c.execute("UPDATE render_jobs SET state='queued',updated_at=? WHERE state='building'", (time.time(),))

    def claim(self):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM render_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            c.execute("UPDATE render_jobs SET state='building',attempts=attempts+1,updated_at=? WHERE id=?", (time.time(), row["id"]))
            payload = json.loads(row["payload_json"])
            if job_identity(payload) != row["id"]:
                raise ValueError("Render payload checksum mismatch")
            return row["id"], payload

    def finish(self, job_id, *, error=None):
        with self.connection() as c:
            row = c.execute("SELECT payload_json,state FROM render_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "building":
                raise ValueError("Job is not being rendered")
            if error is None:
                output, status_file = self.paths(json.loads(row["payload_json"]))
                status = json.loads(status_file.read_text("utf-8"))
                if status.get("state") != "ready" or not output.is_file() or output.stat().st_size <= 0:
                    raise ValueError("Ready video was not produced")
            c.execute("UPDATE render_jobs SET state=?,error=?,updated_at=? WHERE id=?",
                      ("error" if error is not None else "ready", str(error)[:1000] if error is not None else None, time.time(), job_id))

    def statistics(self):
        with self.connection() as c:
            counts = {row["state"]: row["n"] for row in c.execute("SELECT state,count(*) AS n FROM render_jobs GROUP BY state")}
        return {"schema_version": 1, **{state: counts.get(state, 0) for state in ("queued", "building", "ready", "error")}}


def status_output(root, status):
    """Accept only server-generated hashes; retain old ready movies in place."""
    root = Path(root)
    mode = status.get("mode", "full")
    if mode not in MODES:
        raise ValueError("Invalid video mode")
    name = "welcome.mp4" if mode == "full" else mode + ".mp4"
    bundle_key = status.get("bundle_key")
    if bundle_key is not None:
        if mode != "full" or not isinstance(bundle_key, str) or not __import__('re').fullmatch(r"[a-f0-9]{64}", bundle_key):
            raise ValueError("Invalid bundle key")
        return root / "bundles" / bundle_key / "full.mp4"
    job_id = status.get("render_job_id")
    if job_id is None:
        return root / name
    if not isinstance(job_id, str) or len(job_id) != 64 or any(ch not in "0123456789abcdef" for ch in job_id):
        raise ValueError("Invalid render job ID")
    return root / "jobs" / job_id / name


MEDIA_TEXT = {'MONTHS': ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'], 'PRESENTATION': [['presentation_location', 'Посёлок и расположение', 'Дом находится в охраняемом посёлке «Балтийская слобода 2», примерно в двадцати минутах от КАД при обычной дорожной обстановке. У въезда работают магазины, кафе и автозаправка.'], ['presentation_lake', 'Озеро и отдых рядом', 'Примерно в ста метрах находится озеро площадью около двух с половиной гектаров, с песчаным пляжем и пирсом. Рядом есть детские и спортивные площадки, прогулочные маршруты, рыбалка и конные прогулки.'], ['presentation_house', 'Дом и участок', 'Современный дом расположен на благоустроенном участке шестнадцать соток. Для гостей доступны вместительная парковка, две террасы, два балкона и архитектурная вечерняя подсветка.'], ['presentation_living', 'Гостиная и второй свет', 'Центр дома — просторная гостиная, объединённая с кухней и столовой. Второй свет и круговой балкон сохраняют ощущение большого открытого пространства.'], ['presentation_bedrooms', 'Спальни и ванные комнаты', 'В доме четыре спальни с новой мебелью и ортопедическими матрасами, две просторные душевые, гидромассажная ванна и отдельные санузлы на этажах.'], ['presentation_kitchen', 'Оснащённая кухня', 'Кухня оборудована варочной поверхностью, духовкой, микроволновой печью, холодильником с морозильным отделением, посудомоечной машиной, фильтром воды и чайником. Посуда, столовые приборы, кастрюли, сковороды и принадлежности для приготовления уже на месте.'], ['presentation_comfort', 'Бытовой комфорт', 'Постельное бельё и полотенца подготовлены. Доступны стиральная машина, утюг с гладильной доской, фен и места для хранения; в комнатах есть москитные сетки и шторы блэкаут.'], ['presentation_menu', 'Главное меню умного дома', 'Управление домом собрано в одном интерфейсе: главная, интерактивный план, управление, Wi-Fi, профиль гостя и помощь.'], ['presentation_lights', 'Свет на интерактивном плане', 'На реальном плане выберите доступный этаж и коснитесь нужной точки. План сразу подтверждает новое состояние освещения.', 'lights'], ['presentation_climate', 'Отопление и проветривание', 'Для помещений доступен индивидуальный температурный режим. Откройте климатическую точку, выберите температуру и подождите, пока система плавно её достигнет.', 'climate'], ['presentation_territory', 'Территория и барбекю', 'В разделе управления доступны разрешённые вам ворота, камера и наружное освещение. Для отдыха подготовлены веранда, садовая мебель и зона барбекю с решётками и шампурами.', 'gate'], ['presentation_vehicles', 'Автомобили и пропуска', 'В разделе «Автомобили и пропуска» отображаются автомобили текущего заезда и история каждого въезда и выезда. Чтобы оформить пропуск, откройте «Оформить пропуск», введите государственный номер и при необходимости комментарий, затем отправьте заявку администратору. Здесь же можно выбрать голосовой режим: только первый приезд, каждый въезд и выезд или без голосового информирования.', 'gate'], ['presentation_multimedia', 'Мультимедиа', 'Телевизоры, музыкальная система, караоке и светомузыка помогают выбрать настроение отдыха.', 'music'], ['presentation_spa', 'SPA-комплекс', 'Русская парная, бассейн и водопад доступны только по вашему пакету и расписанию.', 'spa'], ['presentation_help', 'Wi-Fi, профиль и помощь', 'Данные Wi-Fi и QR-код находятся в одноимённом разделе. В профиле видны ваши даты, разрешённые зоны и доступы. Если понадобится помощь, откройте одноимённый раздел и свяжитесь с администратором.'], ['presentation_alice', 'Голосовой помощник Алиса', 'Скажите: «Алиса, включи навык Помощник по дому». После запуска навыка Алиса переходит в режим помощника Relax47: можно свободно задавать вопросы о доме, доступных функциях, правилах и отдыхе. Чтобы завершить диалог, скажите: «Алиса, хватит».']], 'RULES': [['rule_quiet', 'Тишина после 22:00', 'После двадцати двух часов соблюдайте тишину на улице и не включайте громкую музыку.'], ['rule_doors', 'Двери и окна', 'Ночью не оставляйте наружные двери и окна открытыми надолго.'], ['rule_fire', 'Открытый огонь', 'Не разводите огонь в доме. Используйте только специально оборудованные места.'], ['rule_fireworks', 'Пиротехника запрещена', 'Салюты, фейерверки, петарды и другая пиротехника запрещены на всей территории посёлка.'], ['rule_smoking', 'Курение', 'Курите только в специально обозначенных местах.'], ['rule_equipment', 'Оборудование и мебель', 'Не перемещайте крупную мебель и не меняйте настройки инженерных систем. При необходимости обратитесь к администратору.'], ['rule_security', 'Технический контроль для комфорта', 'Пожалуйста, не закрывайте камеры и датчики. Уличный режим тишины контролируется акустическими датчиками, открытие дверей — контактными датчиками, а проход в закрытые зоны — камерами и датчиками присутствия. Системы не требуют действий от гостей и помогают бережно соблюдать ограничения ради общего комфорта и безопасности.'], ['rule_clean', 'Порядок и мусор', 'Сохраняйте порядок и выбрасывайте мусор только в предусмотренных местах.']], 'SPA_RULES': [['spa_rule_schedule', 'Ваше время в SPA', 'Пользуйтесь SPA только в назначенное вам время.'], ['spa_rule_water', 'Чистая и безопасная вода', 'Не приносите к бассейну еду, посуду и алкоголь и ничего не выливайте в воду.'], ['spa_rule_heater', 'Каменка: только чистая вода', 'На каменку подавайте только чистую воду, без химии, масел и ароматизаторов.'], ['spa_rule_filter', 'Фильтрацию оставим автоматике', 'Не отключайте и не перенастраивайте фильтрацию и доочистку.'], ['spa_rule_skimmer', 'Скиммер и водозабор', 'Не закрывайте решётки и держите руки, волосы и предметы подальше от водозабора.'], ['spa_rule_engineering', 'Оборудование обслуживает персонал', 'Газовое, электрическое и инженерное оборудование обслуживает персонал.']], 'meta': {'welcome': ['WELCOME', 'Персональное приветствие'], 'presentation_location': ['LOCATION', 'Посёлок и расположение'], 'presentation_lake': ['LAKE', 'Озеро и отдых рядом'], 'presentation_house': ['HOUSE', 'Дом и участок'], 'presentation_living': ['LIVING', 'Гостиная и второй свет'], 'presentation_bedrooms': ['BEDROOMS', 'Спальни и ванные'], 'presentation_kitchen': ['KITCHEN', 'Оснащённая кухня'], 'presentation_comfort': ['COMFORT', 'Бытовой комфорт'], 'presentation_menu': ['MENU', 'Главное меню'], 'presentation_lights': ['LIGHTS', 'Интерактивный план'], 'presentation_climate': ['CLIMATE', 'Климат'], 'presentation_territory': ['TERRITORY', 'Территория и барбекю'], 'presentation_vehicles': ['VEHICLES', 'Автомобили и пропуска'], 'presentation_multimedia': ['MULTIMEDIA', 'Мультимедиа'], 'presentation_spa': ['SPA', 'SPA по вашему доступу'], 'presentation_help': ['HELP', 'Wi-Fi, профиль и помощь'], 'presentation_alice': ['ALICE', 'Голосовой помощник Алиса'], 'presentation_finish': ['FINISH', 'Презентация завершена'], 'rules_welcome': ['RULES', 'Правила проживания'], 'bridge_to_rules': ['RULES', 'Важные правила'], 'rule_quiet': ['QUIET', 'Правило 1 из 8'], 'rule_doors': ['DOORS', 'Правило 2 из 8'], 'rule_fire': ['FIRE', 'Правило 3 из 8'], 'rule_fireworks': ['FIREWORKS', 'Правило 4 из 8'], 'rule_smoking': ['SMOKING', 'Правило 5 из 8'], 'rule_equipment': ['EQUIPMENT', 'Правило 6 из 8'], 'rule_security': ['SECURITY', 'Правило 7 из 8'], 'rule_clean': ['CLEAN', 'Правило 8 из 8'], 'spa_rule_schedule': ['S1', 'SPA · время'], 'spa_rule_water': ['S2', 'SPA · вода'], 'spa_rule_heater': ['S3', 'SPA · каменка'], 'spa_rule_filter': ['S4', 'SPA · фильтрация'], 'spa_rule_skimmer': ['S5', 'SPA · водозабор'], 'spa_rule_engineering': ['S6', 'SPA · оборудование'], 'rules_finish': ['FINISH', 'Правила завершены'], 'tour_finish': ['FINISH', 'Полный курс завершён']}, 'images': {'WELCOME': ['/local/relax47/welcome/media/01_PRESENTATION/01_WELCOME_house_aerial_day.jpeg'], 'LOCATION': ['/local/relax47/welcome/media/01_PRESENTATION/02_LOCATION_gate_baltic_freedom.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/02_LOCATION_gate_gas_magnit.png', '/local/relax47/welcome/media/01_PRESENTATION/03_LAKE_and_settlement_aerial.png'], 'LAKE': ['/local/relax47/welcome/media/01_PRESENTATION/03_LAKE_and_settlement_aerial.png'], 'HOUSE': ['/local/relax47/welcome/media/01_PRESENTATION/04_HOUSE_SITE_aerial_full.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/04_HOUSE_SITE_rear_terraces.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/04_HOUSE_SITE_night_lighting.jpeg'], 'LIVING': ['/local/relax47/welcome/media/01_PRESENTATION/05_LIVING_double_height.jpeg'], 'BEDROOMS': ['/local/relax47/welcome/media/01_PRESENTATION/06_BEDROOM_main.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/06_BATH_hydromassage.jpeg'], 'KITCHEN': ['/local/relax47/welcome/media/01_PRESENTATION/07_KITCHEN_main.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/07_KITCHEN_alt.jpeg'], 'COMFORT': ['/local/relax47/welcome/media/01_PRESENTATION/08_HOUSEHOLD_washer.jpeg'], 'MENU': ['/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/09_UI_main_guest_menu.jpg'], 'LIGHTS': ['/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/10_UI_light_01_plan.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/10_UI_light_02_room.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/10_UI_light_03_controls.jpg'], 'CLIMATE': ['/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/11_UI_climate_01_plan.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/11_UI_climate_02_room.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/11_UI_climate_03_setpoint.jpg'], 'TERRITORY': ['/local/relax47/welcome/media/01_PRESENTATION/12_TERRITORY_terrace.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/12_TERRITORY_barbecue.jpeg'], 'VEHICLES': ['/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/12_UI_vehicles_01_overview.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/12_UI_vehicles_02_pass_request.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/12_UI_vehicles_03_notifications.jpg'], 'MULTIMEDIA': ['/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/13_UI_multimedia_controls.jpg', '/local/relax47/welcome/media/01_PRESENTATION/13_MULTIMEDIA_living_tv.jpeg'], 'SPA': ['/local/relax47/welcome/media/01_PRESENTATION/14_SPA_exterior.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/14_SPA_pool_sauna.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/14_SPA_pool_waterfall.jpeg', '/local/relax47/welcome/media/01_PRESENTATION/14_SPA_sauna.jpeg'], 'HELP': ['/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/15_UI_wifi.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/15_UI_profile.jpg', '/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/15_UI_help.jpg'], 'ALICE': ['/local/relax47/welcome/media/03_UI_CAPTURE_REQUIRED/16_UI_alice_assistant.jpg'], 'FINISH': ['/local/relax47/welcome/media/01_PRESENTATION/04_HOUSE_SITE_night_lighting.jpeg'], 'RULES': ['/local/relax47/welcome/media/01_PRESENTATION/04_HOUSE_SITE_night_lighting.jpeg'], 'QUIET': ['/local/relax47/welcome/media/02_RULES/17_RULE_quiet_after_22.png'], 'DOORS': ['/local/relax47/welcome/media/02_RULES/18_RULE_doors_windows.jpeg'], 'FIRE': ['/local/relax47/welcome/media/02_RULES/19_RULE_open_fire.jpeg'], 'FIREWORKS': ['/local/relax47/welcome/media/02_RULES/20_RULE_no_fireworks.png'], 'SMOKING': ['/local/relax47/welcome/media/02_RULES/21_RULE_smoking.jpeg'], 'EQUIPMENT': ['/local/relax47/welcome/media/02_RULES/22_RULE_equipment_furniture.jpeg'], 'SECURITY': ['/local/relax47/welcome/media/02_RULES/23_RULE_cameras_sensors.jpeg'], 'CLEAN': ['/local/relax47/welcome/media/02_RULES/24_RULE_order_trash_v2.webp'], 'S1': ['/local/relax47/welcome/media/02_RULES/25_RULE_spa_time.jpeg'], 'S2': ['/local/relax47/welcome/media/02_RULES/26_RULE_pool_clean_safe_water_v2.webp'], 'S3': ['/local/relax47/welcome/media/02_RULES/27_RULE_heater_clean_water_v2.webp'], 'S4': ['/local/relax47/welcome/media/02_RULES/28_RULE_filtration_automatic.jpeg'], 'S5': ['/local/relax47/welcome/media/02_RULES/29_RULE_skimmer_intake_v2.webp'], 'S6': ['/local/relax47/welcome/media/02_RULES/30_RULE_staff_equipment_v2.webp']}}

# Server-owned stay media pipeline, release media-2.0.
import asyncio
import copy
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime

MEDIA_REVISION = 'stay-media-2.0'
PARTS = ('presentation', 'smart', 'rules')
ALL_MODES = ('full',) + PARTS
TERMINAL_STAYS = {'completed', 'archived', 'cancelled', 'canceled', 'deleted'}


def media_profile(stay, voices):
    """Only fields that affect narration, not notes, money, lifecycle or version."""
    access = stay.get('access_snapshot') or {}
    spa = stay.get('spa_snapshot') or {}
    sessions = []
    for item in spa.get('sessions', []):
        sessions.append({
            'date': str(item.get('date') or '')[:10],
            'start': str(item.get('start') or ''),
            'durationHours': item.get('durationHours', item.get('duration_hours', 0)),
            'mode': item.get('mode', 'heated_spa'),
            'repeat': item.get('repeat', 'once'),
            'byAgreement': bool(item.get('byAgreement', item.get('by_agreement', False))),
        })
    sessions.sort(key=canonical)
    return {'stayId': str(stay['id']), 'guest': str(stay.get('guest') or '').strip(),
            'checkIn': str(stay.get('check_in') or '').replace(' ', 'T')[:19],
            'checkOut': str(stay.get('check_out') or '').replace(' ', 'T')[:19],
            'floors': [bool(x) for x in access.get('floors', [True, True, False])],
            'thirdFloorRooms': int(access.get('third_floor_rooms') or 0),
            'access': {k: bool(access.get(k)) for k in ('gate','lights','climate','spa','pool','music','tvSocket','contact')},
            'spaByAgreement': bool(spa.get('by_agreement')),
            'spaSessions': sessions if access.get('spa') else [],
            'voices': dict(voices), 'revision': MEDIA_REVISION}


def media_key(profile):
    return hashlib.sha256(canonical(profile).encode()).hexdigest()


def source_profile(stay, voices):
    p = media_profile(stay, voices)
    p.update(stayVersion=int(stay.get('version') or 1), mediaFingerprint=media_key(p),
             videoRevision='stay-profile-media-manifest-v5',
             mediaManifest='2026-08-22.1:a91d40900b6c63521d4c403d4fb0ea30a329ecd3b87abd7794d4a6053802c82e',
             yandexVoice=voices.get('yandex', 'Марина · дружелюбная'))
    return p


def validate_bound_profile(profile, stay):
    if str(stay.get('status', '')).lower() in TERMINAL_STAYS:
        raise ValueError('Заезд завершён или отменён')
    expected = media_key(media_profile(stay, profile.get('voices', {})))
    if profile.get('mediaFingerprint') != expected:
        raise ValueError('Параметры ролика изменились; сохраните карточку и обновите комплект')


def spoken_date(value):
    d = datetime.fromisoformat(value)
    return f'{d.day} {MEDIA_TEXT["MONTHS"][d.month-1]} {d.year} года в {d:%H:%M}'


def spa_description(p):
    if not p['access']['spa']:
        return ''
    items = []
    for s in p['spaSessions'][:3]:
        date = s['date']
        if s['repeat'] == 'daily':
            date = 'ежедневно, начиная с ' + date
        if s['mode'] == 'without_heating':
            items.append(f'{date}: посещение без топки')
        elif s['byAgreement'] or not s['start']:
            items.append(f'{date}: время по согласованию с администратором')
        else:
            items.append(f'{date}: начало в {s["start"]}, продолжительность {s["durationHours"]} ч')
    return 'Для вас предусмотрен SPA-комплекс. ' + ('; '.join(items) + '.' if items else 'Время согласуйте с администратором.') + (' Полное расписание доступно в карточке заезда.' if len(p['spaSessions']) > 3 else '')


def build_media_scenes(profile):
    """Use the approved text and images; future stays are fully personalized."""
    p = profile
    if not p['guest'] or not p['stayId']:
        raise ValueError('Нужна сохранённая карточка заезда')
    if datetime.fromisoformat(p['checkOut']) <= datetime.fromisoformat(p['checkIn']):
        raise ValueError('Некорректный период заезда')
    floors = 'В вашем распоряжении первый и второй этажи.'
    if len(p['floors']) > 2 and p['floors'][2]:
        floors += f' На третьем этаже доступны комнаты: {p["thirdFloorRooms"]}.'
    welcome = (f'{p["guest"]}, добро пожаловать в Relax47! Ваше время проживания: '
               f'с {spoken_date(p["checkIn"])} по {spoken_date(p["checkOut"])}. '
               f'{floors} {spa_description(p)}')
    def scene(scene_id, title, message):
        visual, kicker = MEDIA_TEXT['meta'].get(scene_id, ['FINISH','Relax47'])
        images = MEDIA_TEXT['images'][visual]
        return {'id': scene_id, 'title': title, 'message': message.strip(),
                'kicker': kicker, 'image_paths': images, 'image_path': images[0]}
    parts = {'presentation': [scene('welcome', 'Добро пожаловать', welcome)],
             'smart': [], 'rules': []}
    house_ids = {'presentation_location','presentation_lake','presentation_house',
                 'presentation_living','presentation_bedrooms','presentation_kitchen',
                 'presentation_comfort','presentation_territory','presentation_spa'}
    for row in MEDIA_TEXT['PRESENTATION']:
        key, title, message, *feature = row
        if feature and not p['access'].get(feature[0]):
            continue
        mode = 'presentation' if key in house_ids else 'smart'
        parts[mode].append(scene(key, title, message))
    parts['rules'].append(scene('rules_welcome','Правила проживания',
        'Уважаемые гости, несколько важных правил помогут сохранить комфорт и безопасность дома.'))
    for key,title,message in MEDIA_TEXT['RULES']:
        parts['rules'].append(scene(key,title,message))
    if p['access']['spa']:
        for key,title,message in MEDIA_TEXT['SPA_RULES']:
            if key == 'spa_rule_schedule': message = spa_description(p) + ' ' + message
            parts['rules'].append(scene(key,title,message))
    parts['rules'].append(scene('rules_finish','Приятного отдыха',
        'Спасибо. Все правила доступны в интерфейсе в любое время. Приятного отдыха!'))
    return parts


class StayMediaQueue(TvRenderQueue):
    """Separate versioned tables; existing immutable render jobs remain intact."""
    def initialize_media(self):
        self.initialize()
        with self.connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS media_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            v = c.execute("SELECT value FROM media_meta WHERE key='schema'").fetchone()
            if v and v[0] != '1': raise ValueError('Unknown media schema')
            c.execute('''CREATE TABLE IF NOT EXISTS stay_media (
                stay_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, profile_json TEXT NOT NULL,
                state TEXT NOT NULL, due_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0, error TEXT, published_key TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS media_bundles (
                fingerprint TEXT PRIMARY KEY, stay_id TEXT NOT NULL, manifest_json TEXT NOT NULL,
                created_at REAL NOT NULL)''')
            c.execute("INSERT OR IGNORE INTO media_meta VALUES ('schema','1')")
            c.execute("UPDATE stay_media SET state='queued' WHERE state IN ('preparing','building')")

    def observe(self, stay, voices, now=None, manual=False):
        now = time.time() if now is None else now
        p = source_profile(stay, voices)
        key = p['mediaFingerprint']
        closed = str(stay.get('status', '')).lower() not in {'planned','active','current','preparing','ready'}
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM stay_media WHERE stay_id=?', (p['stayId'],)).fetchone()
            if row is None:
                # Backfill existing stays without immediately launching all old media.
                state = 'cancelled' if closed else 'queued' if manual else 'scheduled'
                c.execute('INSERT INTO stay_media (stay_id,fingerprint,profile_json,state,due_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                          (p['stayId'],key,canonical(p),state,now if manual else now+7200,now,now))
            else:
                changed = row['fingerprint'] != key
                state, due = row['state'], row['due_at']
                if closed: state, due = 'cancelled', None
                elif changed:
                    # Pending first preparation follows latest saved inputs at the original deadline.
                    if state in ('scheduled','queued','preparing','building') and not row['published_key']:
                        state, due = 'scheduled', due if due is not None else now
                    else: state, due = 'outdated', None
                if manual and not closed and (changed or state not in ('queued','preparing','building','ready')):
                    state, due = 'queued', now
                c.execute('UPDATE stay_media SET fingerprint=?,profile_json=?,state=?,due_at=?,updated_at=?,attempt=CASE WHEN ? THEN 0 ELSE attempt END,error=CASE WHEN ? THEN NULL ELSE error END WHERE stay_id=?',
                          (key,canonical(p),state,due,now,changed or manual,changed or manual,p['stayId']))
            return dict(c.execute('SELECT * FROM stay_media WHERE stay_id=?',(p['stayId'],)).fetchone())

    def reconcile_ids(self, ids):
        with self.connection() as c:
            for row in c.execute('SELECT stay_id FROM stay_media').fetchall():
                if row['stay_id'] not in ids:
                    c.execute("UPDATE stay_media SET state='cancelled',due_at=NULL WHERE stay_id=?", (row['stay_id'],))

    def claim_media(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM stay_media WHERE state IN ('queued','scheduled') AND due_at<=? ORDER BY due_at,created_at LIMIT 1",(now,)).fetchone()
            if row is None: return None
            c.execute("UPDATE stay_media SET state='preparing',attempt=attempt+1,updated_at=? WHERE stay_id=?",(now,row['stay_id']))
            return json.loads(row['profile_json'])

    def set_phase(self, p, phase):
        with self.connection() as c:
            c.execute('UPDATE stay_media SET state=?,updated_at=? WHERE stay_id=? AND fingerprint=? AND state IN (\'preparing\',\'building\')',
                      (phase,time.time(),p['stayId'],p['mediaFingerprint']))

    def publish(self, p, manifest):
        if set(manifest) != set(ALL_MODES): raise ValueError('Incomplete media bundle')
        for mode, value in manifest.items():
            if value.get('state') != 'ready' or value.get('mode') != mode: raise ValueError('Unverified part')
            source = json.loads(value['source_signature'])
            if source.get('mediaFingerprint') != p['mediaFingerprint'] or source.get('stayId') != p['stayId']:
                raise ValueError('Foreign media part')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT fingerprint,state FROM stay_media WHERE stay_id=?',(p['stayId'],)).fetchone()
            if not row or row['fingerprint'] != p['mediaFingerprint'] or row['state'] not in ('preparing','building'):
                return False
            c.execute('INSERT OR REPLACE INTO media_bundles VALUES (?,?,?,?)',
                      (p['mediaFingerprint'],p['stayId'],canonical(manifest),time.time()))
            c.execute("UPDATE stay_media SET state='ready',error=NULL,published_key=?,updated_at=? WHERE stay_id=?",(p['mediaFingerprint'],time.time(),p['stayId']))
            return True

    def fail(self,p,error):
        with self.connection() as c:
            row=c.execute('SELECT attempt FROM stay_media WHERE stay_id=? AND fingerprint=? AND state IN (\'preparing\',\'building\')', (p['stayId'],p['mediaFingerprint'])).fetchone()
            if row:
                retry=row['attempt'] < 3
                c.execute('UPDATE stay_media SET state=?,due_at=?,error=?,updated_at=? WHERE stay_id=?',
                          ('scheduled' if retry else 'error',time.time()+60*2**row['attempt'] if retry else None,
                           str(error)[:500],time.time(),p['stayId']))

    def legacy_media(self, stay):
        # Keep matching old full/rules playable during the first new bundle build.
        result={}
        if str(stay.get('status','')).lower() in TERMINAL_STAYS: return result
        with self.connection() as c:
            rows=c.execute("SELECT payload_json FROM render_jobs WHERE stay_id=? AND stay_version=? AND state='ready' ORDER BY updated_at DESC", (stay['id'],int(stay.get('version') or 1))).fetchall()
        for row in rows:
            payload=json.loads(row[0]);mode=payload.get('mode')
            if mode not in ('full','rules') or mode in result: continue
            output,path=self.paths(payload)
            try:
                status=json.loads(path.read_text('utf-8'))
                if status.get('state')=='ready' and output.is_file(): result[mode]=status
            except (OSError,ValueError): pass
        return result

    def get_media(self, stay_id):
        with self.connection() as c:
            row=c.execute('SELECT * FROM stay_media WHERE stay_id=?',(stay_id,)).fetchone()
            if row is None: return None
            result=dict(row)
            bundle=c.execute('SELECT manifest_json FROM media_bundles WHERE fingerprint=? AND stay_id=?',
                             (row['fingerprint'],stay_id)).fetchone() if row['state']=='ready' else None
            result['manifest']=json.loads(bundle[0]) if bundle else {}
            return result


def concat_media(root, profile, manifest):
    """Remux compatible verified parts. No TTS and no video re-encoding."""
    directory=Path(root)/'bundles'/profile['mediaFingerprint']
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    inputs=[status_output(root,manifest[m]).resolve() for m in PARTS]
    ffmpeg,ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: raise RuntimeError('ffmpeg/ffprobe unavailable')
    durations=[]; contracts=[]
    for path in inputs:
        if not path.is_file(): raise ValueError('Missing media part')
        probe=subprocess.run([ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,check=True,timeout=30)
        data=json.loads(probe.stdout)
        streams=data['streams']
        contracts.append([{k:s.get(k) for k in ('codec_type','codec_name','width','height','pix_fmt','r_frame_rate','time_base','sample_rate','channels')} for s in streams])
        durations.append(float(data['format']['duration']))
    if any(c!=contracts[0] for c in contracts): raise ValueError('Incompatible media streams')
    listing=directory/'concat.txt'
    listing.write_text(''.join("file '"+str(p).replace("'", "'\\''")+"'\n" for p in inputs),'utf-8')
    temporary=directory/'full.tmp.mp4'; output=directory/'full.mp4'
    subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-map','0:v:0','-map','0:a:0','-c','copy','-movflags','+faststart',str(temporary)],capture_output=True,check=True,timeout=300)
    probe=subprocess.run([ffprobe,'-v','error','-show_entries','format=duration','-of','json',str(temporary)],capture_output=True,text=True,check=True,timeout=30)
    duration=float(json.loads(probe.stdout)['format']['duration'])
    if abs(duration-sum(durations)) > max(1.0,len(inputs)*0.1): raise ValueError('Joined duration mismatch')
    temporary.replace(output)
    return {'state':'ready','mode':'full','bundle_key':profile['mediaFingerprint'],
            'source_signature':canonical(profile),'size_bytes':output.stat().st_size,
            'section_count':sum(manifest[m].get('section_count',0) for m in PARTS),
            'scene_count':sum(manifest[m].get('scene_count',0) for m in PARTS),
            'duration':duration,'validation':'passed','updated_at':time.time()}


async def async_setup_stay_media(hass, root, render, render_lock, issue_url):
    """All preparation is server-owned. UI only saves, requests and polls."""
    from homeassistant.core import SupportsResponse
    import voluptuous as vol
    from custom_components.relax47_rbac import async_require_role, async_role_for_user, ROLE_ADMINISTRATOR, ROLE_GUEST
    logger=logging.getLogger(__name__)
    q=StayMediaQueue(root)
    await hass.async_add_executor_job(q.initialize_media)
    stopping=False
    wake=asyncio.Event()
    observe_lock=asyncio.Lock()
    def voices():
        def value(entity,default):
            s=hass.states.get(entity)
            return s.state if s and s.state not in ('unknown','unavailable','') else default
        return {'engine':value('input_select.voice_engine','Yandex SpeechKit'),
                'yandex':value('input_select.voice_yandex_voice','Марина · дружелюбная'),
                'piper':value('input_select.voice_piper_voice','ru_RU-irina-medium'),
                'piperEntity':value('input_text.voice_tts_entity','tts.piper')}
    def entries():
        s=hass.states.get('sensor.relax47_guest_journal')
        return [copy.deepcopy(x) for x in (s.attributes.get('entries',[]) if s else []) if isinstance(x,dict) and x.get('id')]
    def lookup(stay_id):
        return next((x for x in entries() if x['id']==stay_id),None)
    async def publish_summary():
        def counts():
            with q.connection() as c:
                return {r['state']:r['n'] for r in c.execute('SELECT state,count(*) AS n FROM stay_media GROUP BY state')}
        values=await hass.async_add_executor_job(counts)
        hass.states.async_set('sensor.relax47_stay_media', 'error' if values.get('error') else 'working' if values.get('preparing') or values.get('building') else 'ready',
            {'friendly_name':'RELAX47 · ролики по заездам','pipeline_version':MEDIA_REVISION,'storage_backend':'SQLite','counts':values})
    async def observe_all(_event=None):
        async with observe_lock:
            rows=entries()
            if not hass.states.get('sensor.relax47_guest_journal'): return
            config=voices()
            for stay in rows:
                await hass.async_add_executor_job(q.observe,stay,config)
            await hass.async_add_executor_job(q.reconcile_ids,{x['id'] for x in rows})
        wake.set()
        await publish_summary()
    async def authorize(call,write=False):
        stay_id=str(call.data.get('stay_id') or '')
        if write: await async_require_role(hass,call,ROLE_ADMINISTRATOR)
        elif await async_role_for_user(hass,call.context.user_id)==ROLE_GUEST:
            current=hass.states.get('sensor.relax47_current_stay')
            current_stay=current.attributes.get('stay',{}) if current else {}
            if not current or current.state!='active' or current_stay.get('id')!=stay_id or not current_stay.get('access_snapshot',{}).get('tvSocket'):
                raise ValueError('Материалы доступны только для вашего активного заезда')
        stay=lookup(stay_id)
        if stay is None: raise ValueError('Сохранённый заезд не найден')
        return stay
    async def media_status(stay):
        row=await hass.async_add_executor_job(q.get_media,stay['id'])
        if not row: return {'stay_id':stay['id'],'state':'scheduled','videos':{},'version':stay.get('version')}
        result={'stay_id':stay['id'],'version':stay.get('version'),'state':row['state'],
                'due_at':row['due_at'],'error':row['error'],'fingerprint':row['fingerprint'],'videos':{}}
        expected=media_key(media_profile(stay,voices()))
        if expected!=row['fingerprint']:
            result['state']='outdated';return result
        selected_manifest=row['manifest']
        if not selected_manifest and row['state'] not in ('cancelled','outdated'):
            selected_manifest=await hass.async_add_executor_job(q.legacy_media,stay)
        for mode,status in selected_manifest.items():
            path=status_output(root,status)
            exists=await hass.async_add_executor_job(path.is_file)
            if not exists:
                result['state']='error';result['error']='Готовый файл отсутствует';result['videos']={};break
            url,expires=issue_url(hass,path,'video/mp4')
            result['videos'][mode]={'state':'ready','ready':True,'mediaPath':url,'mediaExpiresAt':expires,
                                  'sceneCount':status.get('scene_count',0),'sourceSignature':status['source_signature']}
        return result
    async def status_service(call):
        stay=await authorize(call)
        return await media_status(stay)
    async def request_service(call):
        stay=await authorize(call,True)
        expected=int(call.data.get('expected_version') or 0)
        if expected!=int(stay.get('version') or 1): raise ValueError('Карточка изменилась: обновите её перед сборкой')
        async with observe_lock:
            await hass.async_add_executor_job(lambda:q.observe(stay,voices(),manual=True))
        wake.set()
        result=await media_status(stay)
        return result if call.return_response else None
    hass.services.async_register('relax47_localtuya_admin','get_stay_videos',status_service,schema=vol.Schema({vol.Required('stay_id'):str,vol.Optional('expected_version'):int}),supports_response=SupportsResponse.ONLY)
    hass.services.async_register('relax47_localtuya_admin','prepare_stay_videos',request_service,schema=vol.Schema({vol.Required('stay_id'):str,vol.Required('expected_version'):int}),supports_response=SupportsResponse.OPTIONAL)
    async def audio_for(scene,p):
        from homeassistant.components import tts
        from homeassistant.components.tts.const import DATA_TTS_MANAGER
        settings=p['voices']
        engines=[(settings['piperEntity'],'ru_RU',settings['piper'])]
        if settings['engine']=='Yandex SpeechKit':
            engines.insert(0,('tts.relax47_yandex_speechkit','ru-RU',settings['yandex']))
        last=None
        for engine,language,voice in engines:
            key=hashlib.sha256(canonical([engine,language,voice,scene['message']]).encode()).hexdigest()
            target=Path(hass.config.path('tts','r47-'+key+'.mp3'))
            if await hass.async_add_executor_job(lambda:target.is_file() and target.stat().st_size>0):
                return '/api/tts_proxy/'+target.name
            try:
                async with asyncio.timeout(120):
                    stream=hass.data[DATA_TTS_MANAGER].async_create_result_stream(engine,language=language,options={'voice':voice,'preferred_format':'mp3'})
                    stream.async_set_message(scene['message'])
                    audio=b''.join([part async for part in stream.async_stream_result()])
                if not audio: raise ValueError('Синтез речи вернул пустой результат')
                def save_audio():
                    target.parent.mkdir(parents=True,exist_ok=True)
                    tmp=target.with_suffix('.tmp')
                    tmp.write_bytes(audio);tmp.replace(target)
                await hass.async_add_executor_job(save_audio)
                return '/api/tts_proxy/'+target.name
            except Exception as err:
                last=err
        raise RuntimeError('Не удалось подготовить озвучку: '+type(last).__name__) from last
    async def verify(p):
        stay=lookup(p['stayId'])
        if not stay: raise ValueError('Заезд удалён')
        validate_bound_profile(p,stay)
        if p['voices']!=voices(): raise ValueError('Настройки голоса изменились')
    async def build(p):
        await verify(p)
        parts=build_media_scenes(p)
        manifest={}
        for mode in PARTS:
            await verify(p)
            scenes=[]
            for scene in parts[mode]:
                await verify(p)
                scenes.append({**scene,'audio_path':await audio_for(scene,p)})
            payload={'mode':mode,'scenes':scenes,'source_signature':canonical(p)}
            output,status_file=q.paths(payload)
            await hass.async_add_executor_job(lambda:status_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700))
            await hass.async_add_executor_job(q.set_phase,p,'building')
            async with render_lock:
                await render(payload)
            status=json.loads(await hass.async_add_executor_job(status_file.read_text,'utf-8'))
            if status.get('state')!='ready': raise ValueError('Монтаж части не завершён')
            manifest[mode]=status
        await verify(p)
        async with render_lock:
            manifest['full']=await hass.async_add_executor_job(concat_media,root,p,manifest)
        await verify(p)
        await hass.async_add_executor_job(q.publish,p,manifest)
    async def worker():
        while not stopping:
            await wake.wait();wake.clear()
            while not stopping:
                p=await hass.async_add_executor_job(q.claim_media)
                if p is None: break
                await publish_summary()
                try: await build(p)
                except asyncio.CancelledError: raise
                except Exception as err:
                    logger.exception('Stay media preparation failed (%s)',p['mediaFingerprint'])
                    await hass.async_add_executor_job(q.fail,p,str(err))
                await publish_summary()
    async def scanner():
        while not stopping:
            try:
                if hass.is_running: await observe_all()
            except asyncio.CancelledError: raise
            except Exception:
                logger.exception('Stay media reconciliation failed')
            await asyncio.sleep(30)
    remove_listeners=[hass.bus.async_listen(event,observe_all) for event in ('relax47_stay_saved','relax47_stay_completed','relax47_stay_started')]
    worker_task=hass.async_create_background_task(worker(),'relax47_stay_media_worker')
    scan_task=hass.async_create_background_task(scanner(),'relax47_stay_media_reconcile')
    async def stop(_event):
        nonlocal stopping
        stopping=True
        for remove in remove_listeners: remove()
        scan_task.cancel()
        # Let in-flight executor montage finish before Core shuts down; SQL state recovers next start.
        worker_task.cancel()
        await asyncio.gather(scan_task,worker_task,return_exceptions=True)
    hass.bus.async_listen_once('homeassistant_stop',stop)
    hass.data['relax47_stay_media']={'queue':q,'status':media_status}
