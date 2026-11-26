// RU | EN without a reload: Firefox paints an empty frame between two pages. The link still sets the cookie;
// the new card replaces the old one and keeps a typed code. Any surprise falls back to following the link
document.addEventListener("click", function (e) {
  var a = e.target.closest && e.target.closest(".lang a");
  if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  var old = document.querySelector(".code"),
    typed = old ? old.value : "";
  fetch(a.href, { credentials: "same-origin" })
    .then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    })
    .then(function (text) {
      var doc = new DOMParser().parseFromString(text, "text/html"),
        box = doc.querySelector(".box");
      if (!box) throw new Error("no card");
      document.documentElement.lang = doc.documentElement.lang;
      document.title = doc.title;
      document.querySelector(".box").replaceWith(box);
      var code = box.querySelector(".code");
      if (code) {
        code.value = typed;
        code.focus();
      }
    })
    .catch(function () {
      location.href = a.href;
    });
});
