// Hall layout check at 19 window sizes (monitors, browser zoom, tablets, phones).
// Needs a local oc-test with a stream and the styling uploaded (kinozal.sh style --local with OWNCAST_API=...18080).
// Usage: node tools/layout-audit.cjs [part of a size name]; screenshots go to ./shots/ next to the script.
// Playwright: PLAYWRIGHT_PATH (its node_modules/playwright directory, e.g. in the npx cache) or NODE_PATH.
const { chromium } = require(process.env.PLAYWRIGHT_PATH || "playwright");

// ===================================================================================================================
// window sizes: monitors, browser zoom, tablets, phones; 1 – a touch screen
// ===================================================================================================================
const V = [
  ["2560x1300 монитор 1440p", 2560, 1300],
  ["1920x950 1080p+браузер", 1920, 950],
  ["1920x1080 F11", 1920, 1080],
  ["1536x730 ноут 125%", 1536, 730],
  ["1440x800", 1440, 800],
  ["1366x657 ноут+браузер", 1366, 657],
  ["1280x650", 1280, 650],
  ["1536x760 зум125", 1536, 760],
  ["1280x633 зум150", 1280, 633],
  ["960x475 зум200", 960, 475],
  ["640x316 зум300", 640, 316],
  ["1024x768 планшет гор", 1024, 768, 1],
  ["768x1024 планшет верт", 768, 1024, 1],
  ["820x1180 iPad Air", 820, 1180, 1],
  ["390x844 iPhone", 390, 844, 1],
  ["360x740 Android", 360, 740, 1],
  ["320x568 мин", 320, 568, 1],
  ["844x390 телефон гор", 844, 390, 1],
  ["740x360 Android гор", 740, 360, 1]
];

const only = process.argv[2];

(async () => {
  const b = await chromium.launch();

  // =================================================================================================================
  // each size: the page in a Russian browser (its longer labels are the harder case), the session stubbed
  // =================================================================================================================
  for (const [name, w, h, touch] of V) {
    if (only && !name.includes(only)) continue;
    const ctx = await b.newContext({
      locale: "ru-RU",
      viewport: { width: w, height: h },
      isMobile: !!touch && w < 1000,
      hasTouch: !!touch
    });
    const p = await ctx.newPage();
    await p.route("**/_gate/live", (r) => {
      const now = Date.now() / 1000;
      r.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ started: now - 3723, title: "Жизнь Брайана по Монти Пайтону", now })
      });
    });
    await p.goto("http://127.0.0.1:18080/", { waitUntil: "networkidle" }).catch(() => {});
    await p.waitForTimeout(6500);

    // the measurements, taken inside the page
    const r = await p.evaluate((isTouch) => {
      const q = (s) => document.querySelector(s),
        R = (e) => e && e.getBoundingClientRect();
      const vid = R(q(".video-js")),
        cont = R(q('[class*="OwncastPlayer-module"][class*="__container"]'));
      const chatEl = q('[class*="ChatContainer-module"][class*="__chatContainer"]'),
        chat = R(chatEl);
      const mobile = !!q('[class*="__floatingMobileChatModalButton"]') || !!q('[class*="ChatModal-module"]');
      const now = R(q("#kz-now")),
        live = R(q("#kz-live")),
        title = q("#kz-now .kz-title");
      const input = R(q('div[role="textbox"]'));

      // the player's control bar: do its buttons fit
      const bar = q(".vjs-control-bar");
      let ctrlSum = 0;
      if (bar) {
        [...bar.children].forEach((c) => {
          if (c.offsetWidth && !c.className.includes("progress")) ctrlSum += c.offsetWidth;
        });
      }

      // the header: nothing past the window or below the header
      const hdr = R(q("header"));
      let hdrOverflow = false;
      if (q("header")) {
        q("header")
          .querySelectorAll("*")
          .forEach((e) => {
            const box = e.getBoundingClientRect();
            if (box.width && (box.right > innerWidth + 1 || box.bottom > hdr.bottom + 1)) hdrOverflow = true;
          });
      }

      // touch screens: controls with a target under 44px
      const small = [];
      if (isTouch) {
        document.querySelectorAll('button, a[href], [role="button"], [role="slider"]').forEach((e) => {
          const box = e.getBoundingClientRect();
          if (
            box.width > 0 &&
            box.height > 0 &&
            box.top < innerHeight &&
            (box.width < 44 || box.height < 44) &&
            getComputedStyle(e).visibility !== "hidden" &&
            !e.closest(".vjs-menu") &&
            box.left > -100
          ) {
            small.push(
              (e.getAttribute("aria-label") ||
                e.id ||
                e.textContent.trim().slice(0, 14) ||
                e.className.toString().slice(0, 20)) + ` ${Math.round(box.width)}x${Math.round(box.height)}`
            );
          }
        });
      }

      // the result, one line per measurement
      let chatText = "нет";
      if (chat && chat.width) chatText = `${Math.round(chat.width)} (${Math.round((chat.width / innerWidth) * 100)}%)`;
      else if (mobile) chatText = "под видео (окно)";
      return {
        mode: mobile ? "моб" : "комп",
        hScroll: document.documentElement.scrollWidth > innerWidth,
        vScroll: document.documentElement.scrollHeight > innerHeight + 1,
        video: vid
          ? `${Math.round(vid.width)}x${Math.round(vid.height)} (${Math.round((vid.width / innerWidth) * 100)}% ширины, ${Math.round((vid.height / innerHeight) * 100)}% высоты)`
          : "-",
        bars: (() => {
          const v = q(".vjs-tech");
          if (!v || !v.videoWidth || !cont) return "-";
          const tr = v.getBoundingClientRect();
          const k = Math.min(tr.width / v.videoWidth, tr.height / v.videoHeight),
            pw = v.videoWidth * k,
            ph = v.videoHeight * k;
          return `бок ${Math.round(cont.width - pw)} верх/низ ${Math.round(cont.height - ph)} (кадр ${Math.round(pw)}x${Math.round(ph)})`;
        })(),
        chat: chatText,
        plaqueOverlap:
          now && live && live.width
            ? now.right > live.left + 2 && !(live.bottom <= now.top || live.top >= now.bottom)
            : null,
        titleCut: title ? title.scrollWidth > title.clientWidth : null,
        inputVisible: input ? input.bottom <= innerHeight + 1 && input.top >= 0 : null,
        controlsFit: bar ? ctrlSum <= bar.clientWidth : null,
        hdrOverflow,
        small: small.slice(0, 8)
      };
    }, !!touch);

    // the report and a screenshot of this size
    console.log(
      `\n### ${name}\n` +
        Object.entries(r)
          .map(([k, v]) => `  ${k}: ${Array.isArray(v) ? v.join(", ") : v}`)
          .join("\n")
    );
    await p.screenshot({ path: `${require("path").join(__dirname, "shots")}/aud-${w}x${h}.png` });
    await ctx.close();
  }

  await b.close();
})();
