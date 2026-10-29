# Навык и задача по расписанию для OpenClaw: «Школьная почта»

Проверено по справке и документации установленной версии OpenClaw 2026.10.7
(контейнер `openclaw-openclaw-gateway-1`).

## Как это работает
Одна задача `*/5 7-22 * * *` (Asia/Dubai) с условием `trigger.js`. Каждые 5 минут условие
без участия модели вызывает `curl http://schoolmail:8000/check` и будит агента только когда:
- есть новые письма и сейчас 07:00–22:00 по Дубаю;
- те же письма так и не подтверждены (например, Telegram не принял сообщение) — не чаще
  раза в час;
- сервис или IMAP недоступны — один раз на каждый сбой, чтобы агент предупредил владельца.

Проверка без писем не вызывает модель и не создаёт записей в истории запусков: токены
тратятся только на реальные письма. Тихие часы 22:00–07:00 — условие даже не обращается
к сервису. Ручной `openclaw cron run <id>` обходит условие и всегда будит агента.

## 1. Навык
```bash
docker cp ~/schoolmail/skill/school-mail openclaw-openclaw-gateway-1:/tmp/school-mail
docker exec openclaw-openclaw-gateway-1 openclaw skills install /tmp/school-mail
docker exec openclaw-openclaw-gateway-1 openclaw skills info school-mail
```
Обновить навык после правок: та же команда `skills install` с `--force`.

## 2. Задача
```bash
bash ~/schoolmail/add_cron.sh
```
Chat id получателя задан в начале `add_cron.sh` (`CHAT_ID`). Скрипт удаляет прежние задачи
«Школьная почта», «Школьная почта 22:00» и «Школьная почта · каждые 5 мин» и создаёт одну
новую, так что повторный запуск не оставляет дублей.

- `--session isolated` — каждый запуск с чистым контекстом.
- `--no-deliver` — итоговый ответ агента никуда не уходит; сводки отправляются инструментом
  `message` по шагам навыка.
- `--tools exec,read,message` — агенту и условию доступны только `exec` (curl к сервису),
  `read` (прочитать SKILL.md) и `message`.

## 3. Проверка
```bash
docker exec openclaw-openclaw-gateway-1 openclaw cron show <id>   # состояние условия (state)
docker exec openclaw-openclaw-gateway-1 openclaw cron runs <id>   # запуски агента
docker run --rm --network ai curlimages/curl:8.11.1 -s http://schoolmail:8000/stats
```
Удалить: `docker exec openclaw-openclaw-gateway-1 openclaw cron rm <id>`.
