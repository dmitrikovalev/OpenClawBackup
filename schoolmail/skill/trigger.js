// Condition gate for the OpenClaw automation "Школьная почта" (runs every 5 minutes).
// Wakes the agent - and spends model tokens - only when http://schoolmail:8000 has new letters,
// 07:00-22:00 Dubai time. Quiet checks make no model call and leave no run history.
// Must return { fire, message?, state? }; trigger.state is the state returned last time.
// The scheduler does not save the state when the agent run fails, so retry limits must not
// depend on it: the service itself reports letters the agent has not received this hour
// (`unserved`), and a service failure may wake the agent only on the :00 check.

const prev = trigger.state ?? {};
const dubai = new Date(Date.now() + 4 * 3600 * 1000);          // UTC+4, no DST
const hhmm = dubai.getUTCHours() * 100 + dubai.getUTCMinutes();
if (hhmm < 700 || hhmm > 2200) return { fire: false, state: prev };   // quiet hours

const res = await exec({ command: "curl -s --max-time 20 http://schoolmail:8000/check" });
const body = String(res?.aggregated ?? "").trim();
let data = null;
try { data = JSON.parse(body); } catch {}

if (!data || !Number.isInteger(data.new) || !Number.isInteger(data.unserved)) {
  // service or IMAP is down: tell the owner once per outage, at most once an hour
  if (prev.failing || hhmm % 100 >= 5) return { fire: false, state: prev };
  return { fire: true, state: { failing: true },
           message: "Проверка сервиса schoolmail не удалась: " + (body || "нет ответа").slice(0, 200) };
}
// letters the agent already received this hour and did not ack (e.g. Telegram failed) wait
if (data.unserved === 0) return { fire: false, state: { at: hhmm, waiting: data.new } };
return { fire: true, state: { at: hhmm }, message: `Новых писем из школы: ${data.new}.` };
