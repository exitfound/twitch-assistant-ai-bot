// After the entry: the Owncast chat token goes to localStorage for Owncast's web client, then the site opens.
// The gate puts both values into data attributes, so no inline script is needed under the page's CSP
(function () {
  var d = document.getElementById("kz-handoff").dataset;
  try {
    localStorage.setItem("accessToken", d.token);
  } catch (e) {
    /* private mode: the chat asks for a name instead */
  }
  location.replace(d.next);
})();
