// "Log out": the chat token leaves localStorage and goes to the gate, which revokes this viewer's Twitch token
(function () {
  var t = null;
  try {
    t = localStorage.getItem("accessToken");
    localStorage.removeItem("accessToken");
  } catch (e) {
    /* private mode: nothing was kept */
  }

  function go() {
    location.replace("/_gate/enter");
  }

  if (!t) {
    go();
    return;
  }
  fetch("/_gate/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: t })
  }).then(go, go);
})();
