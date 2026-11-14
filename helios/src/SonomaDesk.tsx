import { ReactNode, useEffect, useState } from "react";
import { api, ArgusEvent, Status } from "./api";
import { Pane, Sparkline, statusLine, useHomeData } from "./Phone";
import { IconTile, Ms } from "./sonoma";
import type { IconName } from "./icons";
import type { Tone } from "./sonoma";

// The desktop's home in the Sonoma look (design/sonoma/boards/Main.dc.html): a calm summary header, the live map as a
// panel, and a column beside it (Waiting for you, Ari, Today). The data is the phone's (useHomeData, statusLine).

const WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
const count = (n: number) => (n < WORDS.length ? WORDS[n] : String(n));
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

// "One job running, three queued, nothing failed today."
function summaryLine(running: number, queued: number, failed: number): string {
  const parts = [running ? `${count(running)} ${running === 1 ? "job" : "jobs"} running` : "nothing running"];
  if (queued) parts.push(`${count(queued)} queued`);
  parts.push(failed ? `${count(failed)} failed today` : "nothing failed today");
  return cap(parts.join(", ")) + ".";
}

export function headline(status: Status | null, live: boolean, down: boolean): string {
  if (down) return "Can't reach Argus";
  if (!live || !status) return "Connecting to Argus";
  return status.status === "ok" ? "Everything is fine" : "Something needs a look";
}

function Stat({ icon, tone, label, children }: { icon: IconName; tone: Tone; label: string; children: ReactNode }) {
  return (
    <div className="dh-stat">
      <IconTile n={icon} tone={tone} />
      <div><div className="sn-lbl">{label}</div><div className="dh-num">{children}</div></div>
    </div>
  );
}

export function DeskHome({ status, events, live, down, waiting, onOpen, onInbox, onAsk, onAri, children }: {
  status: Status | null; events: ArgusEvent[]; live: boolean; down: boolean; waiting: number;
  onOpen: (view: string) => void; onInbox: () => void; onAsk: (text: string) => void; onAri: (mic: boolean) => void;
  children: ReactNode;  // the live map panel
}) {
  const d = useHomeData(events);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const i = window.setInterval(() => setNow(new Date()), 60000); return () => window.clearInterval(i); }, []);
  const j = status?.jobs ?? {};
  const running = (j.running ?? 0) + (j.leased ?? 0);
  const queued = (j.queued ?? 0) + (j.retry ?? 0);
  const workers = status?.workers ?? [];
  const hosts = new Set(workers.map((w) => w.host));
  const hostsOn = new Set(workers.filter((w) => w.state === "online").map((w) => w.host));
  const online = workers.some((w) => w.state === "online");
  const first = d.pending[0];
  const tone = statusLine(status, live, down).tone;
  const date = now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }).replace(",", "");
  const decide = async (id: string, answer: "approve" | "reject") => {
    setBusy(id);
    try { await api(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ answer, by: "helios" }) }); await d.reload(); }
    finally { setBusy(null); }
  };
  return (
    <div className="dhome">
      <section className="dh-sum" aria-label="Summary">
        <div>
          <div className="sn-eye">{date}</div>
          <h1 className="dh-h1">{headline(status, live, down)}<span className="ac">.</span></h1>
          <p className="dh-sub">{down || !status ? "Helios keeps trying and reconnects by itself." : summaryLine(running, queued, d.failed)}</p>
        </div>
        <div className="dh-stats">
          <Stat icon="play_arrow" tone="green" label="Running">{running}</Stat>
          <Stat icon="hourglass_top" tone="amber" label="Waiting for you">{waiting}</Stat>
          <Stat icon="devices" tone={tone === "coral" ? "coral" : "blue"} label="Machines">{hostsOn.size} of {hosts.size}</Stat>
        </div>
      </section>

      {children}

      <div className="dh-side">
        {first && (
          <Pane label="Waiting for you" className="sn-waiting" meta={<button type="button" className="sn-link" onClick={onInbox}>{d.pending.length > 1 ? `All ${d.pending.length}` : "Open"}</button>}>
            <div className="sn-row">
              <IconTile n="folder_open" tone="amber" />
              <div className="sn-row-t"><b>{first.title}</b><span>{first.plugin}{first.payload.summary[0] ? ` · ${first.payload.summary[0]}` : ""}</span></div>
            </div>
            <div className="sn-acts">
              <button type="button" className="sn-btn primary" disabled={busy === first.id} onClick={() => decide(first.id, "approve")}>
                <Ms n="check" />Approve
              </button>
              <button type="button" className="sn-btn quiet" disabled={busy === first.id} onClick={() => decide(first.id, "reject")}>Reject</button>
            </div>
          </Pane>
        )}

        <Pane label="Ari" icon="graphic_eq" tone="violet" className="dh-ari">
          <p className="sn-ari-last">{d.lastAri ?? "Hi, I'm Ari. Ask me anything, or tell me what to do."}</p>
          <span className="grow" />
          <form className="sn-ask" onSubmit={(e) => { e.preventDefault(); if (q.trim()) { onAsk(q.trim()); setQ(""); } }}>
            <span className="sn-field"><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Ari anything" aria-label="Ask Ari" enterKeyHint="send" /></span>
            <button type="button" className="sn-iconbtn accent breathe" aria-label="Talk to Ari" onClick={() => onAri(true)}><Ms n="mic" fill /></button>
          </form>
        </Pane>

        <section className="sn-pane dh-today" aria-label="Today">
          <span className="sn-lbl">Today</span>
          <Sparkline data={d.hours} />
          <span className="grow" />
          <button type="button" className="sn-pill t-blue" onClick={() => onOpen("runs")}>{d.today} {d.today === 1 ? "job" : "jobs"} ran</button>
          {d.failed > 0 && <button type="button" className="sn-pill t-coral" onClick={() => onOpen("runs")}>{d.failed} failed</button>}
          <button type="button" className={`sn-pill ${online ? "t-green" : "t-coral"}`} onClick={() => onOpen("power")}>PC {online ? "online" : "offline"}</button>
        </section>
      </div>
    </div>
  );
}
