import { useEffect, useMemo, useState } from "react";
import { api, getToken } from "./api";

type Rules = { label: string; file: string; text: string; default: string; custom: boolean; learned: Record<string, string> | null };

// A plugin's rules (YAML), readable and editable. Saved in Argus; the plugin uses them from its next job on.
export function RulesView({ plugin, onBack }: { plugin: string; onBack: () => void }) {
  const [r, setR] = useState<Rules | null>(null);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api<Rules>(`/plugins/${encodeURIComponent(plugin)}/rules`)
    .then((x) => { setR(x); setText(x.text); }).catch((e) => setErr(String(e)));
  useEffect(() => { load(); }, [plugin]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = r !== null && text !== r.text;
  const lines = useMemo(() => text.split("\n").length, [text]);

  const save = async () => {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const res = await fetch(`/plugins/${encodeURIComponent(plugin)}/rules`, {
        method: "PUT", body: JSON.stringify({ text }),
        headers: { "Content-Type": "application/json", ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}) },
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `HTTP ${res.status}`);
      await load();
      setMsg("Saved. Used from the next job on; if the rules don't make sense, that job stops and says what to fix.");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  const reset = async () => {
    if (!confirmReset()) return;
    setBusy(true); setErr(null);
    try { await api(`/plugins/${encodeURIComponent(plugin)}/rules`, { method: "DELETE" }); await load(); setMsg(`Back to ${r?.file}.`); }
    catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };

  const forget = async (name: string) => {
    if (!r?.learned) return;
    const next = { ...r.learned };
    delete next[name];
    await api(`/plugins/${encodeURIComponent(plugin)}/state/learned`, { method: "PUT", body: JSON.stringify({ value: next }) });
    await load();
  };

  const learned = Object.entries(r?.learned ?? {});
  return (
    <section className="panel rules" aria-label="Rules">
      <div className="ph">
        <button type="button" className="btn" onClick={onBack}>← Back</button>
        <span className="pt">{r?.label ?? "Rules"}</span>
        <span className="muted mono">{plugin}</span>
        {r && <span className={`pill ${r.custom ? "warn" : ""}`}>{r.custom ? "edited in Helios" : `default (${r.file})`}</span>}
        <div className="tools">
          <button type="button" className="btn" disabled={busy || !dirty} onClick={() => r && setText(r.text)}>Discard changes</button>
          <button type="button" className="btn" disabled={busy || !r?.custom} onClick={reset}>Reset to default</button>
          <button type="button" className="btn primary-btn" disabled={busy || !dirty} onClick={save}>Save</button>
        </div>
      </div>
      <div className="rules-body">
        <div className="editor mono">
          <div className="gutter" aria-hidden="true">{Array.from({ length: lines }, (_, i) => <div key={i}>{i + 1}</div>)}</div>
          <textarea spellCheck={false} rows={lines + 1} wrap="off" value={text} onChange={(e) => setText(e.target.value)} aria-label="Rules (YAML)"
            onKeyDown={(e) => {
              if (e.key === "Tab") { // indent with spaces, YAML style
                e.preventDefault();
                const t = e.currentTarget, a = t.selectionStart;
                setText(text.slice(0, a) + "  " + text.slice(t.selectionEnd));
                requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = a + 2; });
              } else if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); if (dirty) save(); }
            }} />
        </div>
        <aside className="rules-side">
          {err && <div className="tip bad">{err}</div>}
          {msg && <div className="tip ok">{msg}</div>}
          <div className="sect">How to edit</div>
          <div className="tip">
            YAML: <code>name: value</code>, lists start with <code>- </code>, indent with two spaces. Ctrl+S saves.
            Lines starting with <code>#</code> are notes.
          </div>
          {learned.length > 0 && (
            <>
              <div className="sect">Learned from “Wrong” ({learned.length})</div>
              <div className="tip">Your corrections, shown to the models as examples.</div>
              <div className="learned">
                {learned.map(([name, cat]) => (
                  <div key={name} className="chg">
                    <span className="mono">{name}</span>
                    <span className="muted">→ {cat}</span>
                    <span className="acts"><button type="button" className="btn" onClick={() => forget(name)}>Forget</button></span>
                  </div>
                ))}
              </div>
            </>
          )}
        </aside>
      </div>
    </section>
  );
}

function confirmReset(): boolean {
  // eslint-disable-next-line no-alert
  return window.confirm("Throw away your edits and use the default rules again?");
}
