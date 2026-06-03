// Progressive enhancement only. The site is fully usable without JavaScript:
// language navigation uses plain <a> links and each page hard-codes its own
// aria-current. This script just keeps the active-language highlight in sync
// if a page is reached through history/back-forward caching.
(function () {
  "use strict";

  var SUPPORTED = ["en", "ko", "ja", "zh-CN", "hi"];

  // Derive the current locale from the URL path segment (…/<locale>/…).
  function currentLocale() {
    var parts = window.location.pathname.split("/").filter(Boolean);
    for (var i = parts.length - 1; i >= 0; i--) {
      if (SUPPORTED.indexOf(parts[i]) !== -1) return parts[i];
    }
    return "en";
  }

  function markActive() {
    var here = currentLocale();
    var links = document.querySelectorAll("nav.lang a[data-locale]");
    links.forEach(function (a) {
      if (a.getAttribute("data-locale") === here) {
        a.setAttribute("aria-current", "true");
      } else {
        a.removeAttribute("aria-current");
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", markActive);
  } else {
    markActive();
  }
})();
