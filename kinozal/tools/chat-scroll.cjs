// Chat autoscroll check (custom.js «chat autoscroll»): slow upward scrolling by 12px keeps the feed still, whether the
// chat is quiet or active; same for a fast jump up; back at the bottom, the feed follows new messages again.
// The gap from the bottom must grow, not drop to 0. Needs a local oc-test with a stream and the styling uploaded
// (as for layout-audit.cjs) and dozens of chat rows. Usage: node tools/chat-scroll.cjs [firefox].
// Playwright: PLAYWRIGHT_PATH or NODE_PATH, as in layout-audit.cjs.
const pw = require(process.env.PLAYWRIGHT_PATH || "playwright");
const chromium = pw[process.argv[2] === "firefox" ? "firefox" : "chromium"];

(async () => {
  // =================================================================================================================
  // page: the hall in a Russian browser, the gate stubbed, the cursor over the chat feed
  // =================================================================================================================
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1600, height: 900 }, locale: "ru-RU" })).newPage();
  await p.route("**/_gate/**", (r) => r.fulfill({ contentType: "application/json", body: "{}" }));
  await p.goto("http://127.0.0.1:18080/", { waitUntil: "networkidle" }).catch(() => {});
  await p.waitForTimeout(7000);
  const sel = '[class*="ChatContainer-module"][class*="__virtuoso"]';
  const gap = () => p.$eval(sel, (v) => Math.round(v.scrollHeight - v.scrollTop - v.clientHeight));
  const box = await (await p.$(sel)).boundingBox();
  await p.mouse.move(box.x + box.width / 2, box.y + box.height / 2);

  // =================================================================================================================
  // chat writer: an Owncast integration that posts lines while the viewer reads above
  // =================================================================================================================
  const auth = "Basic " + Buffer.from("admin:testpass").toString("base64");

  const token = (
    await (
      await fetch("http://127.0.0.1:18080/api/admin/accesstokens/create", {
        method: "POST",
        headers: { "Authorization": auth, "Content-Type": "application/json" },
        body: JSON.stringify({ name: "Поток [TW]", scopes: ["CAN_SEND_MESSAGES"] })
      })
    ).json()
  ).accessToken;

  let k = 0,
    flow = null;

  const send = () =>
    fetch("http://127.0.0.1:18080/api/integrations/chat/send", {
      method: "POST",
      headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ body: "поток " + k++ + " – чат пишет, пока зритель читает выше" })
    }).catch(() => {});

  // =================================================================================================================
  // scrolling up: slowly in a quiet chat, slowly in a busy one, one jump; the gap from the bottom must grow
  // =================================================================================================================
  for (const [name, step, n, pause, live] of [
    ["медленно, чат молчит", -12, 30, 150, false],
    ["медленно, чат пишет", -12, 30, 150, true],
    ["рывок, чат пишет", -400, 1, 0, true]
  ]) {
    if (live) flow = setInterval(send, 700);
    for (let i = 0; i < 6; i++) {
      await p.mouse.wheel(0, 5000);
      await p.waitForTimeout(150);
    }
    await p.waitForTimeout(1500);
    const start = await gap(),
      seen = [];
    for (let i = 0; i < n; i++) {
      await p.mouse.wheel(0, step);
      await p.waitForTimeout(pause);
      seen.push(await gap());
    }
    await p.waitForTimeout(1200);
    clearInterval(flow);
    console.log(
      `${name}: прокручено ${-step * n}px, отступ от низа: старт ${start}, по шагам ${seen.slice(0, 12).join(" ")}…, через 1.2 с ${await gap()}`
    );
  }

  // =================================================================================================================
  // back at the bottom by the wheel: the feed follows new lines again, the gap stays near 0
  // =================================================================================================================
  for (let i = 0; i < 6; i++) {
    await p.mouse.wheel(0, 5000);
    await p.waitForTimeout(150);
  }

  await p.waitForTimeout(1200);
  flow = setInterval(send, 400);
  const follow = [];

  for (let i = 0; i < 8; i++) {
    await p.waitForTimeout(500);
    follow.push(await gap());
  }

  clearInterval(flow);
  console.log("вернулся вниз, чат пишет: отступ от низа " + follow.join(" "));
  await b.close();
})();
