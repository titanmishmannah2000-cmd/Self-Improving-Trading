export function timeAgo(iso) {
  if (iso === null || iso === undefined || iso === "") return "—";
  let ms;
  if (typeof iso === "number") {
    // Unix seconds vs milliseconds
    ms = iso < 1e12 ? iso * 1000 : iso;
  } else if (iso instanceof Date) {
    ms = iso.getTime();
  } else {
    ms = new Date(iso).getTime();
  }
  if (!Number.isFinite(ms)) return "—";
  const diff = Date.now() - ms;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export function pairId(pair) {
  return pair.replace(/[^a-z0-9]/gi, "");
}
