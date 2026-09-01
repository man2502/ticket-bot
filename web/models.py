"""Схема базы.

Принципы, по которым она устроена:

* **Пассажиры и станции — общие.** Один и тот же пассажир летит самолётом
  и едет поездом; дублировать его в двух таблицах значило бы рано или поздно
  получить два разных написания фамилии в паспортных данных.

* **Параметры процесса лежат в JSON.** У самолёта и поезда разные поля (рейс
  против типа вагона и этажа полки), и заводить под них две таблицы с почти
  одинаковой обвязкой смысла нет. Проверку полей делает слой сервиса.

* **Секреты хранятся зашифрованными.** Поля с суффиксом `_enc` проходят через
  `web.crypto`; читать их напрямую нельзя.

* **Брони — отражение журнала бота, а не источник правды.** Настоящий журнал
  (`railway_bookings.json` / `api_bookings.json`) ведут сами боты, и именно он
  защищает от повторной брони. Здесь его копия для показа в вебе.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now()


# --------------------------------------------------------------------------- #
# Перечисления
# --------------------------------------------------------------------------- #


class Role(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class Service(str, enum.Enum):
    """Сервисы, которыми управляет панель. chapar и airlines_web сюда не входят."""

    AIRLINES = "airlines"  # airlines_api — HTTP API приложения T5
    RAILWAY = "railway"  # railway-api — api.railway.gov.tm


class JobStatus(str, enum.Enum):
    QUEUED = "queued"  # ждёт своей очереди: на сервис пускаем один процесс
    RUNNING = "running"
    BOOKED = "booked"  # бронь создана, есть ссылка на оплату
    NEEDS_REVIEW = "needs_review"  # бот вышел с кодом 2 — бронь под вопросом
    FAILED = "failed"
    STOPPED = "stopped"  # остановлен человеком


class LogLevel(str, enum.Enum):
    INFO = "info"
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


class CaptchaStatus(str, enum.Enum):
    PENDING = "pending"  # ждёт разметки админом
    LABELED = "labeled"  # админ ввёл правильный код
    DISCARDED = "discarded"  # нечитаемая картинка


class PaxType(str, enum.Enum):
    ADULT = "adult"
    CHILD = "child"
    INFANT = "infant"


# --------------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------------- #


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # scrypt из стандартной библиотеки — отдельный пакет для хеша не нужен.
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.USER.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="user")

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value


# --------------------------------------------------------------------------- #
# Пассажиры
# --------------------------------------------------------------------------- #


class Passenger(Base):
    """
    Общий справочник пассажиров для обоих сервисов.

    Поля повторяют формат `passengers.json`, который читают боты: воркер
    выгружает выбранных пассажиров в такой файл перед запуском.
    """

    __tablename__ = "passengers"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(64))
    last_name: Mapped[str] = mapped_column(String(64))
    # Хранится как в паспорте: ДД.ММ.ГГГГ. Тип пассажира считается от даты рейса.
    dob: Mapped[str] = mapped_column(String(16))
    gender: Mapped[str] = mapped_column(String(1), default="M")
    doc_number: Mapped[str] = mapped_column(String(32))
    doc_expdt: Mapped[str] = mapped_column(String(16), default="")
    citizenship: Mapped[str] = mapped_column(String(8), default="TM")
    email: Mapped[str] = mapped_column(String(128), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    # Пусто = определить по дате рождения на дату рейса (0-4 infant, 5-17 child).
    pax_type: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(255), default="")

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        # Один и тот же паспорт дважды в списке — почти всегда опечатка,
        # а в брони это ошибка сервера на этапе оплаты.
        UniqueConstraint("doc_number", name="uq_passenger_doc"),
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def to_bot_dict(self) -> dict[str, Any]:
        """Запись для passengers.json в формате, который ждут боты."""
        data: dict[str, Any] = {
            "id": str(self.id),
            "first_name": self.first_name,
            "last_name": self.last_name,
            "dob": self.dob,
            "gender": self.gender,
            "doc_number": self.doc_number,
            "doc_expdt": self.doc_expdt,
            "citizenship": self.citizenship,
            "email": self.email,
            "phone": self.phone,
        }
        if self.pax_type:
            data["type"] = self.pax_type
        return data


# --------------------------------------------------------------------------- #
# Справочники станций и портов
# --------------------------------------------------------------------------- #


class Station(Base):
    """
    Станция поезда или аэропорт — в одной таблице, разделены полем service.

    Данные тянутся из внешних источников (`GET /v2/stations` у railway,
    `GET /ru/get_internal_ports` у airlines) и обновляются кнопкой в веб-панели.
    Хранить локально нужно, чтобы форма создания процесса открывалась мгновенно
    и работала, когда сайт перевозчика лежит.
    """

    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(String(16), index=True)
    # У railway это числовой id, у airlines — код порта (ASB, MYP).
    external_id: Mapped[str] = mapped_column(String(32))
    title_ru: Mapped[str] = mapped_column(String(128), default="")
    title_en: Mapped[str] = mapped_column(String(128), default="")
    code: Mapped[str] = mapped_column(String(16), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("service", "external_id", name="uq_station_service_external"),
    )

    @property
    def title(self) -> str:
        return self.title_ru or self.title_en or self.code or self.external_id


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #


class Setting(Base):
    """
    Настройки приложения: SMTP, SMS-шлюз, получатель по умолчанию.

    Плоский key-value вместо колонок под каждый параметр — чтобы добавление
    настройки не требовало миграции. Значения с `is_secret=True` лежат
    зашифрованными; читать их надо через `web.services.settings`, а не напрямую.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ServiceConfig(Base):
    """
    Настройки одного сервиса: доступы и поведение цикла проверки.

    Про интервал. Свободного поля «проверять каждые N секунд» здесь намеренно
    нет: боты стреляют в фиксированные секунды минуты с джиттером, и произвольный
    интервал — прямой путь к бану. Выбор ограничен списком (10/30/60/120 с),
    а сдвинуть можно только секунду первой отметки.
    """

    __tablename__ = "service_configs"

    service: Mapped[str] = mapped_column(String(16), primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Доступы к сервису. Пароли зашифрованы (см. web.crypto).
    login: Mapped[str] = mapped_column(String(64), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    extra_login: Mapped[str] = mapped_column(String(64), default="")
    extra_password_enc: Mapped[str] = mapped_column(Text, default="")

    # Расписание проверок: период (10/30/60/120 с) и секунда первой отметки.
    # Раньше здесь были две галочки «окно :00» и «окно :30» — они умели выразить
    # только 30 и 60 секунд, а до railway вообще не доезжали.
    check_period_sec: Mapped[int] = mapped_column(Integer, default=30)
    check_at_second: Mapped[int] = mapped_column(Integer, default=0)

    # Ограничители, чтобы не словить бан.
    max_searches_per_hour: Mapped[int] = mapped_column(Integer, default=0)  # 0 = без лимита
    repeated_error_pause_after: Mapped[int] = mapped_column(Integer, default=3)
    rate_limit_pause_sec: Mapped[int] = mapped_column(Integer, default=150)

    # Капча: авторежим без ожидания человека — как договорились.
    captcha_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_max_attempts: Mapped[int] = mapped_column(Integer, default=5)

    # Автобронь. Выключенный режим = только уведомление о найденных местах.
    auto_book: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --------------------------------------------------------------------------- #
# Процессы поиска
# --------------------------------------------------------------------------- #


class Job(Base):
    """
    Один запуск поиска: маршрут, дата, пассажиры, параметры сервиса.

    `params_json` хранит то, что зависит от сервиса — номер рейса и время для
    самолёта, тип вагона и этаж полки для поезда. Схему этих полей знает
    соответствующий модуль в `web/services/`.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    service: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED.value, index=True)

    # Маршрут: для railway это id станций, для airlines — коды портов.
    source: Mapped[str] = mapped_column(String(32))
    destination: Mapped[str] = mapped_column(String(32))
    depart_date: Mapped[str] = mapped_column(String(16))  # ДД.ММ.ГГГГ

    params_json: Mapped[str] = mapped_column(Text, default="{}")

    # Куда слать ссылку на оплату. Пусто — берётся из общих настроек.
    notify_email: Mapped[str] = mapped_column(String(255), default="")
    notify_phone: Mapped[str] = mapped_column(String(32), default="")

    # pid нужен, чтобы остановить процесс; после выхода очищается.
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Каталог с временными check.env и passengers.json этого запуска.
    run_dir: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="jobs")
    passengers: Mapped[list["JobPassenger"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    # Без delete-orphan: удаление процесса не должно стирать бронь. Она есть
    # у перевозчика, за неё выставлен счёт, и в панели она остаётся без процесса.
    bookings: Mapped[list["Booking"]] = relationship(back_populates="job")

    @property
    def params(self) -> dict[str, Any]:
        try:
            data = json.loads(self.params_json or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @params.setter
    def params(self, value: dict[str, Any]) -> None:
        self.params_json = json.dumps(value, ensure_ascii=False)

    @property
    def is_active(self) -> bool:
        """Процесс занимает очередь сервиса."""
        return self.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


class JobPassenger(Base):
    """Связка процесса и пассажира. Порядок важен: он влияет на выбор мест."""

    __tablename__ = "job_passengers"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    passenger_id: Mapped[int] = mapped_column(ForeignKey("passengers.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)

    job: Mapped[Job] = relationship(back_populates="passengers")
    passenger: Mapped[Passenger] = relationship()


class JobLog(Base):
    """
    Строка вывода бота. Показывается в правой панели как терминал.

    Хранится всё, а не последние N строк: после неудачной охоты за билетом
    разбираться приходится именно по логу — где сработал backoff, где сервер
    отдал «Error Search», в какую секунду ушёл запрос.
    """

    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    level: Mapped[str] = mapped_column(String(8), default=LogLevel.INFO.value)
    text: Mapped[str] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="logs")


# --------------------------------------------------------------------------- #
# Брони
# --------------------------------------------------------------------------- #


class Booking(Base):
    """
    Результат брони — копия записи из журнала бота.

    `guard_key` — тот самый ключ из `core.booking_guard`, по которому бот не даёт
    забронировать второй раз. Через него веб показывает, что именно блокирует
    повторный запуск, и по нему же работает кнопка «разблокировать» у админа.

    `job_id` может быть пустым. Бронь существует у перевозчика независимо от
    того, жив ли поиск, который её создал: бота могли запустить из командной
    строки, а процесс — удалить уже после брони. Терять из-за этого ссылку
    на оплату нельзя, поэтому такие брони живут без процесса.
    """

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True, nullable=True
    )
    service: Mapped[str] = mapped_column(String(16))
    # done | unknown | failed — статусы из core.booking_guard.
    status: Mapped[str] = mapped_column(String(16))
    guard_key: Mapped[str] = mapped_column(String(64), default="")
    booking_number: Mapped[str] = mapped_column(String(64), default="")
    payment_url: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    job: Mapped[Job | None] = relationship(back_populates="bookings")


class NotificationLog(Base):
    """История отправок: кому ушла ссылка на оплату и ушла ли вообще."""

    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # SET NULL, а не каскад: запись о том, что ссылка на оплату ушла на такой-то
    # адрес, ценна сама по себе. Удаление процесса не должно стирать
    # доказательство отправки — особенно если бронь была настоящей и оплаченной.
    booking_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(16))  # email | sms
    recipient: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255), default="")
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------- #
# Капчи
# --------------------------------------------------------------------------- #


class CaptchaSample(Base):
    """
    Картинка капчи, которую CNN не смогла решить.

    Процесс поиска никогда не ждёт человека: не решилась — берём новую капчу,
    а эту складываем сюда. Админ размечает накопленное в свободное время,
    размеченное уходит в датасет для дообучения модели.
    """

    __tablename__ = "captcha_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    service: Mapped[str] = mapped_column(String(16), index=True)
    # Тоже SET NULL: размеченная капча остаётся в датасете и в журнале
    # независимо от того, жив ли процесс, при котором она появилась.
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    file_path: Mapped[str] = mapped_column(String(255))
    cnn_guess: Mapped[str] = mapped_column(String(16), default="")
    cnn_confidence: Mapped[float] = mapped_column(default=0.0)
    correct_code: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(
        String(16), default=CaptchaStatus.PENDING.value, index=True
    )
    labeled_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    labeled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class TrainingStatus(str, enum.Enum):
    RUNNING = "running"
    DONE = "done"  # обучение прошло, модель-кандидат лежит на диске
    FAILED = "failed"
    STOPPED = "stopped"  # остановлено человеком


class TrainingRun(Base):
    """
    Один запуск дообучения модели капчи.

    Обучение идёт около часа на процессоре, поэтому это отдельный процесс
    с живым логом — как и поиск. Но, в отличие от поиска, оно НЕ занимает
    очередь сервиса: останавливать поиск на час ради модели бессмысленно,
    билет важнее. Обучение всегда пишет в свой файл, а живую модель подменяет
    отдельное действие, и вот оно уже ждёт, пока самолётный поиск закончится.
    """

    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), default=TrainingStatus.RUNNING.value, index=True
    )
    started_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    pid: Mapped[int | None] = mapped_column(nullable=True)
    exit_code: Mapped[int | None] = mapped_column(nullable=True)

    # Сколько образцов было в датасете на момент запуска. Нужно, чтобы понимать,
    # стоит ли обучать снова: если с прошлого раза не прибавилось, незачем.
    samples: Mapped[int] = mapped_column(Integer, default=0)
    epochs: Mapped[int] = mapped_column(Integer, default=0)

    # Куда обучение сложило результат и что из этого вышло.
    model_path: Mapped[str] = mapped_column(String(255), default="")
    accuracy: Mapped[float | None] = mapped_column(nullable=True)
    # Точность модели, которая была живой на момент запуска, — чтобы панель
    # могла сказать «стало хуже» и не дать подменить вслепую.
    baseline_accuracy: Mapped[float | None] = mapped_column(nullable=True)

    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    logs: Mapped[list["TrainingLog"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class TrainingLog(Base):
    """Строка вывода обучения. Отдельно от job_logs: у обучения нет процесса поиска."""

    __tablename__ = "training_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("training_runs.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(8), default=LogLevel.INFO.value)
    text: Mapped[str] = mapped_column(Text)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    run: Mapped[TrainingRun] = relationship(back_populates="logs")
