import { useEffect, useMemo, useState } from "react";
import { api, getToken } from "./api";

type Target = { plugin: string; name: string; workflow: string; label: string; accepts: string[] };
type Item = { name: string; type: string; size: number; blob: Blob; preview?: string };

const CACHE = "helios-share";

function kb(n: number) { return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`; }
const looksLikeUrl = (s: string) => /^https?:\/\/\S+$/i.test(s.trim());

// What arrived from the phone's share menu (kept by sw.js), if anything.
async function readShared(): Promise<{ title: string; text: string; url: string; items: Item[] } | null> {
  if (!("caches" in window)) return null;
  const cache = await caches.open(CACHE);
  const m = await cache.match("/helios/__share/meta");
  if (!m) return null;
  const meta = await m.json() as { title: string; text: string; url: string; files: { key: string; name: string; type: string; size: number }[] };
  const items: Item[] = [];
  for (const f of meta.files) {
    const r = await cache.match(f.key);
    if (!r) continue;
    const blob = await r.blob();
    items.push({ name: f.name, type: f.type, size: f.size, blob, preview: f.type.startsWith("image/") ? URL.createObjectURL(blob) : undefined });
  }
  return { title: meta.title, text: meta.text, url: meta.url, items };
}

async function clearShared() {
  if ("caches" in window) await caches.delete(CACHE);
}

// Helios > Share: send photos, PDFs, links or text to a plugin. From the phone's share menu, or picked here.
export function ShareView({ onJob, onDone }: { onJob: (id: string) => void; onDone: () => void }) {
  const [targets, setTargets] = useState<Target[] | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [pick, setPick] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const [fromPhone, setFromPhone] = useState(false);

  useEffect(() => {
    api<Target[]>("/share/targets").then(setTargets).catch((e) => { setTargets([]); setErr(String(e)); });
    readShared().then((s) => {
      if (!s) return;
      setFromPhone(true);
      setItems(s.items);
      setTitle(s.title);
      // Android often puts the link in "text"
      if (!s.url && looksLikeUrl(s.text)) { setUrl(s.text.trim()); setText(""); } else { setUrl(s.url); setText(s.text); }
    }).catch(() => {});
  }, []);

  const kinds = useMemo(() => {
    const k = new Set<string>();
    for (const i of items) { k.add("file"); if (i.type.startsWith("image/")) k.add("image"); if (i.type === "application/pdf" || i.name.toLowerCase().endsWith(".pdf")) k.add("pdf"); }
    if (url.trim()) k.add("url");
    if (text.trim()) k.add("text");
    return k;
  }, [items, url, text]);
  const fits = (targets ?? []).filter((t) => t.accepts.some((a) => kinds.has(a)));
  const chosen = fits.find((t) => `${t.plugin}.${t.workflow}` === pick) ?? fits[0];

  const addFiles = (list: FileList | null) => {
    if (!list) return;
    setItems((old) => [...old, ...Array.from(list).map((f) => ({ name: f.name, type: f.type, size: f.size, blob: f as Blob,
      preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined }))]);
  };

  const send = async () => {
    if (!chosen) return;
    setBusy(true); setErr(null);
    try {
      const s = await api<{ id: string }>("/shares", { method: "POST", body: JSON.stringify({ title, text, url, note }) });
      for (const i of items) {
        const t = getToken();
        const r = await fetch(`/shares/${s.id}/files?name=${encodeURIComponent(i.name)}&type=${encodeURIComponent(i.type || "application/octet-stream")}`,
          { method: "PUT", body: i.blob, headers: t ? { Authorization: `Bearer ${t}` } : {} });
        if (!r.ok) throw new Error(`${i.name}: ${(await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`}`);
      }
      const j = await api<{ id: string }>(`/shares/${s.id}/send`, { method: "POST", body: JSON.stringify({ plugin: chosen.plugin, workflow: chosen.workflow }) });
      await clearShared();
      setSent(j.id);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  const reset = async () => {
    await clearShared();
    setItems([]); setTitle(""); setUrl(""); setText(""); setNote(""); setSent(null); setErr(null); setFromPhone(false);
  };

  if (sent) {
    return (
      <section className="panel share" aria-label="Share">
        <div className="share-done">
          <div className="big ok">Sent</div>
          <p>{chosen?.label} is on it.</p>
          <div className="appr-actions">
            <button type="button" className="btn primary-btn" onClick={() => onJob(sent)}>Follow the job</button>
            <button type="button" className="btn" onClick={reset}>Share something else</button>
            <button type="button" className="btn" onClick={onDone}>Done</button>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="panel share" aria-label="Share">
      <div className="ph"><span className="pt">Share</span>
        <span className="muted">{fromPhone ? "from your phone's share menu" : "send files, a link or text to Argus"}</span>
      </div>
      <div className="share-body">
        <div className="share-items">
          {items.map((i, n) => (
            <div key={n} className="sitem">
              {i.preview ? <img src={i.preview} alt="" /> : <div className="ficon mono">{(i.name.split(".").pop() ?? "").slice(0, 4).toUpperCase()}</div>}
              <div className="sinfo"><span className="mono">{i.name}</span><span className="muted">{kb(i.size)}</span></div>
              <button type="button" className="btn" onClick={() => setItems(items.filter((_, k) => k !== n))} aria-label={`Remove ${i.name}`}>×</button>
            </div>
          ))}
          <label className="btn addfiles">
            + Add files
            <input type="file" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
          </label>
        </div>
        <label className="fld"><span>Link</span><input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" /></label>
        <label className="fld"><span>Title</span><input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="optional" /></label>
        <label className="fld"><span>Text</span><textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="optional" /></label>
        <label className="fld"><span>Note</span><input value={note} onChange={(e) => setNote(e.target.value)} placeholder="optional, for you or the plugin" /></label>

        <div className="sect">Send to</div>
        {targets === null && <div className="tip">Loading…</div>}
        {targets && fits.length === 0 && (
          <div className="tip">{kinds.size ? "No plugin takes this kind of item yet." : "Add a file, a link or some text."}</div>
        )}
        <div className="targets">
          {fits.map((t) => {
            const id = `${t.plugin}.${t.workflow}`;
            return (
              <label key={id} className={`target${chosen && id === `${chosen.plugin}.${chosen.workflow}` ? " on" : ""}`}>
                <input type="radio" name="target" checked={!!chosen && id === `${chosen.plugin}.${chosen.workflow}`} onChange={() => setPick(id)} />
                <span><b>{t.label}</b><span className="muted"> · {t.name}</span></span>
              </label>
            );
          })}
        </div>
        {err && <div className="tip bad">{err}</div>}
        <div className="appr-actions">
          <button type="button" className="btn primary-btn" disabled={busy || !chosen} onClick={send}>{busy ? "Sending…" : "Send"}</button>
          {(fromPhone || items.length > 0) && <button type="button" className="btn" disabled={busy} onClick={reset}>Discard</button>}
        </div>
      </div>
    </section>
  );
}
