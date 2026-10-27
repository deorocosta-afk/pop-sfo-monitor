import assert from "node:assert";
import worker from "../cloudflare_worker.js";

async function runTests() {
  console.log("Running Cloudflare Worker Node.js test suite...");

  const baseEnv = {
    TELEGRAM_BOT_TOKEN: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    GITHUB_TOKEN: "ghp_mock_token_for_tests",
    TELEGRAM_WEBHOOK_SECRET: "my_ultra_secret_webhook_token_999",
    ALLOWED_CHAT_IDS: "12345678,87654321",
    GITHUB_REPO: "owner/pop-sfo-monitor",
    WORKER_URL: "https://pop-sfo.example.workers.dev",
  };

  // Test 1: missing essential secrets -> 500
  {
    const req = new Request("https://example.com/sfo", { method: "GET" });
    const res = await worker.fetch(req, {}, {});
    assert.strictEqual(res.status, 500, "Should return 500 on missing bot/github tokens");
    const text = await res.text();
    assert(text.includes("Worker misconfigured"), "Error must mention misconfiguration");
    console.log("  ✓ Test 1 Passed: missing tokens reject with 500");
  }

  // Test 2: GET portal -> 200 with POP-SFO branding
  {
    const req = new Request("https://example.com/", { method: "GET" });
    const res = await worker.fetch(req, baseEnv, {});
    assert.strictEqual(res.status, 200, "Portal page should return 200");
    const html = await res.text();
    assert(html.includes("POP ⇄ SFO"), "Portal HTML must include POP ⇄ SFO");
    console.log("  ✓ Test 2 Passed: GET / returns the POP-SFO portal");
  }

  // Test 3: POST without the secret header -> 403
  {
    const req = new Request("https://example.com/webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: { chat: { id: 12345678 }, from: { id: 12345678 }, text: "/start" } }),
    });
    const res = await worker.fetch(req, baseEnv, {});
    assert.strictEqual(res.status, 403, "Missing secret header must return 403");
    assert((await res.text()).includes("Forbidden"), "Must return Forbidden");
    console.log("  ✓ Test 3 Passed: missing secret returns 403");
  }

  // Test 4: invalid secret -> 403
  {
    const req = new Request("https://example.com/webhook", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "wrong_secret_hacker",
      },
      body: JSON.stringify({ message: { chat: { id: 12345678 }, from: { id: 12345678 }, text: "/start" } }),
    });
    const res = await worker.fetch(req, baseEnv, {});
    assert.strictEqual(res.status, 403, "Invalid secret header must return 403");
    console.log("  ✓ Test 4 Passed: invalid secret returns 403");
  }

  // Test 5: empty env secret -> 403 (fail-closed)
  {
    const envNoSecret = { ...baseEnv, TELEGRAM_WEBHOOK_SECRET: "" };
    const req = new Request("https://example.com/webhook", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "my_ultra_secret_webhook_token_999",
      },
      body: JSON.stringify({ message: { chat: { id: 12345678 }, from: { id: 12345678 }, text: "/start" } }),
    });
    const res = await worker.fetch(req, envNoSecret, {});
    assert.strictEqual(res.status, 403, "Empty env secret must return 403");
    console.log("  ✓ Test 5 Passed: empty env secret fails closed with 403");
  }

  // Test 6: deny-by-default whitelist -> 200 without dispatch (no Telegram retries)
  {
    const origFetch = globalThis.fetch;
    const calls = [];
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      return new Response("{}", { status: 200 });
    };
    try {
      const headers = {
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": "my_ultra_secret_webhook_token_999",
      };
      const unauthorized = new Request("https://example.com/webhook", {
        method: "POST",
        headers,
        body: JSON.stringify({ message: { chat: { id: 99999999 }, from: { id: 99999999 }, text: "Сканировать" } }),
      });
      const res1 = await worker.fetch(unauthorized, baseEnv, {});
      assert.strictEqual(res1.status, 200, "Unauthorized chat must be refused with 200");
      assert.strictEqual(calls.length, 0, "Unauthorized chat must not call Telegram or GitHub");

      const noWhitelist = { ...baseEnv, ALLOWED_CHAT_IDS: "", TELEGRAM_CHAT_ID: "" };
      const allowedChat = new Request("https://example.com/webhook", {
        method: "POST",
        headers,
        body: JSON.stringify({ message: { chat: { id: 12345678 }, from: { id: 12345678 }, text: "Сканировать" } }),
      });
      const res2 = await worker.fetch(allowedChat, noWhitelist, {});
      assert.strictEqual(res2.status, 200, "Empty whitelist must deny with 200");
      assert.strictEqual(calls.length, 0, "Empty whitelist must not trigger actions");
      console.log("  ✓ Test 6 Passed: whitelist is deny-by-default, refusals return 200");
    } finally {
      globalThis.fetch = origFetch;
    }
  }

  // Test 7: authorized scan request -> 200, Telegram message + GitHub dispatch
  {
    const origFetch = globalThis.fetch;
    const calls = [];
    globalThis.fetch = async (url) => {
      calls.push(String(url));
      return new Response("{}", { status: 200 });
    };
    try {
      const req = new Request("https://example.com/webhook", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Telegram-Bot-Api-Secret-Token": "my_ultra_secret_webhook_token_999",
        },
        body: JSON.stringify({
          message: { chat: { id: 12345678 }, from: { id: 12345678 }, text: "Сканировать" },
        }),
      });
      const res = await worker.fetch(req, baseEnv, {});
      assert.strictEqual(res.status, 200, "Authorized request should succeed with 200");
      assert(calls.some((u) => u.includes("api.telegram.org")), "Telegram sendMessage must be called");
      assert(calls.some((u) => u.includes("/dispatches")), "GitHub workflow dispatch must be called");
      console.log("  ✓ Test 7 Passed: authorized scan sends Telegram + GitHub dispatch");
    } finally {
      globalThis.fetch = origFetch;
    }
  }

  console.log("\nAll 7 Cloudflare Worker tests passed successfully!");
}

runTests().catch((err) => {
  console.error("Worker test failed:", err);
  process.exit(1);
});
