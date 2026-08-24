"""Тесты настроек, секретов и отправки уведомлений.

Главное здесь — секреты. Пароль SMTP и ключ SMS не должны попадать ни в HTML,
ни в логи, а пустое поле формы не должно их стирать: иначе правка соседней
галочки молча обнуляет пароль, и это выясняется в момент брони.

Сеть не трогается: SMTP и HTTP подменяются.

Запуск (из корня репозитория):
    python run_tests.py web/test_settings.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import web.config as config
    import web.crypto as crypto
    import web.db as db_module

    monkeypatch.setenv("TICKET_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(crypto, "SECRET_KEY_FILE", tmp_path / "secret.key")
    monkeypatch.setattr(crypto, "SESSION_KEY_FILE", tmp_path / "session.key")
    monkeypatch.delenv("TICKET_SECRET_KEY", raising=False)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(db_module, "_SessionFactory", None)

    db_module.init_db()
    with db_module.session_scope() as session:
        yield session


# --------------------------------------------------------------------------
# Секреты
# --------------------------------------------------------------------------


def test_secret_is_stored_encrypted(db) -> None:
    """В базе должен лежать шифротекст, а не пароль."""
    from web.models import Setting
    from web.services.settings import save_settings

    save_settings(db, {"smtp_password": "мой-пароль-приложения"})

    raw = db.get(Setting, "smtp_password").value
    assert raw
    assert "мой-пароль" not in raw


def test_secret_reads_back(db) -> None:
    from web.services.settings import get_secret, save_settings

    save_settings(db, {"smtp_password": "тайна-42"})
    assert get_secret(db, "smtp_password") == "тайна-42"


def test_secret_never_reaches_template(db) -> None:
    """
    В шаблон уходит только признак «задан».

    Иначе пароль читался бы прямо из исходного кода страницы.
    """
    from web.services.settings import get_all, save_settings

    save_settings(db, {"smtp_password": "тайна-42"})
    values = get_all(db)

    assert values["smtp_password_set"] is True
    assert "smtp_password" not in values
    assert "тайна-42" not in str(values)


def test_empty_field_keeps_secret(db) -> None:
    """Пустое поле — «не менять». Иначе правка соседнего поля стирает пароль."""
    from web.services.settings import get_secret, save_settings

    save_settings(db, {"smtp_password": "тайна-42"})
    save_settings(db, {"smtp_host": "smtp.example.tm", "smtp_password": ""})

    assert get_secret(db, "smtp_password") == "тайна-42"


def test_mask_value_keeps_secret(db) -> None:
    """Если браузер вернул маску, это тоже «не менять»."""
    from web.services.settings import MASK, get_secret, save_settings

    save_settings(db, {"smtp_password": "тайна-42"})
    save_settings(db, {"smtp_password": MASK})

    assert get_secret(db, "smtp_password") == "тайна-42"


def test_dash_clears_secret(db) -> None:
    """Стереть пароль можно, но только явным действием."""
    from web.services.settings import get_secret, save_settings

    save_settings(db, {"smtp_password": "тайна-42"})
    save_settings(db, {"smtp_password": "-"})

    assert get_secret(db, "smtp_password") == ""


def test_unreadable_secret_returns_empty_not_crash(db, tmp_path: Path) -> None:
    """
    Сменился ключ — получаем пустую строку и предупреждение, а не исключение.

    Пустой пароль всё равно приведёт к отказу авторизации, но панель при этом
    останется работоспособной.
    """
    from cryptography.fernet import Fernet

    from web.services.settings import get_secret, save_settings

    save_settings(db, {"smtp_password": "тайна-42"})
    (tmp_path / "secret.key").write_bytes(Fernet.generate_key())

    assert get_secret(db, "smtp_password") == ""


def test_plain_setting_is_not_encrypted(db) -> None:
    from web.models import Setting
    from web.services.settings import save_settings

    save_settings(db, {"smtp_host": "smtp.example.tm"})
    assert db.get(Setting, "smtp_host").value == "smtp.example.tm"


# --------------------------------------------------------------------------
# Настройки сервисов
# --------------------------------------------------------------------------


def test_service_password_is_encrypted(db) -> None:
    from web.models import ServiceConfig
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"password_enc": "пароль-кабинета"})

    assert "пароль-кабинета" not in db.get(ServiceConfig, "airlines").password_enc


def test_service_password_reads_back(db) -> None:
    from web.services.settings import get_service_secret, save_service_config

    save_service_config(db, "airlines", {"password_enc": "пароль-кабинета"})
    assert get_service_secret(db, "airlines", "password_enc") == "пароль-кабинета"


def test_service_view_hides_passwords(db) -> None:
    from web.services.settings import save_service_config, service_config_view

    save_service_config(db, "airlines", {"password_enc": "пароль-кабинета"})
    view = service_config_view(db, "airlines")

    assert view["password_set"] is True
    assert "пароль-кабинета" not in str(view)


def test_toggling_checkbox_keeps_password(db) -> None:
    """Самый частый сценарий потери пароля — правка соседней настройки."""
    from web.services.settings import get_service_secret, save_service_config

    save_service_config(db, "airlines", {"password_enc": "пароль-кабинета"})
    save_service_config(db, "airlines", {"auto_book": False, "password_enc": ""})

    assert get_service_secret(db, "airlines", "password_enc") == "пароль-кабинета"


@pytest.mark.parametrize(
    "field, given, expected",
    [
        # Нулевая пауза после 429 — гарантия бана вместо билета.
        ("rate_limit_pause_sec", "0", 30),
        ("rate_limit_pause_sec", "99999", 3600),
        ("check_at_second", "99", 59),
        ("check_at_second", "-5", 0),
        ("max_searches_per_hour", "5000", 200),
        ("captcha_max_attempts", "0", 1),
    ],
)
def test_numeric_limits_are_clamped(db, field: str, given: str, expected: int) -> None:
    """Интерфейс не должен позволять выставить самоубийственные значения."""
    from web.services.settings import save_service_config

    config = save_service_config(db, "airlines", {field: given})
    assert getattr(config, field) == expected


def test_garbage_number_keeps_previous(db) -> None:
    from web.services.settings import save_service_config

    save_service_config(db, "airlines", {"rate_limit_pause_sec": "200"})
    config = save_service_config(db, "airlines", {"rate_limit_pause_sec": "быстро"})
    assert config.rate_limit_pause_sec == 200


def test_railway_needs_no_credentials(db) -> None:
    """API поездов открытый — требовать логин было бы неверно."""
    from web.services.settings import missing_credentials

    assert missing_credentials(db, "railway") == []


def test_airlines_reports_missing_credentials(db) -> None:
    from web.services.settings import missing_credentials

    assert len(missing_credentials(db, "airlines")) == 3

    from web.services.settings import save_service_config

    save_service_config(
        db,
        "airlines",
        {"login": "+99361234567", "password_enc": "п", "extra_password_enc": "и"},
    )
    assert missing_credentials(db, "airlines") == []


# --------------------------------------------------------------------------
# Почта
# --------------------------------------------------------------------------


def _setup_smtp(db) -> None:
    from web.services.settings import save_settings

    save_settings(
        db,
        {
            "smtp_host": "smtp.example.tm",
            "smtp_port": "587",
            "smtp_user": "bot@example.tm",
            "smtp_password": "пароль",
        },
    )


class _FakeSMTP:
    """Заглушка smtplib: запоминает вызовы, ничего не отправляет."""

    sent: list[tuple[str, list[str], str]] = []
    used_starttls = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        type(self).used_starttls = True

    def login(self, user, password):
        self.user = user

    def sendmail(self, sender, recipients, body):
        type(self).sent.append((sender, recipients, body))


def test_email_is_sent(db, monkeypatch: pytest.MonkeyPatch) -> None:
    import web.services.notifications as notifications

    _setup_smtp(db)
    _FakeSMTP.sent = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)

    result = notifications.send_email(db, to="a@b.tm", subject="Тема", body="Текст")

    assert result.ok is True
    assert len(_FakeSMTP.sent) == 1


def test_email_without_settings_is_rejected(db) -> None:
    from web.services.notifications import send_email

    result = send_email(db, to="a@b.tm", subject="Тема", body="Текст")
    assert result.ok is False
    assert "не настроен" in result.error


def test_several_recipients(db, monkeypatch: pytest.MonkeyPatch) -> None:
    import web.services.notifications as notifications

    _setup_smtp(db)
    _FakeSMTP.sent = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)

    notifications.send_email(db, to="a@b.tm, c@d.tm; a@b.tm", subject="Т", body="Т")

    _sender, recipients, _body = _FakeSMTP.sent[0]
    assert recipients == ["a@b.tm", "c@d.tm"]  # дубль отброшен


def test_port_465_uses_ssl(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """На 465 порту starttls не работает — нужен SSL с самого начала."""
    import web.services.notifications as notifications

    _setup_smtp(db)
    from web.services.settings import save_settings

    save_settings(db, {"smtp_port": "465"})

    used: list[str] = []

    class _SSL(_FakeSMTP):
        def __init__(self, host, port, timeout=None, context=None):
            used.append("ssl")
            super().__init__(host, port)

    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", _SSL)
    notifications.send_email(db, to="a@b.tm", subject="Т", body="Т")

    assert used == ["ssl"]


def test_auth_error_mentions_app_password(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Самая частая причина отказа Gmail — обычный пароль вместо пароля приложения."""
    import smtplib

    import web.services.notifications as notifications

    _setup_smtp(db)

    class _Failing(_FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"denied")

    monkeypatch.setattr(notifications.smtplib, "SMTP", _Failing)
    result = notifications.send_email(db, to="a@b.tm", subject="Т", body="Т")

    assert result.ok is False
    assert "пароль приложения" in result.error


