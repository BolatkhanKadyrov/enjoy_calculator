(function () {
  "use strict";

  const form = document.querySelector(".employee-form");
  if (!form) return;
  const role = form.querySelector('[name="role"]');
  const selection = form.querySelector("[data-employee-rights]");
  const notice = form.querySelector("[data-admin-rights]");
  if (!role || !selection || !notice) return;

  role.setAttribute("aria-controls", "employee-permission-selection employee-admin-rights");
  function updatePermissions() {
    const automatic = role.value === "admin";
    selection.hidden = automatic;
    notice.hidden = !automatic;
    selection.querySelectorAll('[name="permissions"]').forEach(function (field) {
      field.disabled = automatic;
    });
  }
  role.addEventListener("change", updatePermissions);
  window.addEventListener("pageshow", updatePermissions);
  updatePermissions();
})();
