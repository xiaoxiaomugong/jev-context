export function showAuthError(code) {
  if (code === "AUTH_EXPIRED") {
    return "Please sign in again";
  }
  return "Request failed";
}