# --------------------------------------------------------------------------
# SMS
# --------------------------------------------------------------------------


def _setup_sms(db, **over) -> None:
    from web.services.settings import save_settings

    data = {
        "sms_url": "https://sms.example.tm/send",
        "sms_phone_field": "phone",
        "sms_message_field": "message",
        "sms_api_key_field": "api_key",
        "sms_api_key_place": "body",
        "sms_api_key": "ключ-шлюза",
    }
    data.update(over)
    save_settings(db, data)


class _FakeResponse:
    """Ответ шлюза. Повторяет core.http.Response в объёме, который читает send_sms."""

    def __init__(self, status=200):
        self.status = status
        self.elapsed_ms = 12.0
        self.text = '{"ok":true}'

    @property
    def json(self):
        import json as _json

        try:
            return _json.loads(self.text)
        except ValueError:
            return None

    def safe_text(self, limit=600):
        return self.text


def _capture_sms(monkeypatch: pytest.MonkeyPatch, status: int = 200) -> dict:
    """Подменить HTTP-клиент и запомнить, что именно ушло в шлюз."""
    import web.services.notifications as notifications

    captured: dict = {}

    class _FakeClient:
        def __init__(self, base, headers_factory=None, **kw):
            captured["base"] = base
            captured["headers"] = dict(headers_factory()) if headers_factory else {}

        def request(self, method, path, body=None, query=None, idempotent=None, **kw):
            captured.update(
                method=method, path=path, body=body, query=query, idempotent=idempotent
            )
            return _FakeResponse(status)

        def close(self):
            pass

    monkeypatch.setattr(notifications, "HttpClient", _FakeClient)
    return captured


