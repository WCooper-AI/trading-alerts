// Shown while the engine starts. The app calls showError() if it can't start.
(function () {
  var msg = document.getElementById("msg");
  var slow = setTimeout(function () {
    msg.textContent = "Starting the engine… The first launch can take a little longer.";
  }, 6000);
  window.showError = function (text) {
    clearTimeout(slow);
    document.getElementById("card").classList.add("failed");
    document.getElementById("bar").hidden = true;
    msg.textContent = text;
  };
})();
