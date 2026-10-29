# RTA Intercity Bus: гостевое бронирование (разведка, 2026-10-26)

Источники: прогоны `recon3.py`, `recon4.py` (out/04_*), конфиг `https://bus.rta.ae/config.js`
и JS-бандл `assets/index-Bl2Il9y4.js` (статический анализ). Формы контактов, OTP и
оплата **не отправлялись**: всё, что после «Guest Checkout», восстановлено по коду бандла
и помечено *(из бандла)*.

## Конфиг сайта (config.js)
- API: `https://bus-api.rta.ae/api/v1/b2c/`, SignalR: `https://bus-api.rta.ae/hubs/booking`, `providerId=1`
- `OTP_LENGTH=4`, `OTP_RESEND_SECONDS=60`, `MAX_PASSENGERS=10`, `PAYMENT_RETRY_COOLDOWN_SECONDS=60`
- `ENABLE_ROUND_TRIP=false`, `ENABLE_EXTRA_LUGGAGE=false` (шаг Extras не показывается)
- `API_TIMEOUT=10000` мс

## Шаги на сайте
Всё на одной странице `https://bus.rta.ae/` (аккордеон: Step 1/2/3), URL не меняется.

### Шаг 1. Маршрут и рейс
«Start your booking as a guest» → станции (ant-select) → «Search Trips».
Первый рейс в списке выбран автоматически. Счётчик Tickets (+/−), кнопки
«Continue to Seat Selection» и «Auto Select Seats». Шаг 1 работает без хаба, по REST:
- `GET booking/locations/by-name?IsBoarding=true&NameFilter=...`
- `GET booking/route-patterns/by-locations?DepartureLocationId=4&DestinationLocationId=6`
- `GET booking/route-patterns/{id}/scheduled-trips?DateUtc&TimeUtc&RequiredStandardCount=N&BoardingPatternStopId&AlightingPatternStopId&...`

### Шаг 2. Места (SignalR)
«Auto Select Seats» → модальный диалог «Auto Select Seats / Selected seats for each
passenger / [53] / **Continue to Payment** / **Cancel**».

Транспорт SignalR: **Server-Sent Events** (WebSocket не предлагается; есть LongPolling).
Клиент → сервер шлёт `POST /hubs/booking?id=<connectionToken>`, сервер → клиент идёт через SSE `GET` на тот же URL.
Кадры заканчиваются символом `\x1e`.

Последовательность (записана):
```
POST /hubs/booking/negotiate?negotiateVersion=1   -> connectionId, connectionToken
POST {"protocol":"json","version":1}  ;  {"type":6} (ping)
<-   {"type":1,"target":"ReservationSessionEstablished","arguments":["<sessionId uuid>"]}
POST {"target":"JoinScheduledTripAsCustomer","arguments":[15152],"invocationId":"0","type":1}
<-   {"type":3,"invocationId":"0","result":null}
POST {"target":"AutoSelectReservation","arguments":[15152,{"boardingStopId":13,"alightingStopId":15,
      "passengers":[{"passengerTypeId":1,"count":1}]}],"invocationId":"1","type":1}
<-   {"type":1,"target":"SeatReservationUpdated","arguments":[{"data":{"seatIds":[833307],"isReserved":true,
      "fromPatternStopSequenceNumber":1,"toPatternStopSequenceNumber":3}}]}
<-   {"type":3,"invocationId":"1","result":{"isSuccess":true,"data":{"holdId":48140,
      "reservedSeatIds":[833307],"expiresAt":"2026-10-26T14:06:49.33","sessionId":"<uuid>"},"error":null}}
GET  booking/scheduled-trips/{tripId}/seats?BoardingPatternStopId=13&AlightingPatternStopId=15
```
- `passengerTypeId=1` = Adult (`GET passengertypes`). Для N пассажиров `count: N`.
- Другие события хаба *(из бандла)*: `AvailableSeatsReceived`, `SeatStatusChanged`.
- **Отпустить места:** `ReleaseReservation(tripId, {sessionId, scheduledTripSeatId})` по одному вызову на место *(из бандла)*.
  В UI: «Cancel» в диалоге шага 2 или «Back» на шаге 3 → диалог «You will lose all progress… Going back will release your seats» → «Continue» («Stay» = остаться).
