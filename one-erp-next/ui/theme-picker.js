/**
 * ONE ERP NEXT independent presentation-only theme selector.
 * Neither access controls nor financial rules may be derived from browser state.
 */
export const ONE_THEMES = Object.freeze(["blue", "slate", "midnight"]);

export function applyOneTheme(theme, root = document.documentElement) {
  if (!ONE_THEMES.includes(theme)) {
    throw new TypeError("Unrecognized ONE ERP NEXT theme");
  }
  root.dataset.oneTheme = theme;
  return theme;
}

export function installOneThemePicker(select, root = document.documentElement) {
  if (!select || typeof select.addEventListener !== "function") {
    throw new TypeError("Theme picker requires a select element");
  }
  const initialTheme = ONE_THEMES.includes(select.value) ? select.value : "blue";
  select.value = applyOneTheme(initialTheme, root);
  const onChange = () => {
    if (!ONE_THEMES.includes(select.value)) {
      select.value = root.dataset.oneTheme || "blue";
      return;
    }
    applyOneTheme(select.value, root);
  };
  select.addEventListener("change", onChange);
  return () => select.removeEventListener("change", onChange);
}
