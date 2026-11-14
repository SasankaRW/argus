// The recent jobs, fetched once per change and shared by every screen that needs them (the home cards, the map's
// worker boxes, the desktop's status bar and timeline), instead of each asking /jobs itself.
import { useEffect, useState } from "react";
import { api, ArgusEvent, Job } from "./api";

let cache: Job[] = [];
let asked = -1;
const subs = new Set<(j: Job[]) => void>();

// Jobs, newest first as the API sends them. `events` is the live event list: the jobs are fetched again when a job
// event arrives.
export function useJobs(events: ArgusEvent[]): Job[] {
  const [jobs, setJobs] = useState(cache);
  const last = events.filter((e) => /^(job|approval|ari)\./.test(e.kind)).map((e) => e.seq).pop() ?? 0;
  useEffect(() => { subs.add(setJobs); return () => { subs.delete(setJobs); }; }, []);
  useEffect(() => {
    if (asked === last) { setJobs(cache); return; }
    asked = last;
    api<Job[]>("/jobs?limit=200").then((j) => { cache = j; subs.forEach((f) => f(j)); }).catch(() => { asked = -1; });
  }, [last]);
  return jobs;
}

export const startOfDay = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };

// Jobs started in the last hour (the "jobs / hr" number) and the last hour in 12 five-minute buckets (the throughput line).
export function lastHour(jobs: Job[], now = Date.now() / 1000): { count: number; buckets: number[] } {
  const buckets = Array.from({ length: 12 }, () => 0);
  let count = 0;
  for (const j of jobs) {
    const age = now - j.created_at;
    if (age < 0 || age >= 3600) continue;
    count++;
    buckets[11 - Math.floor(age / 300)]++;
  }
  return { count, buckets };
}

// How long finished jobs took on average today (null when none finished), in seconds.
export function avgRun(jobs: Job[]): number | null {
  const since = startOfDay();
  const done = jobs.filter((j) => j.finished_at && j.created_at >= since && j.state === "succeeded");
  if (!done.length) return null;
  return done.reduce((a, j) => a + (j.finished_at! - j.created_at), 0) / done.length;
}