- sessionStorage после hold: `bookingReservationHoldId`, `bookingReservationSessionId`,
  `bookingReservationExpiresAt`, `seatSession_{tripId}`, `selectedSeats_{tripId}`.
- REST-запросы после hold передают сессию в заголовке `X-Reservation-Session: <sessionId>` *(из бандла)*.

### Удержание мест
**15 минут** от `AutoSelectReservation` (наблюдалось: 13:50:26Z → expiresAt 14:05:26Z).
Ошибки хаба *(из бандла)*: `holdExpired`, `holdLimit`, `noEnoughSeats`, `partyCeiling`, `sessionMissing`.

### Шаг 3. Payment: Checkout Method
«Continue to Payment» (в диалоге шага 2) открывает Step 3 «Checkout Method»:
- `button-login-checkout`: «Login / Register»;
- `button-guest-checkout`: «Guest Checkout — Book without creating an account»;
- «Back» и «Verify & Continue» (неактивна, пока метод не выбран).

Клик по «Guest Checkout» запросов не шлёт, а только включает «Verify & Continue». Дальше разведка **не шла**.

### Шаг 3a. Форма гостя *(из бандла)*
Ant Form, поля:
| name | тип | правила | data-testid |
|---|---|---|---|
| `passengerTitle` | select Mr / Mrs / Ms / Dr | required | `select-passenger-title` |
| `passengerFirstName` | text | required, min 2 | `input-first-name` |
| `passengerLastName` | text | required, min 2 | |
| `passengerEmail` | email | required, email | `input-email` |
| `passengerPhoneCode` | выбор страны | required, по умолчанию `+971` | |
| `passengerPhone` | text | required, проверка по коду страны | `input-phone` |

Кнопки: `button-back`, «Verify & Continue».

### Шаг 3b. OTP *(из бандла)*
Отправка формы вызывает:
```
POST booking/scheduled-trips/reservations/{holdId}/confirmationcode/request
{"holdId","email","phoneCode":"971","phoneNumber","title","firstName","lastName","fromEmail":false}
-> {data:{source, email, phoneCode, phoneNumber}}   (куда фактически ушёл код)
```
- **По умолчанию код уходит по SMS** (`fromEmail:false`). В модальном окне OTP
  («Verify Your Identity», 4 поля по 1 цифре) через 60 с доступны «Resend code» и
  «Send to Email» / «Send to SMS». Это тот же запрос с `fromEmail:true/false`.
```
POST booking/scheduled-trips/reservations/{holdId}/confirmationcode/verify
{"holdId","code":"1234","email","phoneCode","phoneNumber"}
-> {data:{token, tokenExpirationInSeconds}}
```
- `token` — гостевой Bearer, хранится в sessionStorage `guestCheckoutToken`; срок жизни по
  умолчанию 900 с. Ошибка `otpRateLimited` возможна.

### Шаг 3c. Сводка и подтверждение брони *(из бандла)*
Экран «summary»: контакты, места, Fees, напоминания («Remind me before» 15 мин…24 ч по
email). Кнопка «Confirm Reservation»:
```
POST booking/scheduled-trips/reservations/confirm
Authorization: Bearer <guest token>;  X-Reservation-Session: <sessionId>
{"holdId":48140,
 "seatReservationProducts":[{"scheduledTripId":15152,"boardingPatternStopId":13,"alightingPatternStopId":15,
   "seatReservationItems":[{"passengerTypeId":1,"scheduledTripSeatId":833307}]}],
 "remindBeforeInMinutes":null,"remindByEmail":false,"remindBySMS":false}
-> {data:{invoiceId, availablePaymentMethod:[{id,name}], total}}
```
Если `total == 0` и методов нет, вызывается `POST booking/invoice/{id}/confirmfree`.

