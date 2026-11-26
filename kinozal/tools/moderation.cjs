// Checks chat moderation (custom.js «moderation» and «reconnect») on the Russian page.
// Covers the "Delete" and "Ban" row buttons, the result plate, the hidden "…" menu and the "Bans" window.
// A "Twitch" row has no ban button. The "Reconnect" plate shows when the gate reports voice: false.
// The gate is stubbed with page.route. Needs a local oc-test with a stream and the styling uploaded.
// Usage: node tools/moderation.cjs [firefox]. Screenshots go to tools/shots/.
const pw = require(process.env.PLAYWRIGHT_PATH || "playwright");

const OC = "http://127.0.0.1:18080",
  auth = "Basic " + Buffer.from("admin:testpass").toString("base64");

const j = async (m, path, body, h = {}) => {
  const r = await fetch(OC + path, {
    method: m,
    headers: { "Content-Type": "application/json", ...h },
    body: body ? JSON.stringify(body) : undefined
  });
  const t = await r.text();
  try {
    return JSON.parse(t);
  } catch {
    return t;
  }
};

const engine = process.argv[2] === "firefox" ? "firefox" : "chromium",
  D = __dirname + "/shots/moderation-" + engine;

const ok = (c, msg) => console.log((c ? "OK   " : "FAIL ") + msg);

