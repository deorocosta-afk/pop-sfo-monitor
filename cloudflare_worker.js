/**
 * Cloudflare Worker for the standalone POP <-> SFO Flight Price Monitor.
 *
 * 1. Serves the POP-SFO HTML dashboard over HTTPS (for Telegram / mobile).
 * 2. Handles the Telegram webhook and triggers GitHub Actions with strict auth:
 *    - mandatory X-Telegram-Bot-Api-Secret-Token (403 by default);
 *    - deny-by-default chat whitelist (ALLOWED_CHAT_IDS / TELEGRAM_CHAT_ID),
 *      checking both chat.id and from.id;
 *    - 200 responses for authorization refusals so Telegram does not retry.
 *
 * Required env: TELEGRAM_BOT_TOKEN, GITHUB_TOKEN, TELEGRAM_WEBHOOK_SECRET,
 *               ALLOWED_CHAT_IDS (or TELEGRAM_CHAT_ID).
 * Optional env: GITHUB_REPO, WORKER_URL.
 */

export default {
  async fetch(request, env, ctx) {
    const BOT_TOKEN = env.TELEGRAM_BOT_TOKEN;
    const GITHUB_TOKEN = env.GITHUB_TOKEN;

    if (!BOT_TOKEN || !GITHUB_TOKEN) {
      return new Response(
        "Worker misconfigured: TELEGRAM_BOT_TOKEN or GITHUB_TOKEN is missing in Cloudflare secrets.",
        { status: 500, headers: { "Content-Type": "text/plain; charset=utf-8" } }
      );
    }

    const url = new URL(request.url);
    const REPO = env.GITHUB_REPO || "OWNER/pop-sfo-monitor";
    const WORKER_URL = env.WORKER_URL || url.origin;

    // 1. HTTP GET: dashboard + portal
    if (request.method === "GET") {
      const path = url.pathname.toLowerCase();

      if (path === "/" || path === "") {
        return new Response(renderPortalHtml(WORKER_URL), {
          headers: { "Content-Type": "text/html; charset=utf-8" },
        });
      }

      if (path === "/sfo" || path.includes("sfo") || path.includes("san")) {
        try {
          const ghRes = await fetch(
            `https://api.github.com/repos/${REPO}/contents/pop_sfo_dashboard.html`,
            {
              headers: {
                "Authorization": `Bearer ${GITHUB_TOKEN}`,
                "Accept": "application/vnd.github.raw+json",
                "User-Agent": "POP-SFO-DashboardViewer",
              },
            }
          );

          if (ghRes.ok) {
            const html = await ghRes.text();
            return new Response(html, {
              headers: {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "public, max-age=60",
              },
            });
          }

          return new Response(
            `<!DOCTYPE html><html><body style="background:#090d16;color:#fff;font-family:sans-serif;padding:30px;text-align:center;">` +
            `<h2>⏳ Дашборд обновляется...</h2>` +
            `<p>GitHub Actions выполняет сканирование. Обновите страницу через 30–60 секунд.</p>` +
            `</body></html>`,
            { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 200 }
          );
        } catch (e) {
          return new Response(`Ошибка загрузки: ${e.message}`, { status: 500 });
        }
      }

      return new Response("Not Found: only / and /sfo are available.", {
        status: 404,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }

    // 2. HTTP POST: Telegram webhook
    if (request.method === "POST") {
      const hookSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (!env.TELEGRAM_WEBHOOK_SECRET || !hookSecret || hookSecret !== env.TELEGRAM_WEBHOOK_SECRET) {
        return new Response("Forbidden: Invalid or missing Webhook Secret", { status: 403 });
      }

      let update;
      try {
        update = await request.json();
      } catch {
        return new Response("OK", { status: 200 });
      }

      if (!update.message) {
        return new Response("OK", { status: 200 });
      }

      const chatId = update.message.chat && update.message.chat.id;
      const fromId = update.message.from && update.message.from.id;

      // Deny-by-default whitelist; both chat.id and from.id must be listed.
      const allowedStr = String(env.ALLOWED_CHAT_IDS || env.TELEGRAM_CHAT_ID || "");
      const allowedList = allowedStr.split(",").map((s) => s.trim()).filter(Boolean);

      if (allowedList.length === 0) {
        console.error("ALLOWED_CHAT_IDS is not configured - denying all updates");
        return new Response("OK", { status: 200 });
      }
      if (
        chatId === undefined || chatId === null ||
        fromId === undefined || fromId === null ||
        !allowedList.includes(String(chatId)) ||
        !allowedList.includes(String(fromId))
      ) {
        console.warn(`Rejected Telegram update from chat=${chatId} user=${fromId}`);
        return new Response("OK", { status: 200 });
      }

      const sendMessage = async (text) => {
        const payload = {
          chat_id: chatId,
          text,
          parse_mode: "HTML",
          disable_web_page_preview: true,
          reply_markup: {
            keyboard: [
              [{ text: "🔍 Сканировать сейчас" }],
              [{ text: "🇺🇸 Дашборд Сан-Франциско" }],
            ],
            resize_keyboard: true,
          },
        };
        const res = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        return res.ok;
      };

      const text = String(update.message.text || "").trim();

      if (text.includes("Сканировать") || text === "/scan") {
        await sendMessage(
          "⏳ <b>Запускаю поиск POP ⇄ SFO на серверах GitHub...</b>\n\n" +
          "Строго 1 пересадка • AA / JetBlue / United / Copa\n" +
          "Окно: 20 мая – 5 июля, 14–30 ночей."
        );

        try {
          const dispatch = await fetch(
            `https://api.github.com/repos/${REPO}/actions/workflows/monitor.yml/dispatches`,
            {
              method: "POST",
              headers: {
                "Authorization": `Bearer ${GITHUB_TOKEN}`,
                "Accept": "application/vnd.github+json",
                "User-Agent": "POP-SFO-Worker",
              },
              body: JSON.stringify({ ref: "main", inputs: { force: "true" } }),
            }
          );
          if (!dispatch.ok) {
            await sendMessage(
              `⚠️ Не удалось запустить GitHub Actions (HTTP ${dispatch.status}). ` +
              "Проверьте GITHUB_TOKEN, GITHUB_REPO и права workflow."
            );
          }
        } catch (e) {
          await sendMessage(`⚠️ Ошибка запуска GitHub Actions: ${e.message}`);
        }
        return new Response("OK", { status: 200 });
      }

      if (text.includes("Сан-Франциско") || text === "/sfo") {
        await sendMessage(
          "🇺🇸 <b>POP ⇄ SFO — интерактивный дашборд</b>\n\n" +
          "<i>Строго 1 пересадка • 20 мая – 5 июля • 14–30 ночей</i>\n\n" +
          `👉 <a href="${WORKER_URL}/sfo"><b>ОТКРЫТЬ ДАШБОРД ↗</b></a>`
        );
        return new Response("OK", { status: 200 });
      }

      if (text === "/start" || text === "меню") {
        await sendMessage(
          "👋 <b>POP ⇄ SFO Flight Monitor активен!</b>\n\n" +
          `🌐 <a href="${WORKER_URL}/sfo">Онлайн-дашборд POP ⇄ SFO</a>\n\n` +
          "Нажмите <b>🔍 Сканировать сейчас</b>, чтобы запустить поиск."
        );
        return new Response("OK", { status: 200 });
      }

      await sendMessage(
        "Используйте кнопки меню внизу.\n" +
        `👉 <a href="${WORKER_URL}/sfo"><b>Открыть дашборд POP ⇄ SFO ↗</b></a>`
      );
      return new Response("OK", { status: 200 });
    }

    return new Response("Method Not Allowed", { status: 405 });
  },
};

function renderPortalHtml(workerUrl) {
  return `<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>POP ⇄ SFO | Мониторинг цен</title>
  <style>
    body { background:#090d16; color:#f8fafc; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
           margin:0; padding:24px; display:flex; justify-content:center; align-items:center; min-height:90vh; }
    .card { background:#131c2e; border:1px solid #1e293b; border-radius:16px; padding:32px;
            max-width:540px; width:100%; text-align:center; box-shadow:0 10px 25px rgba(0,0,0,.5); }
    h1 { font-size:24px; margin-bottom:8px; color:#fff; }
    p { color:#94a3b8; font-size:14px; margin-bottom:24px; }
    .btn { display:flex; align-items:center; justify-content:space-between; background:#1e293b;
           color:#fff; text-decoration:none; padding:16px 20px; border-radius:12px; margin-bottom:12px;
           border:1px solid #334155; font-weight:600; font-size:15px; transition:all .2s; }
    .btn:hover { background:#2563eb; border-color:#3b82f6; transform:translateY(-2px); }
    .badge { font-size:12px; padding:4px 10px; border-radius:9999px; background:rgba(16,185,129,.2);
             color:#34d399; border:1px solid #10b981; }
  </style>
</head>
<body>
  <div class="card">
    <h1>✈️ POP ⇄ SFO Flight Monitor</h1>
    <p>Пуэрто-Плата ⇄ Сан-Франциско • строго 1 пересадка • AA / B6 / UA / CM</p>
    <a href="${workerUrl}/sfo" class="btn">
      <span>🇺🇸 Сан-Франциско (POP ⇄ SFO)</span>
      <span class="badge">1 пересадка</span>
    </a>
  </div>
</body>
</html>`;
}