### Шаг 3d. Оплата *(из бандла)*
«Select Payment Method» (карточки `availablePaymentMethod[].name`) → «Pay Now»:
```
POST booking/invoice/{invoiceId}/request-payment   {"paymentMethodId": <id>}
-> {data:{redirectUri, transactionId}}
```
- Браузер уходит на `window.location.assign(redirectUri)`, внешний платёжный шлюз.
  **Домен шлюза и список методов не наблюдались**: они видны только после реального
  `reservations/confirm`.
- Возврат: `https://bus.rta.ae/paymentconfirm`. Перед редиректом в sessionStorage
  `pendingPaymentConfirm` = {invoiceId, paymentMethodId, transactionId}, затем
  `POST booking/invoice/{id}/confirm` и экран билета
  (`GET booking/invoice/{id}/tickets/{ticketId}/download`: PDF, есть QR и Wallet).
- Повторная оплата не раньше чем через 60 с.

## Открытые вопросы
1. Домен платёжной страницы и методы оплаты: нужен один осознанный прогон до `request-payment` (это уже реальная бронь/инвойс).
2. Срок жизни инвойса после `reservations/confirm` и продлевает ли он hold.
3. Можно ли вызывать REST `confirmationcode/*` без живого SSE-соединения хаба (вероятно, hold привязан к сессии хаба).

## Журнал прогонов recon4 (2026-10-26)
| # | рейс | место | holdId | отпущено? |
|---|---|---|---|---|
| 1 | 15151 18:10 | 53 | ? | нет: истёк ~14:03Z |
| 2 | 15152 18:30 | 53 | 48139 | нет: истёк 14:05:26Z |
| 3 | 15152 18:30 | 49 | 48140 | нет: истёк 14:06:49Z |

Отпускание через UI не сработало из-за таймингов диалогов (исправлено в recon4.py, без перепроверки).

---

# Дизайн book.py (предложение, не реализовано)

**Цель:** гостевое бронирование до ссылки на оплату. На выходе JSON `{payment_url, invoice_id, total, expires_at}`.

### Подход: чистый API (без браузера)
Все шаги — HTTP + SignalR. Браузер не нужен, это быстрее и стабильнее на ARM.
- SignalR: библиотека `pysignalr` (WebSocket не предлагается) или свой мини-клиент:
  `negotiate` → SSE-поток (`httpx.stream("GET", ...)` в фоновой задаче) + POST-вызовы.
  Нужен только JSON-протокол с разделителем `\x1e`, примерно 80 строк.
- План B: Playwright по тем же шагам (селекторы и testid выше), если API начнёт
  проверять `app_fingerprint` и подобное.

### Входы
- CLI/JSON: `--from`, `--to`, `--date`, `--time` (или `--trip-id`), `--pax`.
- ENV (`RTA_*` в отдельном env-файле, только для чтения):
  `RTA_TITLE`, `RTA_FIRST_NAME`, `RTA_LAST_NAME`, `RTA_EMAIL`, `RTA_PHONE_CODE=971`,
  `RTA_PHONE`, `RTA_OTP_CHANNEL=sms|email`.

### Поток
1. `search_trips.search()` → выбрать рейс (ближайший или указанный `trip_id`).
2. Hub: connect → дождаться `ReservationSessionEstablished` → `JoinScheduledTripAsCustomer`
   → `AutoSelectReservation` → `holdId`, `reservedSeatIds`, `expiresAt`.
3. `confirmationcode/request` (`fromEmail` по `RTA_OTP_CHANNEL`).
4. **Ожидание OTP** (состояние `awaiting_otp`, таймаут = min(10 мин, expiresAt − 1 мин)):
   - файл: `state/<run_id>/otp`, опрос раз в 2 с (локальный файл, не сайт);
   - или HTTP: маленький `aiohttp`-сервер `POST /otp {run_id, code}` на 127.0.0.1;
   - OpenClaw: агент получает от book.py событие «нужен код» (stdout JSON-строка
     `{"event":"otp_required","run_id":...,"sent_to":"+971•••12"}`), спрашивает
     пользователя в Telegram и пишет код в файл / шлёт POST.
