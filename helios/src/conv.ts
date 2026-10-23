// Which Ari chat is current (kept tiny so the first paint does not load the whole Ari page).
export function newConv() { return Math.random().toString(36).slice(2, 12); }
// The chat you had open last: "Hey Ari" and questions from other screens continue it.
export function currentConv(): string {
  try { const c = localStorage.getItem("ari.conv"); if (c) return c; } catch { /* private */ }
  const c = newConv();
  try { localStorage.setItem("ari.conv", c); } catch { /* private */ }
  return c;
}
