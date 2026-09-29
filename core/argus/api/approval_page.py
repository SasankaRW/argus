"""The small page behind the phone notification's Open button: one approval, Approve / Reject.

Public, but only with the approval's signed token (?t=). Everything shown is escaped.
"""

from __future__ import annotations

import html
import json
from typing import Any

from ..approvals import fmt_money, money

_CSS = """
:root{color-scheme:dark;--bg:#0b0c0f;--panel:#14161b;--line:#262a33;--tx:#e8eaf0;--tx2:#9aa1ad;--ok:#34d399;
--bad:#f87171;--amber:#f5a524}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,sans-serif;
padding:16px}main{max-width:520px;margin:0 auto}.card{background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:18px}.eyebrow{color:var(--tx2);font-size:12px;text-transform:uppercase;
letter-spacing:.06em}h1{font-size:20px;margin:4px 0 12px}table{width:100%;border-collapse:collapse}
td{padding:7px 0;border-top:1px solid var(--line);vertical-align:top}td:first-child{color:var(--tx2);
width:38%;padding-right:10px}input{width:100%;background:#0f1115;color:var(--tx);border:1px solid var(--line);
border-radius:8px;padding:7px 9px;font:inherit}.big{font-size:26px;font-weight:600;margin:6px 0 14px}
.row{display:flex;gap:10px;margin-top:18px}button{flex:1;border:0;border-radius:10px;padding:13px;
font:600 16px system-ui;cursor:pointer}.ok{background:var(--ok);color:#06281c}.no{background:#2a1416;
color:var(--bad);border:1px solid #5b2327}.state{margin-top:16px;padding:12px;border-radius:10px;
background:#0f1115;border:1px solid var(--line)}.muted{color:var(--tx2)}a{color:var(--amber)}
"""


_JS = """
document.querySelectorAll("button[data-a]").forEach(b => b.onclick = async () => {
  document.querySelectorAll("button").forEach(x => x.disabled = true);
  const fields = {};
  document.querySelectorAll("input[name]").forEach(i => { if (i.value !== i.defaultValue) fields[i.name] = i.value; });
  const r = await fetch(`/approvals/${ID}/decide?t=${T}&answer=${b.dataset.a}`, {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({fields, by: "phone page"})});
  const j = await r.json().catch(() => ({}));
  document.getElementById("out").innerHTML = r.ok
    ? `<div class='state'>Done: <b>${j.state}</b>. You can close this.</div>`
    : `<div class='state'>Could not save: ${j.detail || r.status}</div>`;
});
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def render(a: dict[str, Any], token: str) -> str:
    p = a["payload"]
    pending = a["state"] == "pending"
    body: list[str] = []
    if a["type"] == "batch":
        head = f"{p.get('count', 0)} items"
        body.append(f"<div class='big'>{_e(head)}{' · ' + _e(p['total']) if p.get('total') else ''}</div>")
        rows = []
        for it in (p.get("items") or [])[:200]:
            name = it.get("label") or it.get("name") or it.get("title") or ""
            amt = money(it.get("amount")) if "amount" in it else None
            rows.append(f"<tr><td>{_e(name)}</td><td>{_e(fmt_money(amt)) if amt is not None else ''}</td></tr>")
        body.append("<table>" + "".join(rows) + "</table>")
    elif a["type"] == "draft":
        body.append("".join(f"<p>{_e(x)}</p>" for x in p.get("summary") or []))
    else:
        rows = []
        for k, v in (p.get("fields") or {}).items():
            shown = p["amount"] if k == "amount" and p.get("amount") else v
            cell = (f"<input name='{_e(k)}' value='{_e(v)}' aria-label='{_e(k)}'>" if pending
                    and isinstance(v, (str, int, float)) else _e(shown))
            rows.append(f"<tr><td>{_e(k)}</td><td>{cell}</td></tr>")
        if p.get("amount"):
            body.insert(0, f"<div class='big'>{_e(p['amount'])}</div>")
        body.append("<table>" + "".join(rows) + "</table>")
    if p.get("link") and str(p["link"]).startswith(("http://", "https://")):
        body.append(f"<p><a href='{_e(p['link'])}' rel='noreferrer'>Open the file</a></p>")

    if pending:
        ok = "Approve all" if a["type"] == "batch" else "Approve"
        actions = (f"<div class='row'><button class='no' data-a='reject'>Reject</button>"
                   f"<button class='ok' data-a='approve'>{ok}</button></div><div id='out'></div>")
    else:
        who = f" by {_e(a['decided_by'])}" if a.get("decided_by") else ""
        actions = f"<div class='state'>Already <b>{_e(a['state'])}</b>{who}.</div>"

    script = ("<script>const T=" + json.dumps(token) + ", ID=" + json.dumps(a["id"]) + ";" + _JS + "</script>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>Approve · Argus</title>"
            f"<style>{_CSS}</style></head><body><main><div class='card'>"
            f"<div class='eyebrow'>{_e(a['plugin'])} · {_e(a['type'])}</div><h1>{_e(a['title'])}</h1>"
            + "".join(body) + actions + "</div><p class='muted'>Argus</p></main>"
            + (script if pending else "") + "</body></html>")


def invalid() -> str:
    return (f"<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<style>{_CSS}</style><main><div class='card'><h1>This link does not work</h1>"
            f"<p class='muted'>It may be mistyped. Decide in Helios instead.</p></div></main>")