5. `confirmationcode/verify` → гостевой токен (900 с).
6. `reservations/confirm` → `invoiceId`, `availablePaymentMethod`, `total`.
7. `request-payment` (метод по умолчанию: карта, выбирается по имени) → `redirectUri`.
8. Вывод: `{"event":"payment_ready","payment_url":redirectUri,"invoice_id":...,"total":...,
   "seat_numbers":[...],"hold_expires_at":...}`. Пользователь сам платит по ссылке.

### Надёжность
- `try/finally`: при любой ошибке до шага 6 вызвать `ReleaseReservation` для каждого
  места, закрыть хаб.
- Держать SSE-соединение живым до выдачи ссылки (ping `{"type":6}` каждые 15 с).
- Без повторов в цикле: максимум 1 повтор `request`/`verify`, учитывать `otpRateLimited`
  и cooldown 60 с.
- `--dry-run`: шаги 1–2 и немедленный release (проверка без OTP).
- Логи без персональных данных (маскировать телефон/email).
- Отдельный `check_payment.py --invoice` (позже): статус по `/paymentconfirm`-логике.

---

# Прогон book.py (2026-10-26, разрешён пользователем)

Рейс: abu-dhabi → dubai, 2026-10-28 08:30, trip 15644, 1 пассажир, метод dubaiPay.

| # | режим | holdId | место | итог | отпущено? |
|---|---|---|---|---|---|
| 1 | `--stop-after seats` | 48176 | 53 | hold + release | да: «Cancel» в диалоге → сайт сам шлёт `ReleaseReservation`; по API `isReserved=false` |
| 2 | полный | 48180 | 53 | остановлен на поле Title (до ввода данных, без SMS) | да, проверено по API |
| 3 | полный | 48185 | 53 | SMS → OTP → Confirm Reservation (инвойс 34103, 25 AED) → Pay Now → **шлюз открыт**; дальше ошибка скрипта | нет (см. ниже) |
| 4 | полный | 48211 | 49 | один `confirmationcode/request` (SMS), оба `verify` → 500 `RTA.VERIFY.OTP.SERVICE.ERROR` | да, проверено по API |
| 5 | полный, рейс 15653 (28.10 11:30) | 48219 | 53 | SMS → OTP ok → Confirm (инвойс 34123) → Pay Now → шлюз → чистый контекст ok | нет (бронь ждёт оплаты, по условию) |

### Уточнения по UI (из прогонов и словаря переводов бандла)
- Шаг 1: «Select Date & Time» открывает модальное окно: календарь (дни — кнопки, `aria-label=Next` листает месяц),
  клик по дню сразу открывает время. Часы и минуты — SVG `role=slider` (`Hour dial` 0–23, `Minute dial`),
  управляются стрелками. Подтверждает кнопка-футер с текстом вида «28 Oct · 08:25».
  «Passengers»: модальное окно, `aria-label="Increase Adult"`, «Confirm».
- **Реальные подписи берутся из словаря переводов, а не из fallback-строк в коде:** кнопка гостевой формы —
  «Verify and Pay», окно OTP — «Verify OTP» (`aria-labelledby=otp-modal-title`), поля кода — «Character n»,
  кнопка OTP — «Verify & Continue».
- Гостевая форма открывается сразу после «Continue to Payment» → «Guest Checkout».
  Title — выпадающий список Mr/Mrs/Ms/Dr, пункты списка без класса `.ant-select-item-option`.
- Сводка: «Confirm Reservation» (`button-confirm-reservation`) неактивна, пока не нажато
  «I agree to The Terms and Conditions.» (`button-accept-terms`, в блоке «View Details ▼»).
