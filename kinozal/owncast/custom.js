// Kinozal: custom JavaScript for the Owncast page, uploaded by kinozal.sh style.
// Each "// ====" banner starts one feature. docs/KINOZAL.md describes them.
(function () {
  "use strict";

  // =================================================================================================================
  // language: the kz_lang cookie (shared with the gate), else the browser's first language
  // =================================================================================================================
  var has = Object.prototype.hasOwnProperty;

  // Browser languages whose speakers usually read Russian. Same list as the gate
  var RU_READERS = ["ru", "uk", "be", "kk", "ky", "uz", "tg", "tk", "az", "hy"];

  var browserLang = ((navigator.languages && navigator.languages[0]) || navigator.language || "")
    .slice(0, 2)
    .toLowerCase();

  var LANG = (function () {
    var m = /(?:^|;\s*)kz_lang=(ru|en)(?:;|$)/.exec(document.cookie);
    if (m) return m[1];
    return !browserLang || RU_READERS.indexOf(browserLang) >= 0 ? "ru" : "en";
  })();

  // The Twitch channel: kinozal.sh style puts in TWITCH_CHANNEL of kinozal/.env on upload
  var CHANNEL = "__TWITCH_CHANNEL__";

  var TEXT = {
    ru: {
      switchTitle: "Switch to English",
      hall: "Кинозал",
      fromStream: "кино со стрима",
      watching: "Смотрим фильм:",
      watchingUntitled: "Смотрим кино",
      channelTitle: "Канал {chan} на Twitch (откроется в новой вкладке)",
      viewers: "Зрители",
      sessionTime: "Сеанс идёт",
      logout: "Выйти",
      logoutTitle: "Выйти из кинозала: войти снова – через Twitch и код",
      settings: "Настройки",
      theater: "Режим кинотеатра",
      theaterOff: "Выйти из режима кинотеатра",
      fromTwitch: "Пишет из чата Twitch",
      chat: "Чат",
      chatMembers: "Участники чата",
      groupBroadcaster: "Стример",
      groupModerator: "Модераторы",
      groupVip: "VIP",
      groupViewer: "Зрители",
      listFailed: "Список не загрузился – попробуем ещё раз",
      loading: "Загрузка…",
      mention: "Упомянуть",
      mentionNick: "Упомянуть @{nick}",
      nobodyFound: "Никого не нашли",
      chatEmpty: "В чате пока никого",
      close: "Закрыть",
      search: "Поиск",
      searchMember: "Поиск участника",
      replyTo: "Ответ ",
      reply: "Ответить",
      cancelReply: "Не отвечать",
      deleteMessage: "Удалить сообщение",
      banNick: "Забанить {nick}",
      deleteNotFound: "Не нашли эту строку в истории чата – не удалена",
      deleteFailed: "Owncast не удалил сообщение – попробуй ещё раз",
      banConfirm: "Забанить?",
      banAgain: "Нажми ещё раз – забанить",
      banNotFound: "Не нашли строку {nick} в истории чата – бан не отправлен",
      banned: "{nick} забанен: в кинозал больше не войдёт, его строки из Twitch сюда не попадут",
      banFailed: "Owncast не принял бан {nick} – попробуй ещё раз",
      bans: "Баны",
      bannedList: "Забаненные",
      noBans: "Никто не забанен",
      twitchViewer: "зритель Twitch",
      hallViewer: "зритель кинозала",
      unban: "Разбанить",
      unbanNick: "Разбанить {nick}",
      unbanFailed: "Не вышло – ещё раз",
      voiceLost: "Твои сообщения сейчас не уходят в чат Twitch",
      reconnect: "Переподключить",
      offline: ""
    },
    en: {
      switchTitle: "Переключить на русский",
      hall: "Cinema",
      fromStream: "films from the stream",
      watching: "Now watching:",
      watchingUntitled: "Watching a film",
      channelTitle: "{chan} on Twitch (opens in a new tab)",
      viewers: "Viewers",
      sessionTime: "Session time",
      logout: "Log out",
      logoutTitle: "Leave the cinema: to come back, sign in with Twitch and the code",
      settings: "Settings",
      theater: "Theatre mode",
      theaterOff: "Exit theatre mode",
      fromTwitch: "Writes from the Twitch chat",
      chat: "Chat",
      chatMembers: "Chat members",
      groupBroadcaster: "Streamer",
      groupModerator: "Moderators",
      groupVip: "VIP",
      groupViewer: "Viewers",
      listFailed: "The list did not load – trying again",
      loading: "Loading…",
      mention: "Mention",
      mentionNick: "Mention @{nick}",
      nobodyFound: "Nobody found",
      chatEmpty: "Nobody in the chat yet",
      close: "Close",
      search: "Search",
      searchMember: "Search for a member",
      replyTo: "Reply to ",
      reply: "Reply",
      cancelReply: "Cancel reply",
      deleteMessage: "Delete message",
      banNick: "Ban {nick}",
      deleteNotFound: "This line is not in the chat history – not deleted",
      deleteFailed: "Owncast did not delete the message – try again",
      banConfirm: "Ban?",
      banAgain: "Press again to ban",
      banNotFound: "The line of {nick} is not in the chat history – ban not sent",
      banned: "{nick} is banned: no way back into the cinema, and their Twitch lines no longer come here",
      banFailed: "Owncast did not accept the ban of {nick} – try again",
      bans: "Bans",
      bannedList: "Banned",
      noBans: "Nobody is banned",
      twitchViewer: "Twitch viewer",
      hallViewer: "cinema viewer",
      unban: "Unban",
      unbanNick: "Unban {nick}",
      unbanFailed: "Did not work – again",
      voiceLost: "Your messages are not reaching the Twitch chat right now",
      reconnect: "Reconnect",
      offline:
        "The broadcast has not started yet or was interrupted. The video appears here by itself – no need to reload the page."
    }
  };

  function txt(key, values) {
    var text = TEXT[LANG][key];
    for (var k in values) {
      if (has.call(values, k)) text = text.split("{" + k + "}").join(values[k]);
    }
    return text;
  }

  // Owncast takes its language from ?lang=, else from the browser
  function addressIn(lang) {
    var u = new URL(location.href);
    u.searchParams.delete("lang");
    if (lang === "en") u.searchParams.set("lang", "en");
    return u.toString();
  }

  if (LANG === "en" && new URL(location.href).searchParams.get("lang") !== "en" && browserLang !== "en") {
    location.replace(addressIn("en"));
  }

  function setLang(lang) {
    document.cookie = "kz_lang=" + lang + "; path=/; max-age=31536000; samesite=lax; secure";
    location.replace(addressIn(lang));
  }

  function englishTitle() {
    if (document.title.indexOf(TEXT.ru.hall) >= 0) {
      document.title = document.title.split(TEXT.ru.hall).join(txt("hall"));
    }
  }

  // Owncast resets the tab title on its updates. Put the English one back at once, not on the next tick
  if (LANG === "en") {
    new MutationObserver(englishTitle).observe(document.head, { childList: true, subtree: true, characterData: true });
    englishTitle();
  }

  // Owncast keeps the header, the tab title and the offline text in Russian. The English page replaces them
  function englishPage() {
    if (LANG !== "en") return;
    var head = document.querySelector('header [class*="Header-module"][class*="__title"]');
    if (head && head.textContent !== txt("hall")) head.textContent = txt("hall");
    englishTitle();
    var off = document.querySelector('#offline-banner [class*="__bodyText"] p');
    if (off && off.textContent !== txt("offline")) off.textContent = txt("offline");
  }

  // =================================================================================================================
  // fonts
  // =================================================================================================================
  // Linked here, not imported in custom.css. Firefox holds a sheet with an @import until Google answers
  // and shows Owncast's own look meanwhile
  var FONTS =
    "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Unbounded:wght@500;700&display=swap";

  if (!document.getElementById("kz-fonts-css")) {
    var fontsLink = document.createElement("link");
    fontsLink.id = "kz-fonts-css";
    fontsLink.rel = "stylesheet";
    fontsLink.href = FONTS;
    document.head.appendChild(fontsLink);
  }

  // Unbounded wraps the heading differently from the fallback font, so the block would jump on load.
  // custom.css hides the text until both fonts are in, 3 s at most
  var BANNER_TEXT = '#offline-banner [class*="__bodyText"]';
  var fontsDeadline = Date.now() + 3000;

  function hasFace(family) {
    var found = false;
    document.fonts.forEach(function (f) {
      if (f.family.replace(/"/g, "") === family) found = true;
    });
    return found;
  }

  function fontsIn() {
    document.documentElement.classList.add("kz-fonts");
  }

  // The @font-face rules come with the linked stylesheet. Poll until the faces exist, then load them
  function waitFonts() {
    if (!document.fonts || Date.now() > fontsDeadline) {
      fontsIn();
      return;
    }
    var bt = document.querySelector(BANNER_TEXT);
    if (!bt || !hasFace("Unbounded") || !hasFace("JetBrains Mono")) {
      setTimeout(waitFonts, 50);
      return;
    }
    var head = getComputedStyle(bt, "::before").content.replace(/^"|"$/g, "");
    Promise.all([
      document.fonts.load('700 1em "Unbounded"', head),
      document.fonts.load('400 1em "JetBrains Mono"', bt.textContent + txt("offline"))
    ]).then(fontsIn, fontsIn);
    setTimeout(fontsIn, fontsDeadline - Date.now());
  }

  waitFonts();

  // =================================================================================================================
  // sound
  // =================================================================================================================
  try {
    localStorage.setItem("owncast_volume", "0");
  } catch (e) {
    /* private mode */
  }

  var muteUntil = Date.now() + 20000;

  function muteOnce() {
    var v = document.querySelector(".video-js video");
    if (v) {
      v.muted = true;
      return;
    }
    if (Date.now() < muteUntil) setTimeout(muteOnce, 250);
  }

  muteOnce();

  // <video> events do not bubble. Capturing them on document survives the player being recreated
  document.addEventListener(
    "volumechange",
    function (e) {
      var v = e.target;
      if (!v || v.tagName !== "VIDEO") return;
      var silentBefore = v.kzSilent !== false;
      v.kzSilent = v.muted || v.volume === 0;
      if (silentBefore && !v.kzSilent && v.volume > 0.6 && !v.kzSoftened) {
        v.kzSoftened = true;
        v.volume = 0.5;
      } else if (silentBefore && !v.muted && v.volume === 0) {
        v.volume = 0.5; // Unmuted at volume 0 would stay silent
      }
    },
    true
  );

  // Key M. Owncast shows "(m)" but has no handler. e.code is the physical key, so the Russian layout works too
  document.addEventListener(
    "keydown",
    function (e) {
      if (e.code !== "KeyM" || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
      var t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      var v = document.querySelector(".video-js video");
      if (!v) return;
      e.preventDefault();
      v.muted = !v.muted;
    },
    true
  ); // Capture phase, since video.js buttons stop key events from propagating

  // =================================================================================================================
  // panel under the player
  // =================================================================================================================
  var EYE =
    '<svg viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true"><path fill="currentColor" ' +
    'd="M12 5c-5 0-9.3 3.1-11 7.5C2.7 16.9 7 20 12 20s9.3-3.1 11-7.5C21.3 8.1 17 5 12 5zm0 12.5a5 5 0 1 1 ' +
    '0-10 5 5 0 0 1 0 10zm0-8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/></svg>';

  var viewers = null,
    online = false,
    started = null,
    title = null,
    skew = 0;

  function two(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function clock(sec) {
    var s = Math.max(0, Math.floor(sec));
    return Math.floor(s / 3600) + ":" + two(Math.floor(s / 60) % 60) + ":" + two(s % 60);
  }

  // React re-renders the block and may drop these elements, so they are re-inserted
  function own(host, id, html) {
    var el = document.getElementById(id);
    if (!el || el.parentNode !== host) {
      if (el) el.remove();
      el = document.createElement("div");
      el.id = id;
      el.innerHTML = html;
      host.appendChild(el);
    }
    return el;
  }

  function render() {
    var host = document.querySelector('[class*="ContentHeader-module"][class*="__root"]');
    if (!host) return;
    var now = own(
      host,
      "kz-now",
      '<div class="kz-ava" role="img" aria-label="' +
        CHANNEL +
        '"></div>' +
        '<div class="kz-text"><h2 class="kz-title"><span class="kz-lead"></span> <span class="kz-film" translate="no">' +
        '</span></h2><p class="kz-sub"><a class="kz-chan" href="https://www.twitch.tv/' +
        CHANNEL +
        '" target="_blank" ' +
        'rel="noopener noreferrer" translate="no" title="' +
        txt("channelTitle", { chan: CHANNEL }) +
        '">' +
        CHANNEL +
        "</a> · <span></span></p></div>"
    );
    var lead = started != null && title ? txt("watching") : "";
    var film = started != null ? title || txt("watchingUntitled") : txt("hall");
    var key = lead + "|" + film;
    if (now.dataset.key !== key) {
      now.dataset.key = key;
      now.querySelector(".kz-lead").textContent = lead;
      now.querySelector(".kz-lead").hidden = !lead;
      now.querySelector(".kz-film").textContent = film;
      now.querySelector(".kz-title").title = (lead ? lead + " " : "") + film;
      now.querySelector(".kz-sub span").textContent = started != null ? txt("hall") : txt("fromStream");
    }
    now.querySelector(".kz-ava").classList.toggle("kz-on", online);

    var live = own(
      host,
      "kz-live",
      '<span class="kz-viewers" title="' +
        txt("viewers") +
        '">' +
        EYE +
        "<b></b></span>" +
        '<span class="kz-timer" title="' +
        txt("sessionTime") +
        '"></span>'
    );
    live.hidden = !online;
    live.querySelector(".kz-viewers b").textContent = viewers == null ? "–" : String(viewers);
    var tm = live.querySelector(".kz-timer");
    tm.hidden = started == null;
    if (started != null) tm.textContent = clock(Date.now() / 1000 + skew - started);
  }

  function getJSON(url, ok) {
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (d) {
        if (d) ok(d);
      })
      .catch(function () {
        /* retried on the next poll */
      });
  }

  function pollStatus() {
    getJSON("/api/status", function (d) {
      online = !!d.online;
      viewers = d.viewerCount || 0; // Owncast omits a zero count
    });
  }

  // false when /_gate/live says the viewer's lines no longer reach Twitch (see «reconnect»)
  var voice = null;

  function pollLive() {
    getJSON("/_gate/live", function (d) {
      started = d.started;
      title = d.title || null;
      skew = d.now - Date.now() / 1000;
      voice = d.voice === undefined ? null : d.voice;
    });
  }

  // =================================================================================================================
  // "Log out" and language buttons in the header, next to the nick
  // =================================================================================================================
  function logoutButton() {
    if (document.getElementById("kz-logout")) return;
    var user = document.querySelector('[class*="UserDropdown-module"][class*="__root"]');
    if (!user || !user.parentNode) return;
    var a = document.createElement("a");
    a.id = "kz-logout";
    a.href = "/_gate/logout";
    a.textContent = txt("logout");
    a.title = txt("logoutTitle");
    user.parentNode.insertBefore(a, user);
  }

  // RU | EN switch with the current language highlighted. Each name is written in its own language
  var LANG_NAMES = { ru: "Русский", en: "English" };

  function langButton() {
    var logout = document.getElementById("kz-logout");
    if (!logout || document.getElementById("kz-lang")) return;
    var group = document.createElement("span");
    group.id = "kz-lang";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Язык / Language");
    ["ru", "en"].forEach(function (lang) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = lang.toUpperCase();
      b.setAttribute("lang", lang);
      b.setAttribute("aria-pressed", lang === LANG ? "true" : "false");
      b.setAttribute("aria-label", LANG_NAMES[lang]);
      b.title = lang === LANG ? LANG_NAMES[lang] : txt("switchTitle");
      if (lang !== LANG) {
        b.addEventListener("click", function () {
          setLang(lang);
        });
      }
      group.appendChild(b);
    });
    logout.parentNode.insertBefore(group, logout);
  }

  // =================================================================================================================
  // Russian UI: Owncast's labels translated on the fly (the Russian mode only)
  // =================================================================================================================
  document.documentElement.lang = LANG;
  var vp = document.querySelector('meta[name="viewport"]');
  if (vp) vp.setAttribute("content", "width=device-width, initial-scale=1");

  var RU = {
    "Send a message to chat": "Сообщение в чат",
    "Chat text input": "Сообщение в чат",
    "Chat is offline": "Чат закрыт",
    "Chat will be available when the stream is live.": "Чат откроется вместе с трансляцией",
    "joined the chat.": "теперь в чате",
    "left the chat.": "больше не в чате",
    "Emoji picker": "Эмодзи",
    "Emoji picker button": "Эмодзи",
    "Send message": "Отправить",
    "Send message Button": "Отправить",
    "Bot": "Бот",
    "Hide Chat (c)": "Скрыть чат (C)",
    "Show Chat (c)": "Показать чат (C)",
    "Pop out chat": "Чат в отдельном окне",
    "Scroll to latest": "К новым сообщениям",
    "Go to last message": "К новым сообщениям",
    "Skip to player": "К плееру",
    "Skip to page content": "К описанию",
    "Skip to offline message": "К сообщению",
    "Перейти к игроку": "К плееру",
    "Перейти к содержимому страницы": "К описанию",
    "Video Player": "Видеоплеер",
    "Video Player is loading.": "Плеер загружается…",
    "Play Video": "Смотреть",
    "Play": "Смотреть",
    "Play (Space)": "Смотреть (пробел)",
    "Pause": "Пауза",
    "Pause (Space)": "Пауза (пробел)",
    "Mute": "Выключить звук",
    "Mute (m)": "Выключить звук (M)",
    "Unmute": "Включить звук",
    "Unmute (m)": "Включить звук (M)",
    "Volume Level": "Громкость",
    "Fullscreen": "Во весь экран",
    "Fullscreen (f)": "Во весь экран (F)",
    "Non-Fullscreen": "Выйти из полноэкранного режима",
    "Exit Fullscreen": "Выйти из полноэкранного режима",
    "Exit Fullscreen (f)": "Выйти из полноэкранного режима (F)",
    "Picture-in-Picture": "Картинка в картинке",
    "Picture-in-Picture (i)": "Картинка в картинке (I)",
    "Exit Picture-in-Picture": "Закрыть картинку в картинке",
    "Exit Picture-in-Picture (i)": "Закрыть картинку в картинке (I)",
    "Seek to live, currently playing live": "Прямой эфир: смотрите без отставания",
    "Seek to live, currently behind live": "Догнать прямой эфир",
    "Settings": "Настройки",
    "Minimize latency": "Меньше задержка",
    "Experimental: slightly speeds up playback to keep you closer to live":
      "Чуть ускоряет видео, чтобы держаться ближе к прямому эфиру (экспериментально)",
    "Cancel": "Отмена",
    "OK": "Да",
    "OFF": "ВЫКЛ",
    "ON": "ВКЛ",
    "The stream is ending.": "Трансляция заканчивается",
    "The media could not be loaded, either because the server or network failed or because the format is not supported.":
      "Видео не загрузилось. Обновите страницу через минуту.",
    // antd icon names. Screen readers read them out next to controls and chat events
    "user": "пользователь",
    "caret-down": "меню",
    "usergroup-add": "вошёл в чат",
    "usergroup-delete": "вышел из чата",
    "smile": "эмодзи",
    "send": "отправить"
  };

  // Phrases split by markup, translated as a whole element. Owncast posts "Stay tuned…" as the stream starts
  var PHRASES = { "Stay tuned, the stream is starting!": "Трансляция начинается, устраивайтесь поудобнее" };

  var RX = [
    [
      /^Sent (.+)$/,
      function (m, x) {
        return "Отправлено в " + clock24(x);
      }
    ],
    [
      /^System message: (.+)$/,
      function (m, x) {
        return "Системное сообщение: " + (PHRASES[x] || RU[x] || x);
      }
    ]
  ];

  var SKIP = '[class*="ChatUserMessage-module"][class*="__message"], #kz-now, #kz-live';

  function tr(s) {
    var k = s.trim();
    if (!k || LANG !== "ru") return null;
    if (Object.prototype.hasOwnProperty.call(RU, k)) return s.replace(k, RU[k]);
    for (var i = 0; i < RX.length; i++) if (RX[i][0].test(k)) return k.replace(RX[i][0], RX[i][1]);
    return null;
  }

  var ATTRS = ["title", "aria-label", "placeholder"];

  function trElement(el) {
    for (var i = 0; i < ATTRS.length; i++) {
      var v = el.getAttribute(ATTRS[i]);
      if (v) {
        var r = tr(v);
        if (r != null && r !== v) el.setAttribute(ATTRS[i], r);
      }
    }
  }

  var TWIN = / \[TW\]$/;

  function clock24(s) {
    var m = /^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)$/i.exec(s.trim());
    if (!m) return s;
    var h = (Number(m[1]) % 12) + (/p/i.test(m[3]) ? 12 : 0);
    return (h < 10 ? "0" : "") + h + ":" + m[2];
  }

  function trTooltip(el) {
    if (LANG !== "ru") return;
    var m = /^(.+?)( \[TW\])? first joined (.+)$/.exec(el.textContent.trim());
    if (m) el.textContent = (m[2] ? "[TW] " : "") + m[1] + " в чате с " + clock24(m[3]);
  }

  function trText(n) {
    var p = n.parentElement;
    if (!p) return;
    if (p.classList.contains("ant-tooltip-container")) {
      trTooltip(p);
      return;
    }
    // "nick [TW]" becomes "nick" with data-kz-tw. CSS draws the label before the nick
    if (TWIN.test(n.nodeValue) && /__userName/.test(p.className)) {
      n.nodeValue = n.nodeValue.replace(TWIN, "");
      p.setAttribute("data-kz-tw", "");
      return;
    }
    if (p.closest(SKIP)) return;
    for (var e = p, up = 0; e && up < 3; e = e.parentElement, up++) {
      var k = e.textContent.trim();
      if (LANG === "ru" && Object.prototype.hasOwnProperty.call(PHRASES, k)) {
        e.textContent = PHRASES[k];
        return;
      }
    }
    var r = tr(n.nodeValue);
    if (r != null && r !== n.nodeValue) n.nodeValue = r;
  }

  function translate(root) {
    if (root.nodeType === 3) {
      trText(root);
      return;
    }
    if (root.nodeType !== 1 || (root.closest && root.closest(SKIP))) return;
    trElement(root);
    var els = root.querySelectorAll("[title],[aria-label],[placeholder]");
    for (var i = 0; i < els.length; i++) trElement(els[i]);
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT),
      n;
    while ((n = w.nextNode())) trText(n);
  }

  function startTranslating() {
    translate(document.body);
    englishPage();
    new MutationObserver(function (list) {
      for (var i = 0; i < list.length; i++) {
        var m = list[i];
        if (m.type === "childList") {
          for (var j = 0; j < m.addedNodes.length; j++) {
            translate(m.addedNodes[j]);
            decorate(m.addedNodes[j]);
          }
        } else if (m.type === "characterData") trText(m.target);
        else if (m.type === "attributes") trElement(m.target);
      }
      // Runs before paint, so the English page never flashes the Russian header or offline text
      englishPage();
    }).observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ATTRS
    });
  }

  // The video.js gear button has no label. Screen readers would say only "button"
  function labelSettings() {
    var b = document.querySelectorAll(".video-js .vjs-menu-button-popup > button:not([aria-label])");
    for (var i = 0; i < b.length; i++) {
      b[i].setAttribute("aria-label", txt("settings"));
      b[i].title = txt("settings");
    }
  }

  // =================================================================================================================
  // phone: chat open from the start, below the video
  // =================================================================================================================
  var chatOpened = false;

  function mobileChat() {
    var btn = document.querySelector('button[class*="__floatingMobileChatModalButton"]');
    var below = document.querySelector('[class*="ContentHeader-module"][class*="__root"]');
    if (!below || !below.getBoundingClientRect().height) below = document.querySelector(".video-js"); // panel hidden (theater mode)
    if (below) {
      var y = Math.round(below.getBoundingClientRect().bottom);
      document.documentElement.style.setProperty("--kz-chat-top", Math.max(y, 120) + "px");
    }
    // Landscape phone. The film gets the space, chat stays behind the "Chat" button
    if (!btn || chatOpened || !online || innerWidth > innerHeight) return;
    if (!document.querySelector('[class*="ChatModal-module"]')) {
      chatOpened = true;
      btn.click();
    }
  }

  // =================================================================================================================
  // phone: fullscreen in landscape
  // =================================================================================================================
  function onFullscreen() {
    var fs = document.fullscreenElement || document.webkitFullscreenElement;
    var o = screen.orientation;
    if (!o || !o.lock || !matchMedia("(pointer: coarse)").matches) return;
    if (fs && fs.closest && (fs.matches(".video-js") || fs.querySelector(".video-js"))) {
      o.lock("landscape").catch(function () {
        /* Unsupported. The viewer rotates by hand */
      });
    } else if (o.unlock) {
      try {
        o.unlock();
      } catch (e) {
        /* no lock was held */
      }
    }
  }

  document.addEventListener("fullscreenchange", onFullscreen);
  document.addEventListener("webkitfullscreenchange", onFullscreen);

  // =================================================================================================================
  // theater mode
  // =================================================================================================================
  var THEATER_ICON =
    '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" ' +
    'd="M3 6.5A1.5 1.5 0 0 1 4.5 5h15A1.5 1.5 0 0 1 21 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5v-11zM5 7v10h10V7H5zm12 0v10h2V7h-2z"/></svg>';

  var theater = false;

  try {
    theater = localStorage.getItem("kz_theater") === "1";
  } catch (e) {
    /* private mode */
  }

  function setTheater(on) {
    theater = on;
    document.body.classList.toggle("kz-theater", on);
    try {
      localStorage.setItem("kz_theater", on ? "1" : "0");
    } catch (e) {
      /* private mode */
    }
    var b = document.getElementById("kz-theater-btn");
    if (b) {
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.title = (on ? txt("theaterOff") : txt("theater")) + " (T)";
      b.setAttribute("aria-label", on ? txt("theaterOff") : txt("theater"));
    }
    setTimeout(function () {
      window.dispatchEvent(new Event("resize"));
    }, 50); // makes video.js recompute its layout
  }

  function theaterButton() {
    var bar = document.querySelector(".video-js .vjs-control-bar");
    if (!bar || document.getElementById("kz-theater-btn")) return;
    var b = document.createElement("button");
    b.id = "kz-theater-btn";
    b.type = "button";
    b.className = "vjs-control vjs-button kz-theater-btn";
    b.innerHTML = THEATER_ICON;
    b.addEventListener("click", function () {
      setTheater(!theater);
    });
    bar.insertBefore(b, bar.querySelector(".vjs-fullscreen-control"));
    setTheater(theater);
  }

  document.addEventListener(
    "keydown",
    function (e) {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
      var t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (e.code === "KeyT") {
        e.preventDefault();
        setTheater(!theater);
      } else if (e.code === "Escape" && theater && !document.fullscreenElement) setTheater(false);
    },
    true
  );

  if (theater && document.body) document.body.classList.add("kz-theater");

  // =================================================================================================================
  // chat look: Twitch nick colours and emotes
  // =================================================================================================================
  var look = { colors: {}, emotes: {}, url: "", badges: {}, badgeImages: {} };

  function chan(v) {
    var c = v / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }

  function lum(c) {
    return 0.2126 * chan(c[0]) + 0.7152 * chan(c[1]) + 0.0722 * chan(c[2]);
  }

  var PANEL = lum([26, 26, 26]),
    readableCache = {};

  function readable(hex) {
    // Like Twitch "readable colors". A dark nick is mixed toward white until contrast reaches 4.5:1
    if (readableCache[hex]) return readableCache[hex];
    var c = [1, 3, 5].map(function (i) {
      return parseInt(hex.substr(i, 2), 16);
    });
    for (var k = 0; k < 12 && (lum(c) + 0.05) / (PANEL + 0.05) < 4.5; k++) {
      c = c.map(function (v) {
        return Math.round(v + (255 - v) * 0.15);
      });
    }
    return (readableCache[hex] = "rgb(" + c.join(",") + ")");
  }

  var NAME_SEL =
    '[class*="ChatUserMessage-module"] [class*="__userName"], [class*="ChatEventMessage-module"] [class*="__user"]';

  var MSG_SEL = '[class*="ChatUserMessage-module"] [class*="__message"]:not([class*="__messagePadding"])';

  function within(root, sel) {
    if (root.nodeType !== 1) return [];
    var list = Array.prototype.slice.call(root.querySelectorAll(sel));
    if (root.matches && root.matches(sel)) list.push(root);
    return list;
  }

  // "" when the nick has no Twitch colour
  function nickColor(name) {
    var key = name.toLowerCase();
    return has.call(look.colors, key) && look.colors[key] ? readable(look.colors[key]) : "";
  }

  function paintNames(root) {
    within(root, NAME_SEL).forEach(function (el) {
      var color = nickColor(el.textContent.trim());
      if (color) el.style.color = color;
    });
  }

  function emoteImg(name, id) {
    var img = document.createElement("img");
    img.className = "kz-emote";
    img.alt = name;
    img.title = name;
    img.src = look.url.replace("{id}", id).replace("{scale}", "1.0");
    img.srcset = look.url.replace("{id}", id).replace("{scale}", "2.0") + " 2x";
    img.loading = "lazy";
    return img;
  }

  function drawEmotes(root) {
    if (!look.url) return;
    within(root, MSG_SEL).forEach(function (msg) {
      var w = document.createTreeWalker(msg, NodeFilter.SHOW_TEXT),
        nodes = [],
        n;
      while ((n = w.nextNode())) if (!n.parentElement.closest("a, code")) nodes.push(n);
      nodes.forEach(function (node) {
        var parts = node.nodeValue.split(/(\s+)/),
          hit = false;
        for (var i = 0; i < parts.length; i++) {
          if (has.call(look.emotes, parts[i])) {
            hit = true;
            break;
          }
        }
        if (!hit) return;
        var frag = document.createDocumentFragment();
        parts.forEach(function (part) {
          if (part && has.call(look.emotes, part)) frag.appendChild(emoteImg(part, look.emotes[part]));
          else if (part) frag.appendChild(document.createTextNode(part));
        });
        node.parentNode.replaceChild(frag, node);
      });
    });
  }

  // =================================================================================================================
  // chat rows: time, [TW] and Twitch badges before the nick
  // =================================================================================================================
  var BADGE_RU = {
    "Broadcaster": "Стример",
    "Moderator": "Модератор",
    "VIP": "VIP",
    "Subscriber": "Подписчик",
    "Founder": "Основатель",
    "Verified": "Проверенный",
    "Partner": "Партнёр",
    "Staff": "Сотрудник Twitch",
    "Lead Moderator": "Главный модератор",
    "Artist": "Художник канала"
  };

  // img is [1x url, 2x url, title] from /_gate/look
  function badgeImg(b, img) {
    var el = document.createElement("img"),
      label = (LANG === "ru" && BADGE_RU[img[2]]) || img[2] || b.split("/")[0];
    el.className = "kz-badge";
    el.src = img[0];
    el.srcset = img[1] + " 2x";
    el.alt = label;
    el.title = label;
    return el;
  }

  function rowTime(row) {
    var m = /(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)?/i.exec(row.getAttribute("title") || "");
    if (!m) return "";
    var h = Number(m[1]);
    if (m[3]) h = (h % 12) + (/p/i.test(m[3]) ? 12 : 0);
    return (h < 10 ? "0" : "") + h + ":" + m[2];
  }

  var ROW_SEL = '[class*="ChatUserMessage-module"][class*="__root"]';
  var WHO_SEL = '[class*="__user"]:not([class*="__userName"]):not([class*="__userBadges"]), [class*="__repeatUser"]';

  function decorateRows(root) {
    within(root, ROW_SEL).forEach(function (row) {
      var who = row.querySelector(WHO_SEL),
        nameEl = who && who.querySelector('[class*="__userName"]');
      if (!who || !nameEl) return;
      var login = nameEl.textContent.trim().toLowerCase(),
        twin = nameEl.hasAttribute("data-kz-tw");
      var badges = has.call(look.badges || {}, login) ? look.badges[login] : [];
      var tm = rowTime(row),
        images = look.badgeImages || {};
      var key = [
        tm,
        twin,
        badges.join(","),
        badges.filter(function (b) {
          return images[b];
        }).length
      ].join("|");
      var pre = who.querySelector(":scope > .kz-pre");
      if (pre && pre.getAttribute("data-key") === key) return;
      if (!pre) {
        pre = document.createElement("span");
        pre.className = "kz-pre";
        who.insertBefore(pre, who.firstChild);
      }
      pre.setAttribute("data-key", key);
      pre.textContent = "";
      if (tm) {
        var t = document.createElement("span");
        t.className = "kz-time";
        t.textContent = tm;
        pre.appendChild(t);
      }
      if (twin) {
        var tw = document.createElement("span");
        tw.className = "kz-tw";
        tw.textContent = "[TW]";
        tw.title = txt("fromTwitch");
        pre.appendChild(tw);
      }
      badges.forEach(function (b) {
        if (images[b]) pre.appendChild(badgeImg(b, images[b]));
      });
      who.classList.toggle("kz-has-badges", pre.querySelector(".kz-badge") !== null);
    });
  }

  function decorate(root) {
    paintNames(root);
    drawEmotes(root);
    decorateRows(root);
    replyRows(root);
    modRows(root);
  }

  function pollLook() {
    getJSON("/_gate/look", function (d) {
      look = {
        colors: d.colors || {},
        emotes: d.emotes || {},
        url: typeof d.url === "string" ? d.url : "",
        badges: d.badges || {},
        badgeImages: d.badgeImages || {}
      };
      var chat = document.querySelectorAll('[class*="ChatContainer-module"][class*="__chatContainer"]');
      for (var i = 0; i < chat.length; i++) decorate(chat[i]);
    });
  }

  // =================================================================================================================
  // chat autoscroll: the latest message stays visible
  // =================================================================================================================
  // Only the viewer's own scrolling changes stickiness. Re-sticking within 60px works only downwards.
  // A slow wheel step up moves 12 to 57px and re-renders virtuoso, so a two-way threshold would pull the feed back
  var stick = { el: null, on: true, userAt: 0, readTop: 0, lastTop: 0, dir: 0, drag: false };

  function gapOf(v) {
    return v.scrollHeight - v.scrollTop - v.clientHeight;
  }

  // A scrollbar drag counts as viewer scrolling until release. Otherwise Owncast snaps the feed back and it flickers
  function userScrolled() {
    return stick.drag || Date.now() - stick.userAt < 500;
  }

  function stickNow() {
    var v = stick.el;
    if (!v || !v.isConnected) return;
    // Nothing to scroll means sticky. Otherwise a wheel step up in a short chat would unstick it for good
    if (v.scrollHeight - v.clientHeight < 4) stick.on = true;
    // While the viewer scrolls, the scroll handler decides. A virtuoso re-render must not move the feed
    if (!stick.on || userScrolled()) return;
    if (gapOf(v) > 1) v.scrollTop = v.scrollHeight;
    requestAnimationFrame(function () {
      if (stick.on && !userScrolled() && gapOf(v) > 1) v.scrollTop = v.scrollHeight;
    }); // after virtuoso has measured and rendered
  }

  function bindChat() {
    var v = document.querySelector('[class*="ChatContainer-module"][class*="__virtuoso"]');
    if (!v || v === stick.el) return;
    stick.el = v;
    stick.on = true;
    var mark = function (dir) {
      stick.userAt = Date.now();
      stick.dir = dir || 0;
    };
    // A step up is only marked, its position is not recorded. Chromium scrolls before the wheel event arrives.
    // The scroll handler unsticks once the feed really moves up. Touchpad jitter scrolls nothing
    var up = function () {
      mark(-1);
    };
    v.addEventListener(
      "wheel",
      function (e) {
        if (e.ctrlKey) return; // Ctrl+wheel and touchpad pinch zoom the page
        if (e.deltaY < 0) up();
        else mark(1);
      },
      { passive: true }
    );
    v.addEventListener(
      "touchmove",
      function () {
        mark(0);
      },
      { passive: true }
    );
    // A press on the scrollbar, not on a row. Left button only, since the right one opens a menu
    // and its release may never arrive
    v.addEventListener("mousedown", function (e) {
      if (e.target === v && e.button === 0) {
        mark(0);
        stick.drag = true;
      }
    });
    v.addEventListener("keydown", function (e) {
      if (/^(PageUp|ArrowUp|Home)$/.test(e.code)) up();
      else if (/^(PageDown|ArrowDown|End|Space)$/.test(e.code)) mark(1);
    });
    v.addEventListener(
      "scroll",
      function () {
        var top = v.scrollTop,
          rising = top < stick.lastTop - 1,
          sinking = top > stick.lastTop + 1;
        stick.lastTop = top;
        // The feed moving down during an upward scroll is Owncast catching up with a new message
        if (userScrolled() && !(stick.dir < 0 && sinking)) {
          if (rising && gapOf(v) > 4) stick.on = false;
          else if ((sinking && gapOf(v) < 60) || gapOf(v) < 4) stick.on = true;
          stick.readTop = top;
        } else if (!stick.on && v.scrollTop > stick.readTop + 2) {
          // The viewer reads above, yet the feed is pulled down. Owncast's virtual list underestimates rows extended
          // with time and badges, thinks the viewer is at the bottom and scrolls to catch up
          v.scrollTop = stick.readTop;
        } else if (gapOf(v) < 4) stick.on = true;
      },
      { passive: true }
    );
    var ro = new ResizeObserver(stickNow);
    ro.observe(v);
    for (var c = v.firstElementChild; c; c = c.firstElementChild) {
      ro.observe(c);
      if (c.children.length !== 1) break;
    }
    new MutationObserver(stickNow).observe(v, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["style"]
    });
    stickNow();
  }

  function dragEnd() {
    if (!stick.drag) return;
    stick.drag = false;
    stick.userAt = Date.now();
    if (stick.el) {
      stick.readTop = stick.el.scrollTop;
      stick.lastTop = stick.readTop;
    }
  }

  document.addEventListener("mouseup", dragEnd, true);

  // A scrollbar drag can lose its mouseup. Mouse movement with no buttons pressed ends the drag
  document.addEventListener(
    "mousemove",
    function (e) {
      if (stick.drag && e.buttons === 0) dragEnd();
    },
    true
  );

  document.addEventListener("contextmenu", dragEnd, true);
  window.addEventListener("blur", dragEnd);

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () {
      setTimeout(stickNow, 50);
    });
  }

  document.addEventListener(
    "click",
    function (e) {
      if (e.target.closest && e.target.closest('[class*="ChatContainer-module"][class*="__toBottomWrap"]')) {
        stick.on = true;
        stick.userAt = 0; // Go down even right after a wheel scroll
        stickNow();
      }
    },
    true
  );

  // =================================================================================================================
  // chat members
  // =================================================================================================================
  var FIELD_SEL = '[class*="ChatContainer-module"][class*="__chatTextField"]';
  var CHAT_SEL = '[class*="ChatContainer-module"][class*="__chatContainer"]';

  var USERS_ICON =
    '<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><path fill="currentColor" ' +
    'd="M7.5 3a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zM2 17.5C2 14.46 4.46 12 7.5 12s5.5 2.46 5.5 5.5V18H2v-.5zM14 4.1a3.5 3.5 ' +
    '0 0 1 0 6.8V9.3a2 2 0 0 0 0-3.6V4.1zm1.2 8.2A6.5 6.5 0 0 1 18 17.5v.5h-3.5v-.5a7 7 0 0 0-1.6-4.5 6.6 6.6 0 0 1 2.3-.7z"/></svg>';

  var GROUPS = [
    ["broadcaster", "groupBroadcaster"],
    ["moderator", "groupModerator"],
    ["vip", "groupVip"],
    ["viewer", "groupViewer"]
  ];

  var people = { list: null, at: 0, failed: false, asking: false, waiting: [] };

  // A request in flight queues the callback. Dropping it would leave the panel on "Loading…"
  function loadUsers(done) {
    if (done) people.waiting.push(done);
    if (people.asking) return;
    people.asking = true;
    fetch("/_gate/users", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(function (d) {
        people.list = d.users || [];
        people.failed = false;
      })
      .catch(function () {
        people.failed = true;
      })
      .then(function () {
        people.asking = false;
        people.at = Date.now();
        var w = people.waiting;
        people.waiting = [];
        w.forEach(function (f) {
          f();
        });
      });
  }

  function badgesOf(name) {
    var key = name.toLowerCase();
    return has.call(look.badges || {}, key) ? look.badges[key] : [];
  }

  function groupOf(u) {
    var kinds = badgesOf(u.name).map(function (b) {
      return b.split("/")[0];
    });
    if (kinds.indexOf("broadcaster") >= 0) return "broadcaster";
    if (u.mod || kinds.indexOf("moderator") >= 0 || kinds.indexOf("lead_moderator") >= 0) return "moderator";
    if (kinds.indexOf("vip") >= 0) return "vip";
    return "viewer";
  }

  function nickParts(host, name) {
    var images = look.badgeImages || {};
    badgesOf(name).forEach(function (b) {
      if (images[b]) host.appendChild(badgeImg(b, images[b]));
    });
    var n = document.createElement("span");
    n.className = "kz-nick";
    n.textContent = name;
    var color = nickColor(name);
    if (color) n.style.color = color;
    host.appendChild(n);
  }

  // The panel ends above the input, which grows with the text
  function fitAboveInput(panel) {
    var field = panel.parentNode && panel.parentNode.querySelector(FIELD_SEL);
    if (field) panel.style.bottom = field.offsetHeight + "px";
  }

  function renderUsers(panel) {
    fitAboveInput(panel);
    var box = panel.querySelector(".kz-u-list"),
      q = panel.querySelector(".kz-u-search").value.trim().toLowerCase();
    var count = panel.querySelector(".kz-u-count");
    box.textContent = "";
    if (people.list === null) {
      count.textContent = "";
      box.innerHTML = '<p class="kz-u-note">' + (people.failed ? txt("listFailed") : txt("loading")) + "</p>";
      return;
    }
    count.textContent = people.list.length ? String(people.list.length) : "";
    var by = {};
    people.list.forEach(function (u) {
      if (q && u.name.toLowerCase().indexOf(q) < 0) return;
      var g = groupOf(u);
      (by[g] = by[g] || []).push(u);
    });
    GROUPS.forEach(function (g) {
      if (!by[g[0]]) return;
      var h = document.createElement("h3");
      h.className = "kz-u-group";
      h.textContent = txt(g[1]);
      box.appendChild(h);
      by[g[0]].forEach(function (u) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "kz-u-name";
        b.title = txt("mentionNick", { nick: u.name });
        b.setAttribute("data-nick", u.name);
        nickParts(b, u.name);
        box.appendChild(b);
      });
    });
    if (!box.firstChild) {
      box.innerHTML = '<p class="kz-u-note">' + (q ? txt("nobodyFound") : txt("chatEmpty")) + "</p>";
    }
  }

  function closeUsers(panel) {
    if (panel) panel.remove();
  }

  function openUsers(chat) {
    var panel = document.createElement("div");
    panel.className = "kz-users";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", txt("chatMembers"));
    panel.innerHTML =
      '<div class="kz-u-head"><span class="kz-u-title">' +
      txt("chatMembers") +
      ' <span class="kz-u-count"></span></span>' +
      '<button type="button" class="kz-u-close" aria-label="' +
      txt("close") +
      '" title="' +
      txt("close") +
      '">×</button></div>' +
      '<input class="kz-u-search" type="search" placeholder="' +
      txt("search") +
      '" aria-label="' +
      txt("searchMember") +
      '" autocomplete="off">' +
      '<div class="kz-u-list"></div>';
    chat.appendChild(panel);
    panel.querySelector(".kz-u-close").addEventListener("click", function () {
      closeUsers(panel);
    });
    panel.querySelector(".kz-u-search").addEventListener("input", function () {
      renderUsers(panel);
    });
    panel.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeUsers(panel);
    });
    panel.querySelector(".kz-u-list").addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".kz-u-name");
      if (!b) return;
      closeUsers(panel);
      insertMention(chat.querySelector("#chat-input-content-editable"), b.getAttribute("data-nick"));
    });
    renderUsers(panel);
    loadUsers(function () {
      if (panel.isConnected) renderUsers(panel);
    });
  }

  function chatHead() {
    document.querySelectorAll(CHAT_SEL).forEach(function (chat) {
      if (chat.querySelector(":scope > .kz-chathead")) return;
      var head = document.createElement("div");
      head.className = "kz-chathead";
      head.innerHTML =
        '<span class="kz-ch-title">' +
        txt("chat") +
        "</span>" +
        '<button type="button" class="kz-ch-users" aria-label="' +
        txt("chatMembers") +
        '" title="' +
        txt("chatMembers") +
        '">' +
        USERS_ICON +
        "</button>";
      chat.insertBefore(head, chat.firstChild);
      head.querySelector(".kz-ch-users").addEventListener("click", function () {
        var open = chat.querySelector(":scope > .kz-users");
        closePanels(chat);
        if (!open) openUsers(chat);
      });
    });
    var panels = document.querySelectorAll(".kz-users");
    panels.forEach(fitAboveInput);
    if (panels.length && Date.now() - people.at > 10000) {
      loadUsers(function () {
        panels.forEach(function (p) {
          if (p.isConnected) renderUsers(p);
        });
      });
    }
  }

  // =================================================================================================================
  // mentions: nick suggestions on "@"
  // =================================================================================================================
  var MENTION_MAX = 7;
  var mention = { box: null, ed: null, items: [], pick: 0 };

  // "@prefix" right before the caret as { q, range }. The range is replaced on selection
  function caretQuery(ed) {
    var sel = getSelection();
    if (!ed || !sel.rangeCount || !sel.isCollapsed) return null;
    var node = sel.anchorNode,
      off = sel.anchorOffset;
    if (!node || node.nodeType !== 3 || !ed.contains(node)) return null;
    var m = /(^|\s)@([^\s@]{0,25})$/.exec(node.data.slice(0, off));
    if (!m) return null;
    var r = document.createRange();
    r.setStart(node, off - m[2].length - 1);
    r.setEnd(node, off);
    return { q: m[2].toLowerCase(), range: r };
  }

  // A nick with a space cannot be mentioned. "Twitch" is the gate's fallback sender, not a person
  function knownNicks() {
    var seen = { twitch: 1 },
      out = [];
    var me = document.querySelector('[class*="UserDropdown-module"][class*="__username"]');
    if (me && me.textContent.trim()) seen[me.textContent.trim().toLowerCase()] = 1;
    function add(raw) {
      var n = (raw || "").replace(TWIN, "").replace(/:$/, "").trim(),
        k = n.toLowerCase();
      if (!n || /\s/.test(n) || has.call(seen, k)) return;
      seen[k] = 1;
      out.push(n);
    }
    var names = document.querySelectorAll(ROW_SEL + ' [class*="__userName"]');
    for (var i = names.length - 1; i >= 0; i--) add(names[i].textContent);
    (people.list || []).forEach(function (u) {
      add(u.name);
    });
    return out;
  }

  function hideMention() {
    if (mention.box) mention.box.remove();
    mention.box = null;
    mention.items = [];
  }

  function drawMention() {
    var root = mention.ed && mention.ed.closest('[class*="ChatTextField-module"][class*="__root"]');
    if (!root || !mention.items.length) {
      hideMention();
      return;
    }
    if (!mention.box || !mention.box.isConnected) {
      mention.box = document.createElement("div");
      mention.box.className = "kz-mention";
      mention.box.setAttribute("role", "listbox");
      mention.box.setAttribute("aria-label", txt("mention"));
      // mousedown, not click, so the input keeps its focus and caret
      mention.box.addEventListener("mousedown", function (e) {
        var b = e.target.closest && e.target.closest(".kz-m-item");
        e.preventDefault();
        if (b) acceptMention(Number(b.getAttribute("data-i")));
      });
      root.appendChild(mention.box);
    }
    mention.box.textContent = "";
    mention.items.forEach(function (nick, i) {
      var b = document.createElement("div");
      b.className = "kz-m-item" + (i === mention.pick ? " kz-on" : "");
      b.setAttribute("role", "option");
      b.setAttribute("aria-selected", i === mention.pick ? "true" : "false");
      b.setAttribute("data-i", String(i));
      nickParts(b, nick);
      mention.box.appendChild(b);
    });
  }

  function updateMention(ed) {
    var found = caretQuery(ed);
    if (!found) {
      hideMention();
      return;
    }
    if (Date.now() - people.at > 15000) {
      loadUsers(function () {
        if (mention.ed === ed) updateMention(ed);
      });
    }
    var all = knownNicks(),
      q = found.q;
    var starts = all.filter(function (n) {
      return n.toLowerCase().indexOf(q) === 0;
    });
    var inside = all.filter(function (n) {
      var i = n.toLowerCase().indexOf(q);
      return i > 0;
    });
    var items = starts.concat(inside).slice(0, MENTION_MAX);
    if (mention.items.join("\n") !== items.join("\n")) mention.pick = 0;
    mention.ed = ed;
    mention.items = items;
    drawMention();
  }

  function insertMention(ed, nick, range) {
    if (!ed || !nick) return;
    ed.focus();
    var sel = getSelection(),
      text = "@" + nick + " ";
    if (range) {
      sel.removeAllRanges();
      sel.addRange(range);
    } else {
      // From the member list. Append at the end, since after focus() the caret may sit at the start
      var end = document.createRange();
      end.selectNodeContents(ed);
      end.collapse(false);
      sel.removeAllRanges();
      sel.addRange(end);
      var r = sel.getRangeAt(0).cloneRange();
      r.setStart(ed, 0);
      if (r.toString() && !/\s$/.test(r.toString())) text = " " + text;
    }
    // insertText acts as typing. Owncast sees normal input and Ctrl+Z works
    document.execCommand("insertText", false, text);
  }

  function acceptMention(i) {
    var nick = mention.items[i],
      found = caretQuery(mention.ed);
    hideMention();
    if (nick && found) insertMention(mention.ed, nick, found.range);
  }

  function isEditor(t) {
    return t && t.id === "chat-input-content-editable";
  }

  document.addEventListener(
    "input",
    function (e) {
      if (isEditor(e.target)) updateMention(e.target);
    },
    true
  );

  document.addEventListener(
    "keyup",
    function (e) {
      if (isEditor(e.target) && /^(ArrowLeft|ArrowRight|Home|End)$/.test(e.key)) updateMention(e.target);
    },
    true
  );

  document.addEventListener(
    "click",
    function (e) {
      if (isEditor(e.target)) updateMention(e.target);
    },
    true
  );

  document.addEventListener(
    "focusout",
    function (e) {
      if (isEditor(e.target)) setTimeout(hideMention, 150);
    },
    true
  );

  // Capture on document runs before Owncast. Its Enter would send the message instead of picking the nick
  document.addEventListener(
    "keydown",
    function (e) {
      if (!mention.box || !isEditor(e.target) || e.isComposing) return;
      var n = mention.items.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        mention.pick = (mention.pick + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
        drawMention();
      } else if (e.key === "Enter" || e.key === "Tab") {
        if (e.shiftKey) return;
        acceptMention(mention.pick);
      } else if (e.key === "Escape") {
        hideMention();
      } else {
        return;
      }
      e.preventDefault();
      e.stopImmediatePropagation();
    },
    true
  );

  // =================================================================================================================
  // replies
  // =================================================================================================================
  var MARK = "\u2063",
    ZW = ["\u200b", "\u200c", "\u200d", "\u2060"];

  var MARK_RE = /\u2063([\u200b\u200c\u200d\u2060]+)\u2063/,
    MARKS_RE = /\u2063[\u200b\u200c\u200d\u2060]*\u2063/g;

  // Owncast id alphabet (shortid), 64 characters. Each id character takes three zero-width characters
  var ID_ABC = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-";

  var REPLY_ICON =
    '<svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true"><path fill="currentColor" ' +
    'd="M8 5V2L2 8l6 6v-3.1c4.2 0 7.1 1.3 9.2 4.3C16.4 10.9 13.4 6 8 5z"/></svg>';

  function encodeId(id) {
    var out = "";
    for (var i = 0; i < id.length; i++) {
      var n = ID_ABC.indexOf(id.charAt(i));
      if (n < 0) return "";
      out += ZW[n >> 4] + ZW[(n >> 2) & 3] + ZW[n & 3];
    }
    return MARK + out + MARK;
  }

  function decodeId(z) {
    if (z.length % 3) return null;
    var id = "";
    for (var i = 0; i < z.length; i += 3) {
      id += ID_ABC.charAt(ZW.indexOf(z[i]) * 16 + ZW.indexOf(z[i + 1]) * 4 + ZW.indexOf(z[i + 2]));
    }
    return id;
  }

  function textOf(el) {
    var out = "",
      w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT),
      n;
    while ((n = w.nextNode())) {
      if (n.nodeType === 3) out += n.data;
      else if (n.tagName === "IMG") out += n.getAttribute("alt") || "";
      else if (n.tagName === "BR" || n.tagName === "P") out += " ";
    }
    return out.replace(MARKS_RE, "").replace(/\s+/g, " ").trim();
  }

  // A bridge line "nick: text" from "Twitch" (no twin available) is attributed to its author
  function author(nick, text) {
    var m = nick === "Twitch" && /^([^\s:]+): (.*)$/.exec(text);
    return m ? { nick: m[1], text: m[2] } : { nick: nick, text: text };
  }

  // Accumulated, not replaced. /api/chat returns only the last ~50 lines, and replies quote older ones
  var HIST_MAX = 3000;
  var hist = { byId: {}, list: [], at: 0, asking: false, waiting: [], tries: 0 };

  function hhmm(ts) {
    var d = new Date(ts);
    return isNaN(d) ? "" : two(d.getHours()) + ":" + two(d.getMinutes());
  }

  // The Owncast page keeps the viewer's chat token in localStorage
  function chatToken() {
    try {
      return localStorage.getItem("accessToken");
    } catch (e) {
      return null; // private mode
    }
  }

  function loadHistory(done) {
    if (done) hist.waiting.push(done);
    if (hist.asking) return;
    var token = chatToken();
    var finish = function () {
      hist.asking = false;
      hist.at = Date.now();
      var w = hist.waiting;
      hist.waiting = [];
      w.forEach(function (f) {
        f();
      });
    };
    // Owncast issues a new viewer's token ~1 s after load. Wait for it instead of recording an empty history
    if (!token) {
      if (hist.tries++ < 30) {
        setTimeout(function () {
          if (!hist.asking) loadHistory();
        }, 1000);
      } else {
        finish();
      }
      return;
    }
    hist.asking = true;
    fetch("/api/chat?accessToken=" + encodeURIComponent(token), { cache: "no-store" })
      .then(function (r) {
        return r.ok ? r.json() : [];
      })
      .then(function (list) {
        (Array.isArray(list) ? list : []).forEach(function (m) {
          if (m.type !== "CHAT" || !m.id || !m.user) return;
          var doc = new DOMParser().parseFromString(m.body || "", "text/html");
          var e = author((m.user.displayName || "").replace(TWIN, ""), textOf(doc.body));
          e.time = hhmm(m.timestamp);
          e.uid = m.user.id;
          e.raw = m.user.displayName || "";
          var old = has.call(hist.byId, m.id) ? hist.byId[m.id] : null;
          if (old) {
            old.nick = e.nick;
            old.text = e.text;
            old.time = e.time;
            old.uid = e.uid;
            old.raw = e.raw;
            return;
          }
          e.id = m.id;
          hist.byId[m.id] = e;
          hist.list.push(e);
        });
        if (hist.list.length > HIST_MAX) {
          hist.list.splice(0, hist.list.length - HIST_MAX).forEach(function (e) {
            delete hist.byId[e.id];
          });
        }
      })
      .catch(function () {
        /* retried on the next call */
      })
      .then(finish, finish);
  }

  function rowInfo(row) {
    var nameEl = row.querySelector('[class*="__userName"]'),
      msg = row.querySelector('[class*="__message"]');
    if (!nameEl || !msg) return null;
    var info = author(nameEl.textContent.replace(TWIN, "").trim(), textOf(msg));
    info.time = rowTime(row);
    return info;
  }

  function sameLine(a, b) {
    return a.text === b.text && a.nick.toLowerCase() === b.nick.toLowerCase();
  }

  // Counts identical lines (author, text, minute) below this one. "+" is often posted several times in a row
  function twinsBelow(row, info) {
    var rows = document.querySelectorAll(ROW_SEL),
      after = false,
      k = 0;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] === row) {
        after = true;
        continue;
      }
      if (!after) continue;
      var o = rowInfo(rows[i]);
      if (o && sameLine(o, info) && o.time === info.time) k++;
    }
    return k;
  }

  // A feed row's entry has the same author, text and minute and is the k-th from the end.
  // Failing that, the newest entry with the same text
  function findInHistory(info, k) {
    var exact = [],
      loose = null;
    for (var i = hist.list.length - 1; i >= 0; i--) {
      var e = hist.list[i];
      if (!sameLine(e, info)) continue;
      if (!loose) loose = e;
      if (info.time && e.time === info.time) exact.push(e);
    }
    return exact[Math.min(k || 0, exact.length - 1)] || loose;
  }

  function plaque(el, nick, text) {
    el.textContent = "";
    var icon = document.createElement("span");
    icon.className = "kz-r-icon";
    icon.innerHTML = REPLY_ICON;
    var line = document.createElement("span"),
      who = document.createElement("b");
    line.className = "kz-r-line";
    who.textContent = "@" + nick;
    line.appendChild(document.createTextNode(txt("replyTo")));
    line.appendChild(who);
    if (text) line.appendChild(document.createTextNode(": " + text));
    el.appendChild(icon);
    el.appendChild(line);
  }

  function replyRows(root) {
    within(root, ROW_SEL).forEach(function (row) {
      if (!row.querySelector(":scope > .kz-reply-btn")) {
        // A new row refetches history, at most every 10 s, while it is still among the last ~50
        if (Date.now() - hist.at > 10000) loadHistory();
        var b = document.createElement("button");
        b.type = "button";
        b.className = "kz-reply-btn";
        b.title = txt("reply");
        b.setAttribute("aria-label", txt("reply"));
        b.innerHTML = REPLY_ICON;
        row.appendChild(b);
      }
      var msg = row.querySelector('[class*="__message"]'),
        old = row.querySelector(":scope > .kz-reply-to");
      var m = msg && MARK_RE.exec(msg.textContent);
      if (!m) {
        if (old) old.remove();
        return;
      }
      var id = decodeId(m[1]),
        e = id && has.call(hist.byId, id) ? hist.byId[id] : null;
      var key = id + "|" + (e ? "1" : "0");
      if (old && old.getAttribute("data-key") === key) return;
      // Source line missing from history (page open for long). Refetch, at most every 3 s
      if (!e && Date.now() - hist.at > 3000) {
        loadHistory(function () {
          replyRows(document.body);
        });
      }
      var el = old || document.createElement("div");
      el.className = "kz-reply-to";
      el.setAttribute("data-key", key);
      var lead = /^@([^\s\u2063]+)\u2063/.exec(msg.textContent.trim());
      var nick = "…";
      if (e) nick = e.nick;
      else if (lead) nick = lead[1];
      plaque(el, nick, e ? e.text : "");
      if (!old) row.insertBefore(el, row.firstChild);
    });
  }

  var replying = null;
  // With a mark, the prefix is searched in the whole input. It may start with an emoji or a line break
  var PREFIX_RE = /@[^\s\u2063]*\u2063[\u200b\u200c\u200d\u2060]*\u2063 ?/;

  function prefixRange(ed) {
    var r = document.createRange(),
      w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT),
      n,
      m;
    while ((n = w.nextNode())) {
      if ((m = PREFIX_RE.exec(n.data))) {
        r.setStart(n, m.index);
        r.setEnd(n, m.index + m[0].length);
        return r;
      }
    }
    var first = ed.firstChild,
      plain = replying && "@" + replying.nick;
    if (plain && first && first.nodeType === 3 && first.data.indexOf(plain) === 0) {
      r.setStart(first, 0);
      r.setEnd(first, Math.min(first.data.length, plain.length + (first.data.charAt(plain.length) === " " ? 1 : 0)));
      return r;
    }
    r.setStart(ed, 0);
    r.collapse(true);
    return r;
  }

  // The bar is a row above the input, in the field's wrapper. Owncast lays out the field root's children
  // in a row, so a bar inside it would push the input past the chat's right edge
  function replyHost(ed) {
    var root = ed && ed.closest && ed.closest('[class*="ChatTextField-module"][class*="__root"]');
    return root && root.parentNode ? { host: root.parentNode, root: root } : null;
  }

  function drawReplyBar() {
    var at = replying && replyHost(replying.ed);
    document.querySelectorAll(".kz-replybar").forEach(function (bar) {
      if (!at || bar.parentNode !== at.host) bar.remove();
    });
    if (!replying) return;
    if (!at) {
      replying = null;
      return;
    }
    var bar = at.host.querySelector(":scope > .kz-replybar");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "kz-replybar";
      at.host.insertBefore(bar, at.root);
    }
    bar.textContent = "";
    var body = document.createElement("div");
    body.className = "kz-rb-body";
    plaque(body, replying.nick, replying.text);
    var close = document.createElement("button");
    close.type = "button";
    close.className = "kz-rb-close";
    close.title = txt("cancelReply");
    close.setAttribute("aria-label", txt("cancelReply"));
    close.textContent = "×";
    close.addEventListener("mousedown", function (e) {
      e.preventDefault();
      cancelReply();
    });
    bar.appendChild(body);
    bar.appendChild(close);
  }

  function startReply(row) {
    var info = rowInfo(row),
      chat = row.closest(CHAT_SEL);
    var ed = (chat || document).querySelector("#chat-input-content-editable");
    if (!info || !info.nick || !ed) return;
    var k = twinsBelow(row, info);
    var go = function () {
      var e = findInHistory(info, k);
      ed.focus();
      var sel = getSelection(),
        r = prefixRange(ed);
      // Line not in history. The reply goes out as a plain "@nick", and others see no quote bar
      replying = { nick: info.nick, text: info.text, mark: e ? encodeId(e.id) : "", ed: ed };
      sel.removeAllRanges();
      sel.addRange(r);
      document.execCommand("insertText", false, "@" + info.nick + replying.mark + " ");
      var end = document.createRange();
      end.selectNodeContents(ed);
      end.collapse(false);
      sel.removeAllRanges();
      sel.addRange(end);
      drawReplyBar();
    };
    if (findInHistory(info, k)) go();
    else loadHistory(go);
  }

  function cancelReply() {
    if (!replying) return;
    var ed = replying.ed,
      sel = getSelection(),
      r = prefixRange(ed);
    if (!r.collapsed) {
      ed.focus();
      sel.removeAllRanges();
      sel.addRange(r);
      document.execCommand("delete");
    }
    replying = null;
    drawReplyBar();
  }

  // The bar stays while the input holds the reply prefix. Owncast clears the input on send, which removes it too
  function checkReply() {
    if (!replying) return;
    var t = replying.ed.isConnected ? replying.ed.textContent : "";
    var still = replying.mark ? t.indexOf(replying.mark) >= 0 : t.indexOf("@" + replying.nick) === 0;
    if (!still) {
      replying = null;
      drawReplyBar();
    }
  }

  document.addEventListener(
    "input",
    function (e) {
      if (isEditor(e.target)) checkReply();
    },
    true
  );

  // Capture, like every own key handler: the editor may stop Escape before it bubbles up.
  // The «mentions» handler runs first and stops a key it uses, so Escape closes the nick list before the reply
  document.addEventListener(
    "keydown",
    function (e) {
      if (isEditor(e.target) && e.key === "Enter") setTimeout(checkReply, 120);
      if (isEditor(e.target) && e.key === "Escape" && replying && !mention.box) cancelReply();
    },
    true
  );

  document.addEventListener(
    "click",
    function (e) {
      var t = e.target,
        b = t.closest && t.closest(".kz-reply-btn");
      if (b) {
        e.preventDefault();
        e.stopPropagation();
        startReply(b.closest(ROW_SEL));
        return;
      }
      if (t.closest && t.closest('[class*="ChatTextField-module"][class*="__sendButton"]')) setTimeout(checkReply, 120);
      if (!matchMedia("(pointer: coarse)").matches) return;
      var row = t.closest && t.closest(ROW_SEL);
      document.querySelectorAll(ROW_SEL + ".kz-tap").forEach(function (r) {
        if (r !== row) r.classList.remove("kz-tap");
      });
      if (row && !t.closest("a, button")) row.classList.toggle("kz-tap");
    },
    true
  );

  // =================================================================================================================
  // moderation: delete and ban buttons on rows, unban in the "Bans" window
  // =================================================================================================================
  var DEL_ICON =
    '<svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true"><path fill="currentColor" ' +
    'd="M8 2h4l1 1.5h4V5H3V3.5h4L8 2zM4.5 6.5h11l-.9 10.6A1.5 1.5 0 0 1 13.1 18.5H6.9a1.5 1.5 0 0 1-1.5-1.4L4.5 6.5zm3 ' +
    '2v8h1.5v-8H7.5zm3.5 0v8h1.5v-8H11z"/></svg>';

  var BAN_ICON =
    '<svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true"><path fill="currentColor" ' +
    'd="M10 2a8 8 0 1 0 0 16 8 8 0 0 0 0-16zm0 2a6 6 0 0 1 4.75 9.66L6.34 5.25A5.97 5.97 0 0 1 10 4zM4 10c0-1.38.47-2.66 ' +
    '1.25-3.66l8.41 8.41A6 6 0 0 1 4 10z"/></svg>';

  var BANS_ICON = BAN_ICON.replace('width="1em" height="1em"', 'width="20" height="20"');

  function myName() {
    var me = document.querySelector('[class*="UserDropdown-module"][class*="__username"]');
    return me ? me.textContent.trim() : "";
  }

  function rawName(row) {
    var el = row.querySelector('[class*="__userName"]');
    return el ? el.textContent.trim() : "";
  }

  function rowButton(row, cls, label, icon) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.title = label;
    b.setAttribute("aria-label", label);
    b.innerHTML = icon;
    row.appendChild(b);
    return b;
  }

  // Owncast renders the hidden "…" menu only for moderators, so it gates the buttons. No ban on own rows
  // or the broadcaster's (the OBS dock shows no own nick). None on "Twitch" either. It is the gate's fallback
  // sender, and banning it cuts the bridge
  function modRows(root) {
    within(root, ROW_SEL).forEach(function (row) {
      if (!row.querySelector('[class*="__modMenuWrapper"]')) return;
      if (!row.querySelector(":scope > .kz-del-btn")) rowButton(row, "kz-del-btn", txt("deleteMessage"), DEL_ICON);
      var raw = rawName(row),
        nick = raw.replace(TWIN, ""),
        ban = row.querySelector(":scope > .kz-ban-btn");
      var streamer = badgesOf(nick).some(function (b) {
        return b.indexOf("broadcaster/") === 0;
      });
      var may = raw && raw !== "Twitch" && !streamer && nick.toLowerCase() !== myName().toLowerCase();
      if (may && !ban) rowButton(row, "kz-ban-btn", txt("banNick", { nick: nick }), BAN_ICON);
      if (!may && ban) ban.remove();
    });
  }

  function rowEntry(row, then) {
    var info = rowInfo(row);
    if (!info) {
      then(null);
      return;
    }
    var k = twinsBelow(row, info),
      e = findInHistory(info, k);
    if (e) {
      then(e);
    } else {
      loadHistory(function () {
        then(findInHistory(info, k));
      });
    }
  }

  // Owncast moderation API with the moderator's own token, as the "…" menu does
  function modPost(path, body) {
    var token = chatToken();
    if (!token) return Promise.reject(new Error("no token"));
    return fetch(path + "?accessToken=" + encodeURIComponent(token), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
    });
  }

  function failed(b) {
    b.classList.add("kz-fail");
    setTimeout(function () {
      b.classList.remove("kz-fail");
    }, 1500);
  }

  function deleteRow(row, b) {
    rowEntry(row, function (e) {
      var chat = row.closest(CHAT_SEL);
      if (!e) {
        failed(b);
        notice(chat, txt("deleteNotFound"));
        return;
      }
      modPost("/api/chat/messagevisibility", { idArray: [e.id], visible: false }).catch(function () {
        failed(b);
        notice(chat, txt("deleteFailed"));
      });
    });
  }

  // Ban needs a second press within 3 s. A banned viewer loses the video, so a misclick costs them the film
  var armed = null;

  function disarm() {
    if (!armed) return;
    clearTimeout(armed.timer);
    armed.b.classList.remove("kz-armed");
    armed.b.innerHTML = BAN_ICON;
    armed.b.title = armed.title;
    armed.b.setAttribute("aria-label", armed.title);
    armed = null;
  }

  // Feed rows lose "[TW]" (CSS draws the label), history keeps it. Compare without it
  function bareNick(n) {
    return (n || "").replace(TWIN, "").trim().toLowerCase();
  }

  function banRow(row, b) {
    if (!armed || armed.b !== b) {
      disarm();
      armed = { b: b, title: b.title, timer: setTimeout(disarm, 3000) };
      b.classList.add("kz-armed");
      b.innerHTML = BAN_ICON + '<span class="kz-ban-q">' + txt("banConfirm") + "</span>";
      b.title = txt("banAgain");
      b.setAttribute("aria-label", b.title);
      return;
    }
    disarm();
    var raw = rawName(row),
      nick = raw.replace(TWIN, ""),
      chat = row.closest(CHAT_SEL);
    rowEntry(row, function (e) {
      // The entry must belong to the row's user. A "Twitch" line with the same text is someone else
      if (!e || !e.uid || e.raw === "Twitch" || bareNick(e.raw) !== bareNick(raw)) {
        failed(b);
        notice(chat, txt("banNotFound", { nick: nick }));
        return;
      }
      modPost("/api/chat/users/setenabled", { userId: e.uid, enabled: false }).then(
        function () {
          notice(chat, txt("banned", { nick: nick }), true);
          setTimeout(function () {
            loadBans();
            refreshBans();
          }, 1500); // an open window waits on the same request
        },
        function () {
          failed(b);
          notice(chat, txt("banFailed", { nick: nick }));
        }
      );
    });
  }

  // The "Bans" window. ok tells whether the gate recognised the streamer (null until asked).
  // list holds the banned viewers
  var bans = { ok: null, list: null, failed: false, at: 0, asking: false, waiting: [] };

  function notice(chat, text, bansLink) {
    if (!chat) return;
    var old = chat.querySelector(":scope > .kz-note");
    if (old) {
      clearTimeout(old.timer);
      old.remove();
    }
    var el = document.createElement("div");
    el.className = "kz-note";
    el.setAttribute("role", "status");
    var t = document.createElement("span");
    t.textContent = text;
    el.appendChild(t);
    if (bansLink && bans.ok) {
      var go = document.createElement("button");
      go.type = "button";
      go.textContent = txt("bans");
      go.addEventListener("click", function () {
        el.remove();
        closePanels(chat);
        openBans(chat);
      });
      el.appendChild(go);
    }
    chat.appendChild(el);
    el.timer = setTimeout(function () {
      el.remove();
    }, 6000);
  }

  document.addEventListener(
    "click",
    function (e) {
      var b = e.target.closest && e.target.closest(".kz-del-btn, .kz-ban-btn");
      if (!b) return;
      e.preventDefault();
      e.stopPropagation();
      var row = b.closest(ROW_SEL);
      if (!row) return;
      if (b.classList.contains("kz-del-btn")) deleteRow(row, b);
      else banRow(row, b);
    },
    true
  );

  function loadBans(done) {
    if (done) bans.waiting.push(done);
    if (bans.asking) return;
    bans.asking = true;
    fetch("/_gate/bans", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (r.status === 401 || r.status === 403) {
          bans.ok = false;
          return null;
        }
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(function (d) {
        if (d) {
          bans.ok = true;
          bans.list = d.bans || [];
          bans.failed = false;
        }
      })
      .catch(function () {
        bans.failed = true;
      })
      .then(function () {
        bans.asking = false;
        bans.at = Date.now();
        var w = bans.waiting;
        bans.waiting = [];
        w.forEach(function (f) {
          f();
        });
      });
  }

  function refreshBans() {
    var open = document.querySelectorAll(".kz-bans");
    if (open.length) {
      loadBans(function () {
        open.forEach(function (p) {
          if (p.isConnected) renderBans(p);
        });
      });
    }
  }

  function banDate(at) {
    var d = new Date(at * 1000);
    return isNaN(d) || !at
      ? ""
      : two(d.getDate()) + "." + two(d.getMonth() + 1) + " " + two(d.getHours()) + ":" + two(d.getMinutes());
  }

  function renderBans(panel) {
    fitAboveInput(panel);
    var box = panel.querySelector(".kz-u-list"),
      count = panel.querySelector(".kz-u-count");
    box.textContent = "";
    if (bans.list === null) {
      count.textContent = "";
      box.innerHTML = '<p class="kz-u-note">' + (bans.failed ? txt("listFailed") : txt("loading")) + "</p>";
      return;
    }
    count.textContent = bans.list.length ? String(bans.list.length) : "";
    if (!bans.list.length) {
      box.innerHTML = '<p class="kz-u-note">' + txt("noBans") + "</p>";
      return;
    }
    bans.list.forEach(function (x) {
      var row = document.createElement("div");
      row.className = "kz-b-row";
      var who = document.createElement("div");
      who.className = "kz-b-who";
      var line = document.createElement("span");
      line.className = "kz-b-name";
      nickParts(line, x.name);
      var meta = document.createElement("span");
      meta.className = "kz-b-meta";
      meta.textContent =
        (x.twitch ? txt("twitchViewer") : txt("hallViewer")) + (banDate(x.at) ? " · " + banDate(x.at) : "");
      who.appendChild(line);
      who.appendChild(meta);
      var lift = document.createElement("button");
      lift.type = "button";
      lift.className = "kz-b-lift";
      lift.textContent = txt("unban");
      lift.setAttribute("data-key", x.key);
      lift.setAttribute("aria-label", txt("unbanNick", { nick: x.name }));
      row.appendChild(who);
      row.appendChild(lift);
      box.appendChild(row);
    });
  }

  function closePanels(chat) {
    chat.querySelectorAll(":scope > .kz-users, :scope > .kz-bans").forEach(function (p) {
      p.remove();
    });
  }

  function openBans(chat) {
    var panel = document.createElement("div");
    panel.className = "kz-bans";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", txt("bannedList"));
    panel.innerHTML =
      '<div class="kz-u-head"><span class="kz-u-title">' +
      txt("bans") +
      ' <span class="kz-u-count"></span></span>' +
      '<button type="button" class="kz-u-close" aria-label="' +
      txt("close") +
      '" title="' +
      txt("close") +
      '">×</button></div>' +
      '<div class="kz-u-list"></div>';
    chat.appendChild(panel);
    panel.querySelector(".kz-u-close").addEventListener("click", function () {
      panel.remove();
    });
    panel.addEventListener("keydown", function (e) {
      if (e.key === "Escape") panel.remove();
    });
    panel.querySelector(".kz-u-list").addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".kz-b-lift");
      if (!b || b.disabled) return;
      b.disabled = true;
      b.textContent = "…";
      fetch("/_gate/unban", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: b.getAttribute("data-key") })
      })
        .then(function (r) {
          if (!r.ok && r.status !== 404) throw new Error(r.status);
        })
        .then(
          function () {
            loadBans(function () {
              if (panel.isConnected) renderBans(panel);
            });
          },
          function () {
            b.disabled = false;
            b.textContent = txt("unbanFailed");
          }
        );
    });
    renderBans(panel);
    loadBans(function () {
      if (panel.isConnected) renderBans(panel);
    });
    panel.querySelector(".kz-u-close").focus();
  }

  function bansHead() {
    if (bans.ok !== false && !bans.asking && Date.now() - bans.at > 60000) loadBans();
    if (!bans.ok) return;
    var n = bans.list ? bans.list.length : 0;
    document.querySelectorAll(CHAT_SEL + " > .kz-chathead").forEach(function (head) {
      var have = head.querySelector(".kz-ch-bans");
      if (have) {
        var c = have.querySelector(".kz-ch-count");
        if (c.textContent !== (n ? String(n) : "")) c.textContent = n ? String(n) : "";
        return;
      }
      var b = document.createElement("button");
      b.type = "button";
      b.className = "kz-ch-bans";
      b.title = txt("bans");
      b.setAttribute("aria-label", txt("bans"));
      b.innerHTML = BANS_ICON + '<span class="kz-ch-count">' + (n ? n : "") + "</span>";
      head.appendChild(b);
      b.addEventListener("click", function () {
        var chat = head.parentNode,
          open = chat.querySelector(":scope > .kz-bans");
        closePanels(chat);
        if (!open) openBans(chat);
      });
    });
    var open = document.querySelectorAll(".kz-bans");
    open.forEach(fitAboveInput);
    if (open.length && Date.now() - bans.at > 10000) refreshBans();
  }

  // =================================================================================================================
  // reconnect: lost Twitch voice
  // =================================================================================================================
  // Viewers' Twitch tokens live only in the gate's memory and a gate restart loses them.
  // The code is already entered, so the login returns straight to chat
  function voiceBar() {
    document.querySelectorAll(CHAT_SEL).forEach(function (chat) {
      var bar = chat.querySelector(":scope > .kz-voicebar");
      if (voice !== false) {
        if (bar) bar.remove();
        return;
      }
      if (bar) return;
      var field = chat.querySelector(":scope > " + FIELD_SEL);
      if (!field) return;
      bar = document.createElement("div");
      bar.className = "kz-voicebar";
      bar.setAttribute("role", "status");
      bar.innerHTML = "<span>" + txt("voiceLost") + '</span><a class="kz-vb-go" href="#">' + txt("reconnect") + "</a>";
      bar.querySelector(".kz-vb-go").href =
        "/_gate/twitch?next=" + encodeURIComponent(location.pathname + location.search);
      chat.insertBefore(bar, field);
    });
  }

  // =================================================================================================================
  // start: the first requests and the timers that keep every section fresh
  // =================================================================================================================
  loadHistory(function () {
    replyRows(document.body);
  });

  pollStatus();
  pollLive();
  pollLook();

  if (document.body) startTranslating();
  else document.addEventListener("DOMContentLoaded", startTranslating);

  setInterval(function () {
    logoutButton();
    langButton();
    englishPage();
    labelSettings();
    theaterButton();
    mobileChat();
    bindChat();
    chatHead();
    bansHead();
    voiceBar();
    checkReply();
  }, 1000);

  setInterval(pollStatus, 15000);
  setInterval(pollLive, 30000);
  setInterval(pollLook, 30000);
  setInterval(render, 1000);
})();