(async () => {
  // =================================================================================================================
  // page: a Russian browser, the gate stubbed (bans, the voice flag, unban requests)
  // =================================================================================================================
  const b = await pw[engine].launch();
  const p = await (await b.newContext({ viewport: { width: 1600, height: 900 }, locale: "ru-RU" })).newPage();
  p.on("pageerror", (e) => console.log("pageerror", String(e).slice(0, 300)));

  let banList = [
    { key: "id:7", name: "Gopnik", twitch: false, at: Math.floor(Date.now() / 1000) - 600 },
    { key: "tw:banme", name: "banme", twitch: true, at: Math.floor(Date.now() / 1000) - 7200 }
  ];

  const unbans = [];
  let voice = true;

  await p.route("**/_gate/**", async (r) => {
    const u = r.request().url();
    if (u.endsWith("/_gate/bans")) {
      return r.fulfill({ contentType: "application/json", body: JSON.stringify({ bans: banList }) });
    }
    if (u.endsWith("/_gate/live")) {
      return r.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ started: null, title: null, now: Date.now() / 1000, voice })
      });
    }
    if (u.endsWith("/_gate/unban")) {
      const k = JSON.parse(r.request().postData()).key;
      unbans.push(k);
      banList = banList.filter((x) => x.key !== k);
      return r.fulfill({ status: 204 });
    }
    return r.fulfill({ contentType: "application/json", body: "{}" });
  });

  // =================================================================================================================
  // the viewer becomes a moderator; the chat gets a Twitch twin, the "Twitch" fallback user and a few rows
  // =================================================================================================================
  const reg = p.waitForResponse((r) => r.url().endsWith("/api/chat/register"));
  await p.goto(OC + "/", { waitUntil: "networkidle" }).catch(() => {});
  const me = await (await reg).json();
  await j("POST", "/api/admin/chat/users/setmoderator", { userId: me.id, isModerator: true }, { Authorization: auth });
  const stamp = Date.now() % 100000;
  const victimName = "victim" + stamp;

  const tw = await j(
    "POST",
    "/api/admin/accesstokens/create",
    { name: victimName + " [TW]", scopes: ["CAN_SEND_MESSAGES"] },
    { Authorization: auth }
  );

  const bot = await j(
    "POST",
    "/api/admin/accesstokens/create",
    { name: "Twitch", scopes: ["CAN_SEND_MESSAGES"] },
    { Authorization: auth }
  );

  const send = (t, body) => j("POST", "/api/integrations/chat/send", { body }, { Authorization: "Bearer " + t });
  let rows = [];

  for (let t = 0; t < 20; t++) {
    await send(tw.accessToken, "line " + t);
    await p.waitForTimeout(700);
    rows = await p.$$('[class*="ChatUserMessage-module"][class*="__root"]:has([class*="__modMenuWrapper"])');
    if (rows.length >= 3) break;
  }

  await send(tw.accessToken, "delete me " + stamp);
  await send(bot.accessToken, "someone: a line via Twitch " + stamp);
  await send(tw.accessToken, "the last one " + stamp);
  await p.waitForTimeout(2500);

  const rowBy = async (text) => {
    for (const row of await p.$$('[class*="ChatUserMessage-module"][class*="__root"]')) {
      if ((await row.innerText()).includes(text)) return row;
    }
    return null;
  };

  // =================================================================================================================
  // 1. row buttons on hover, Owncast's "…" menu hidden, no ban on a "Twitch" row
  // =================================================================================================================
  let row = await rowBy("the last one " + stamp);
  await row.hover();
  await p.waitForTimeout(300);
  const vis = (sel) => row.$eval(sel, (e) => getComputedStyle(e).display).catch(() => "missing");

  ok(
    (await vis(".kz-reply-btn")) === "grid" &&
      (await vis(".kz-del-btn")) === "grid" &&
      (await vis(".kz-ban-btn")) === "grid",
    "reply, delete and ban show on hover"
  );

  const mm = await vis('[class*="__modMenuWrapper"]');

  const mmBox = await row
    .$eval('[class*="__modMenuWrapper"]', (e) => {
      const r = e.getBoundingClientRect();
      return r.width + "x" + r.height;
    })
    .catch(() => "-");

  ok(mm === "none" || mmBox === "0x0", "the «…» menu is hidden (" + mm + ", " + mmBox + ")");
  await p.screenshot({ path: D + "-hover.png", clip: { x: 1220, y: 40, width: 380, height: 860 } });
  const twitchRow = await rowBy("a line via Twitch " + stamp);

  ok(
    twitchRow && !(await twitchRow.$(".kz-ban-btn")) && !!(await twitchRow.$(".kz-del-btn")),
    "a «Twitch» line has delete but no ban"
  );

  // =================================================================================================================
  // 2. delete: hidden in Owncast and gone from the feed
  // =================================================================================================================
  row = await rowBy("delete me " + stamp);
  await row.hover();
  await (await row.$(".kz-del-btn")).click();
  await p.waitForTimeout(1500);

  const hidden = (await j("GET", "/api/admin/chat/messages", null, { Authorization: auth })).find((m) =>
    (m.body || "").includes("delete me " + stamp)
  );

  ok(hidden && hidden.hiddenAt, "the message is hidden in Owncast");
  ok(!(await rowBy("delete me " + stamp)), "the row left the feed");

  // =================================================================================================================
  // 3. ban: the first press arms for 3 s, the second bans the author and shows the plate
  // =================================================================================================================
  // Ban from a row decorated for the streamer, with "[TW]" stripped from the nick (data-kz-tw)
  const decorated = async () => {
    for (const r of await p.$$('[class*="ChatUserMessage-module"][class*="__root"]')) {
      const un = await r
        .$eval('[class*="__userName"]', (e) => (e.hasAttribute("data-kz-tw") ? e.textContent.trim() : ""))
        .catch(() => "");
      if (un === victimName) return r;
    }
    return null;
  };

  row = null;

  for (let t = 0; t < 40 && !row; t++) {
    row = await decorated();
    if (!row) await p.waitForTimeout(250);
  }

  ok(!!row, "a styled row of " + victimName + " (nick without [TW])");
  await row.hover();
  let ban = await row.$(".kz-ban-btn");
  await ban.click();
  await p.waitForTimeout(200);

  ok(
    await ban.evaluate((e) => e.classList.contains("kz-armed") && e.textContent.includes("Забанить?")),
    "the first click arms the red «Забанить?»"
  );

  await p.screenshot({ path: D + "-armed.png", clip: { x: 1220, y: 40, width: 380, height: 860 } });
  await p.waitForTimeout(3300);
  ok(!(await ban.evaluate((e) => e.classList.contains("kz-armed"))), "the confirmation resets after 3 s");

  let user = (await j("GET", "/api/admin/chat/messages", null, { Authorization: auth })).find((m) =>
    (m.body || "").includes("the last one " + stamp)
  ).user;

  ok(!user.disabledAt, "one click does not ban");
  row = await decorated();
  await row.hover();
  ban = await row.$(".kz-ban-btn");
  await ban.click();
  await p.waitForTimeout(150);
  await ban.click();
  await p.waitForTimeout(2000);

  user = (await j("GET", "/api/admin/chat/messages", null, { Authorization: auth })).find(
    (m) => (m.body || "").includes("line 0") && m.user.displayName === victimName + " [TW]"
  ).user;

  ok(!!user.disabledAt, "the second click bans the author");
  const note = await p.$eval(".kz-note", (e) => e.textContent).catch(() => "");
  ok(
    note.includes(victimName + " забанен") && note.includes("Баны"),
    "the «забанен» note with the «Баны» button: " + note
  );
  await p.screenshot({ path: D + "-banned.png", clip: { x: 1220, y: 40, width: 380, height: 860 } });
  await j("POST", "/api/admin/chat/users/setenabled", { userId: user.id, enabled: true }, { Authorization: auth });

  // =================================================================================================================
  // 4. the "Bans" window: the count, unban through the gate, "Chat members" closes it
  // =================================================================================================================
  const head = await p.$(".kz-ch-bans");
  ok(!!head, "the «Баны» button in the header");
  await head.click();
  await p.waitForTimeout(800);
  ok((await p.$$(".kz-bans .kz-b-row")).length === 2, "two banned in the window");
  ok((await p.$eval(".kz-ch-bans .kz-ch-count", (e) => e.textContent)) === "2", "the «Баны» button shows 2");
  await p.screenshot({ path: D + "-bans.png", clip: { x: 1220, y: 40, width: 380, height: 860 } });
  await (await p.$('.kz-bans .kz-b-lift[data-key="id:7"]')).click();
  await p.waitForTimeout(800);
  ok(
    unbans.join() === "id:7" && (await p.$$(".kz-bans .kz-b-row")).length === 1,
    "the unban reached the gate, the row is gone"
  );
  await (await p.$(".kz-ch-users")).click();
  await p.waitForTimeout(300);
  ok(!(await p.$(".kz-bans")) && !!(await p.$(".kz-users")), "the members list closes the bans window");

  // =================================================================================================================
  // 5. lost Twitch voice: the "Reconnect" plate comes and goes with it
  // =================================================================================================================
  // The page asks the gate every 30 s, hence the 31 s waits
  voice = false;
  await p.waitForTimeout(31000);
  const go = await p.$eval(".kz-voicebar .kz-vb-go", (e) => e.getAttribute("href")).catch(() => null);
  ok(go === "/_gate/twitch?next=%2F", "voice: false – the «Переподключить» bar (" + go + ")");
  await p.screenshot({ path: D + "-voice.png", clip: { x: 1220, y: 40, width: 380, height: 860 } });
  voice = true;
  await p.waitForTimeout(31000);
  ok(!(await p.$(".kz-voicebar")), "voice: true – no bar");
  await b.close();
})();