- Если `redirectUri` выглядит как FAKE (функция `y4`), сайт не делает редирект, а сразу вызывает
  `invoice/{id}/confirm`. book.py такой ответ блокирует.
- Методы оплаты в словаре: «Pay with dubaiPay», «Pay with Apple Pay»; на шлюзе ещё не наблюдались.

## Результат прогона 3 (2026-10-26 14:37–14:40 UTC)

Хронология (UTC): 14:38:32 hold 48185 → 14:38:37 форма заполнена (все 6 полей сверены) →
14:38:38 `confirmationcode/request` → `source: "Phone"` (SMS) → 14:39:56 код из `out/otp.txt` →
14:39:58 `verify` 200 → 14:40:01 принятие условий → 14:40:02 `reservations/confirm` 200:
`invoiceId 34103, total 25.0`, один метод оплаты `id 5` («Dubai Pay») → 14:40:0x «Pay Now» →
`POST invoice/34103/request-payment {"paymentMethodId":5}` 200 → редирект на шлюз.

### Платёжная страница
- **Домен:** `epayment.dubai.ae` (dubaipay / Digital Dubai, ePayHub).
- **URL без токена:** `https://epayment.dubai.ae/ePayHub/Authentication/SPServlet?token=<…>`.
  Это первая страница после `redirectUri`, единственный параметр — `token`.
- Шапка: «Roads and Transport Authority3 · #DU-<invoiceId><цифры>» (SP Transaction No), Service
  «Bus Intercity Ticketing», Amount 25.00 AED.
- **Способы оплаты на шлюзе:** Credit Card, Net Banking, Google Pay. Есть и четвёртая строка
  с переключателем без подписи (в headless Chromium/Linux; вероятно Apple Pay, который показывается только в Safari).
  На bus.rta.ae перед шлюзом один метод — «Dubai Pay» (id 5).
- Contact Info заполнен данными брони (email, mobile), есть «Add Another Contact».
  Кнопки «Cancel» / «Pay», подпись «By tapping on "Pay" you are agreeing with our Terms & Conditions».
  Справки: 600 560 000. На шлюзе ничего не нажималось и не вводилось.
- Скриншот: `out/06_08_error.png` (снят при обработке ошибки, уже на шлюзе; контакты замазаны вручную после снимка).

### Не проверено (в прогоне 3; см. прогон 5 ниже)
- **Открывается ли `redirectUri` в чистом контексте браузера: в прогоне 3 НЕ проверено.** Из-за ошибки
  скрипта `redirectUri` не был сохранён: страница ушла на шлюз раньше, чем обработчик прочитал
  тело ответа. `out/private/redirect_uri.txt` не создан. Повторный `request-payment` создал бы новую
  транзакцию, его не делали. (Исправлено в book.py: URI читается в `page.route`.)
- Письмо на почту после брони: ждём ответа пользователя.

### Прочие наблюдения
- Гостевой токен после `verify`: `tokenExpirationInSeconds: 86399` (24 ч, а не 900 с, как в fallback кода).
  Ответ `verify` содержит профиль: `id`, `accountType: "RTA"`, `nationality`, `externalId`.
  Похоже, гостевая проверка заводит/привязывает учётную запись RTA.
- После `reservations/confirm` место остаётся `isReserved=true`. Отпустить его со страницы
  шлюза нельзя: соединение хаба закрыто, `ReleaseReservation` получает `Failed to fetch`.
  Отмена неоплаченного инвойса через API не найдена.
- «Confirm Reservation» требует нажать «I agree to The Terms and Conditions.» (текст условий в `out/06_terms.txt`).

## Прогон 4 (2026-10-26 14:54–15:05 UTC): отказ OTP
- 14:55:17 `confirmationcode/request` 200 (`source: Phone`). Со страницы ушёл ровно один запрос кода.
- 14:55:52 и 15:04:57 `confirmationcode/verify` → **500** `RTA.VERIFY.OTP.SERVICE.ERROR`:
  «Reference ID in the request is not the latest reference ID generated for the user.»