def test_sms_is_sent_with_configured_fields(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.services.notifications import send_sms

    _setup_sms(db, sms_phone_field="msisdn", sms_message_field="text")
    captured = _capture_sms(monkeypatch)

    result = send_sms(db, phone="+99361234567", message="Привет")

    assert result.ok is True
    assert captured["body"]["msisdn"] == "+99361234567"
    assert captured["body"]["text"] == "Привет"


def test_sms_is_never_retried(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Повтор дал бы человеку второе сообщение вместо одного."""
    from web.services.notifications import send_sms

    _setup_sms(db)
    captured = _capture_sms(monkeypatch)
    send_sms(db, phone="+99361234567", message="Привет")

    assert captured["idempotent"] is False


def test_api_key_in_body(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.services.notifications import send_sms

    _setup_sms(db, sms_api_key_place="body")
    captured = _capture_sms(monkeypatch)
    send_sms(db, phone="+99361234567", message="П")

    assert captured["body"]["api_key"] == "ключ-шлюза"


def test_api_key_in_header(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.services.notifications import send_sms

    _setup_sms(db, sms_api_key_place="header", sms_api_key_field="X-Api-Key")
    captured = _capture_sms(monkeypatch)
    send_sms(db, phone="+99361234567", message="П")

    assert captured["headers"]["X-Api-Key"] == "ключ-шлюза"
    assert "X-Api-Key" not in captured["body"]


def test_api_key_in_query(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.services.notifications import send_sms

    _setup_sms(db, sms_api_key_place="query")
    captured = _capture_sms(monkeypatch)
    send_sms(db, phone="+99361234567", message="П")

    assert captured["query"]["api_key"] == "ключ-шлюза"


def test_sms_without_key_sends_nothing_extra(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ необязателен — без него в теле только номер и текст."""
    from web.services.notifications import send_sms

    _setup_sms(db, sms_api_key="-")
    captured = _capture_sms(monkeypatch)
    send_sms(db, phone="+99361234567", message="П")

    assert set(captured["body"]) == {"phone", "message"}


def test_http_url_is_rejected(db) -> None:
    """Ключ шлюза не должен уходить открытым текстом."""
    from web.services.notifications import send_sms

    _setup_sms(db, sms_url="http://sms.example.tm/send")
    result = send_sms(db, phone="+99361234567", message="П")

    assert result.ok is False
    assert "https" in result.error


def test_gateway_error_is_reported(db, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.services.notifications import send_sms

    _setup_sms(db)
    _capture_sms(monkeypatch, status=401)
    result = send_sms(db, phone="+99361234567", message="П")

    assert result.ok is False
    assert "401" in result.error


def test_sms_without_url_is_rejected(db) -> None:
    from web.services.notifications import send_sms

    assert send_sms(db, phone="+99361234567", message="П").ok is False


# --------------------------------------------------------------------------
# Рассылка о брони
# --------------------------------------------------------------------------


def test_booking_uses_default_recipients(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Если в процессе получатель не задан — берём из общих настроек."""
    import web.services.notifications as notifications
    from web.services.settings import save_settings

    _setup_smtp(db)
    save_settings(db, {"default_notify_email": "boss@example.tm"})
    _FakeSMTP.sent = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)

    results = notifications.notify_booking(db, subject="Т", body="Текст")

    assert len(results) == 1
    assert _FakeSMTP.sent[0][1] == ["boss@example.tm"]


def test_job_recipient_overrides_default(db, monkeypatch: pytest.MonkeyPatch) -> None:
    import web.services.notifications as notifications
    from web.services.settings import save_settings

    _setup_smtp(db)
    save_settings(db, {"default_notify_email": "boss@example.tm"})
    _FakeSMTP.sent = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)

    notifications.notify_booking(db, subject="Т", body="Т", email="me@example.tm")

    assert _FakeSMTP.sent[0][1] == ["me@example.tm"]


def test_sends_are_logged(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Потом видно, ушло письмо или нет, и не приходится гадать."""
    import web.services.notifications as notifications
    from web.models import NotificationLog

    _setup_smtp(db)
    _FakeSMTP.sent = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", _FakeSMTP)

    notifications.notify_booking(db, subject="Т", body="Т", email="a@b.tm")

    log = db.query(NotificationLog).one()
    assert log.channel == "email" and log.is_sent is True


def test_failed_send_is_logged_too(db) -> None:
    from web.models import NotificationLog
    from web.services.notifications import notify_booking

    # SMTP не настроен — отправка провалится, но запись остаться должна.
    notify_booking(db, subject="Т", body="Т", email="a@b.tm")

    log = db.query(NotificationLog).one()
    assert log.is_sent is False and log.error


def test_booking_message_contains_link() -> None:
    from web.services.notifications import format_booking_message

    subject, body, sms = format_booking_message(
        route="ASB → MYP, 10.08.2026",
        payment_url="https://bank.tm/pay/1",
        booking_number="RW-1",
        passengers="TEST ANVAR",
    )

    assert "RW-1" in subject
    assert "https://bank.tm/pay/1" in body
    assert "https://bank.tm/pay/1" in sms
    assert len(sms) < 200  # влезет в SMS


# --------------------------------------------------------------------------
# Шлюз отвечает 200, но отказывает
# --------------------------------------------------------------------------


class _BodyResponse(_FakeResponse):
    """Ответ с разбираемым телом — как у настоящего шлюза."""

    def __init__(self, payload, status=200):
        super().__init__(status)
        self._payload = payload

    @property
    def json(self):
        return self._payload


def _capture_with_body(monkeypatch: pytest.MonkeyPatch, payload, status: int = 200):
    import web.services.notifications as notifications

    class _FakeClient:
        def __init__(self, base, headers_factory=None, **kw):
            pass

        def request(self, *a, **kw):
            return _BodyResponse(payload, status)

        def close(self):
            pass

    monkeypatch.setattr(notifications, "HttpClient", _FakeClient)


@pytest.mark.parametrize(
    "payload",
    [
        # Ровно то, что отвечает наш шлюз при нехватке параметров.
        {"success": "false", "message": "<phone> yada <message> param gelmedi"},
        {"success": False, "message": "нет баланса"},
        {"status": "error", "message": "номер заблокирован"},
        {"error": "invalid number"},
        {"ok": False},
    ],
)
def test_gateway_failure_in_body_is_not_success(
    db, monkeypatch: pytest.MonkeyPatch, payload
) -> None:
    """
    Код 200 сам по себе ничего не значит.

    Шлюз может ответить 200 и сообщить об отказе в теле. Поверив статусу,
    панель записала бы «доставлено» там, где SMS не ушла, и человек ждал бы
    ссылку на оплату впустую.
    """
    from web.services.notifications import send_sms

    _setup_sms(db)
    _capture_with_body(monkeypatch, payload)

    result = send_sms(db, phone="+99361234567", message="Привет")
    assert result.ok is False
    assert "шлюз отказал" in result.error


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},
        {"success": "true"},
        {"status": "ok", "id": 42},
        {"error": 0},
        {"message_id": "abc"},
        None,          # тело не JSON
        "OK",          # шлюз ответил простым текстом
    ],
)
def test_successful_gateway_answers_are_accepted(
    db, monkeypatch: pytest.MonkeyPatch, payload
) -> None:
    """Непонятный ответ считаем успехом: иначе рабочая отправка ложно падала бы."""
    from web.services.notifications import send_sms

    _setup_sms(db)
    _capture_with_body(monkeypatch, payload)

    assert send_sms(db, phone="+99361234567", message="Привет").ok is True
