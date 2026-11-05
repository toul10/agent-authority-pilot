// Runs before the page paints, so a chosen theme never flashes the other one.
try {
  const theme = localStorage.getItem("authz-theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch (e) {}