- Это не ошибка «неверный код»: сервер считает, что для пользователя позже был сгенерирован
  другой reference ID. Возможные причины: вторая SMS/запрос кода не со страницы скрипта,
  либо влияние неоплаченной брони 34103 (тот же пользователь). Не выяснено.
- Место 49 отпущено (UI «Back» → `ReleaseReservation`), по API `isReserved=false`.
- Неоплаченная бронь 34103 (место 53) после истечения hold (14:53:29) **осталась** `isReserved=true`
  (проверено в 14:54 и 15:05), то есть живёт дольше 15-минутного удержания.

## Прогон 5 (2026-10-26 15:08–15:10 UTC): дошли до шлюза, итог по вопросам

Рейс abu-dhabi → dubai 2026-10-28 11:30 (trip 15653), 1 пассажир, место 53, hold 48219 (до 15:24:14Z).
15:09:25 SMS → 15:10:03 OTP ok (первый же код) → 15:10:08 `reservations/confirm` → **инвойс 34123**, 25 AED,
метод `5: Dubai Pay (DUBAIPAYBT)` → 15:10:12 `request-payment` → `redirectUri` → шлюз 15:10:17.

| вопрос | ответ |
|---|---|
| Домен платёжной страницы | `epayment.dubai.ae` (dubaipay / Digital Dubai, ePayHub), заголовок «Welcome to Dubai Pay» |
| URL без токенов | `https://epayment.dubai.ae/ePayHub/Authentication/SPServlet?token=<…>`. `redirectUri` ведёт сразу на него, редиректов нет |
| Способы оплаты на шлюзе | **Credit Card, Net Banking, Google Pay** (Google Pay во iframe `pay.google.com`). На bus.rta.ae перед шлюзом один метод: «Dubai Pay» (`DUBAIPAYBT`, id 5). Apple Pay в headless Chromium/Linux не показывается; в словаре сайта есть «Pay with Apple Pay» |
| Поля шлюза | радио `PYMTMETHODCODE` ×3, кнопки `btnPay` («Pay»), `btnCancel` («Cancel»), Contact Info (email и mobile из брони, «Add Another Contact»), SP Transaction No `DU-<invoiceId><…>` |
| redirectUri в другом браузере | **Открывается.** Новый чистый контекст Chromium (без cookies/storage/referrer): HTTP 200, та же страница, те же методы и сумма. Ссылка не привязана к сессии браузера; токен пережил два открытия. Пользователь открыл её и на своём телефоне (~15:13Z) |
| Письмо после брони | **Ничего.** После SMS с OTP не пришло ни письма, ни SMS (со слов пользователя; за все прогоны 3 SMS, только OTP). Ссылку на оплату RTA не присылает: оплата только онлайн сразу после брони |
| Срок жизни неоплаченной брони | **~44 мин после подтверждения.** Бронь 34103: confirm 14:40:02, hold до 14:53:29, место освободилось в **15:24:07Z** (опрос раз в 30 с, ±30 с), т.е. ~31 мин после конца hold. Шлюз таймер не показывает. См. раздел «Срок жизни неоплаченной брони» |

Скриншоты: `out/06_08_payment_page.png`, `out/06_09_payment_page_clean_context.png` (контакты и переключатели замазаны).
Полный `redirectUri` в `out/private/redirect_uri.txt` (600, владелец ubuntu); в логах и NOTES его нет.
На шлюзе ничего не нажималось и не вводилось.

### OTP: ошибка «not the latest reference ID» (прогон 4) не повторилась
В прогоне 5 первый же код прошёл. Отличие от прогона 4: другой рейс, и прошло ~15 мин после прогона 4.
Причина ошибки не выяснена. book.py при такой ошибке теперь делает «Resend code» (новая SMS) и ждёт новый код.

