// Корень приложения: "/" на устройстве, "/fnadmin/" через сервер (docs/contracts/remote.md).
// Все адреса приложения (api/, ws, tiles/, display/, admin/) строятся от него, а не от "/".
// Копия этих функций — в web/admin/js/api.js (админка не зависит от модулей экрана).

/** "/admin/…" → "/", "/fnadmin/admin/…" → "/fnadmin/", "/apps/x/display/…" → "/apps/x/". */
export function appRoot(pathname) {
  const r = String(pathname || "/").replace(/\/(admin|display)(\/.*)?$/, "");
  return r.endsWith("/") ? r : r + "/";
}

export const ROOT = typeof location !== "undefined" ? appRoot(location.pathname) : "/";
/** Страница открыта не с самого устройства, а через сервер (туннель). */
export const REMOTE = ROOT !== "/";

/** "api/x" или "/api/x" → адрес от корня приложения; полные адреса (https://…) — как есть. */
export function appUrl(p, root = ROOT) {
  const s = String(p);
  if (/^[a-z][a-z0-9+.-]*:/i.test(s) || s.startsWith("//")) return s;
  return root + s.replace(/^\/+/, "");
}