## Срок жизни неоплаченной брони
| инвойс | рейс | hold до (UTC) | confirm (UTC) | место освободилось (UTC) | после confirm | после hold |
|---|---|---|---|---|---|---|
| 34103 | 15644 28.10 08:30, место 53 | 14:53:29 | 14:40:02 | **15:24:07** (±30 с) | ~44 мин | ~31 мин |
| 34123 | 15653 28.10 11:30, место 53 | 15:24:14 | 15:10:08 | **15:54:50** (±30 с) | ~45 мин | ~31 мин |

Вывод (2 брони, одинаково): неоплаченная подтверждённая бронь держит место **~45 мин с подтверждения**
(= hold 15 мин + ~30 мин). Наблюдение остановлено. Гарантированный срок для пользователя всё же
`hold_expires_at` (сервис отдаёт именно его); оплата после него на шлюзе не проверялась.

---

# Сервис для OpenClaw (server.py, 2026-10-26)

Файлы: `server.py` (FastAPI), `book.py` (браузерные шаги, класс `Run` + `Hooks`),
`search_trips.py`, `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `skill/rta-bus/SKILL.md`.

### API (`http://rta:8000`, только сеть `ai`, наружу порт не опубликован)
| метод | путь | ответ |
|---|---|---|
| GET | `/health` | `{status, buyer_env: "ok"|"invalid: …", active_job, time_dubai}` |
| GET | `/search?from&to&date&time&until&pax` | как `search_trips.search()`: `trips[]` с `trip_id, departure, arrival, seats_standard, total` |
| POST | `/book` `{from,to,date,trip_id|departure_time,pax}` | 202 `{job_id,status}`; 409 если идёт другая бронь; 404 нет рейса; 503 нет данных покупателя |
| GET | `/book/{id}` | `status, message, seats, hold_expires_at, invoice_id, total`; `otp_deadline` при `otp_required`; `payment_url` только при `payment_ready`; `reason, seats_released` при `failed/cancelled` |
| POST | `/book/{id}/otp` `{code}` | 4 цифры, только в `otp_required` (иначе 409) |
| POST | `/book/{id}/cancel` | до подтверждения брони: отмена и отпускание мест; после — 409 |

Статусы: `holding → otp_required → processing → payment_ready | failed | cancelled`.
При ошибке «not the latest reference ID» сервис сам нажимает «Resend code» и снова ставит `otp_required`.

### Устройство
- Одна бронь за раз (глобальная блокировка), фоновый поток, sync Playwright. Таймауты как в CLI:
  код ждётся min(10 мин, hold − 90 с). Отмена проверяется между шагами и каждые 2 с при ожидании кода.
- Любая ошибка или отмена до `payment_ready` отпускает места (UI → `ReleaseReservation`).
- `payment_url` хранится только в памяти задачи и отдаётся только в ответе API. В лог пишется
  домен и путь без токена; файл `out/private/…` и проверка в чистом контексте в режиме сервиса выключены.
- Данные покупателя из `env_file: .env.rta`, наружу не отдаются. stdout сервиса проходит через `Redactor`.
- Артефакты каждой брони (замаскированные скриншоты, отредактированный API-лог):
  `out/service/jobs/<job_id>/` (том `./out/service:/work/out`, файлы от root).
- CLI `book.py` работает как раньше (`FileHooks` читает `out/otp.txt`).

### Проверки (2026-10-26)
- Офлайн-тест логики сервера с подставным `Run` (без браузера и сайта): 16/16 — 409 на вторую бронь,
  OTP 422/409, `payment_url` только в API и не в логах, код не в логах, отмена отпускает места,
  отмена после `payment_ready` → 409.
- `docker compose up -d`: контейнер `rta-rta-1`, алиас `rta` в сети `ai`, `PortBindings: {}`.
- Из `docker run --rm --network ai curlimages/curl:8.11.1`: `/health` 200 (`buyer_env: ok`),
  `/search` 200, неверный запрос 422. С хоста `127.0.0.1:8000` недоступен.
- Бронирование через сервис не запускалось.
