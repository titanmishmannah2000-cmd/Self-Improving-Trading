import React from "react";

export function Skeleton({ width = "100%", height = 14, ...style }) {
  return <div className="skeleton" style={{ width, height, borderRadius: 4, ...style }} />;
}

// ── Live tab skeletons ────────────────────────────────────────

export function SkeletonCard() {
  return (
    <div className="skeleton-card">
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
        <Skeleton width="50%" height={18} />
        <Skeleton width="30%" height={14} style={{borderRadius:10}} />
      </div>
      <Skeleton width="35%" height={16} style={{marginTop:10}} />
      <Skeleton width="45%" height={12} style={{marginTop:8}} />
      <div style={{display:"flex",gap:6,marginTop:8}}>
        {[1,2,3,4].map(i => <Skeleton key={i} width={24} height={8} style={{borderRadius:"50%"}} />)}
        {[1,2,3].map(i => <Skeleton key={`l${i}`} width={12} height={2} style={{alignSelf:"center"}} />)}
      </div>
      <Skeleton width="70%" height={10} style={{marginTop:8}} />
      <Skeleton width="72" height={22} style={{marginTop:6,borderRadius:2}} />
      <Skeleton width="60%" height={10} style={{marginTop:6}} />
    </div>
  );
}

export function SkeletonPortfolio() {
  return (
    <div className="pulse" style={{opacity:0.7}}>
      <div style={{display:"flex",alignItems:"center",gap:16}}>
        <div>
          <Skeleton width={80} height={12} />
          <Skeleton width={60} height={22} style={{marginTop:6}} />
          <Skeleton width={100} height={10} style={{marginTop:4}} />
        </div>
        <div style={{display:"flex",gap:20}}>
          <div><Skeleton width={30} height={18} /><Skeleton width={24} height={10} style={{marginTop:2}} /></div>
          <div><Skeleton width={30} height={18} /><Skeleton width={24} height={10} style={{marginTop:2}} /></div>
        </div>
      </div>
    </div>
  );
}

// ── Activity tab skeletons ────────────────────────────────────

export function SkeletonActivity({ rows = 5 }) {
  return (
    <div style={{padding:"12px 0"}}>
      <Skeleton width="60" height={14} style={{marginBottom:12}} />
      {Array.from({length:rows}).map((_,i) => (
        <div key={i} style={{display:"flex",alignItems:"center",gap:8,marginBottom:8}}>
          <Skeleton width={50} height={12} />
          <Skeleton width={10} height={10} style={{borderRadius:"50%"}} />
          <Skeleton width={`${55 + Math.random()*40}%`} height={12} />
        </div>
      ))}
    </div>
  );
}

// ── Reports tab skeletons ─────────────────────────────────────

export function SkeletonReportCard() {
  return (
    <div className="report-card" style={{opacity:0.6}}>
      <div style={{display:"flex",justifyContent:"space-between",marginBottom:12}}>
        <Skeleton width={100} height={16} />
        <Skeleton width={80} height={12} />
      </div>
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10}}>
        {[1,2,3,4].map(i => (
          <div key={i}><Skeleton width="60%" height={18} /><Skeleton width="40%" height={10} style={{marginTop:4}} /></div>
        ))}
      </div>
      <div style={{marginTop:12}}>
        {[1,2,3].map(i => (
          <div key={i} style={{display:"flex",gap:8,marginTop:6}}>
            <Skeleton width={60} height={12} />
            <Skeleton width={30} height={12} />
            <Skeleton width={50} height={12} />
            <Skeleton width={40} height={12} />
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Discovered tab skeletons ──────────────────────────────────

export function SkeletonDiscovered() {
  return (
    <div style={{padding:8}}>
      <Skeleton width="70%" height={16} style={{marginBottom:12}} />
      <Skeleton width="40%" height={12} style={{marginBottom:16}} />
      {[1,2].map(i => (
        <div key={i} style={{marginBottom:16,background:"var(--card-bg)",borderRadius:8,padding:12}}>
          <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:8}}>
            <Skeleton width={16} height={16} style={{borderRadius:"50%"}} />
            <Skeleton width={80} height={14} />
            <Skeleton width={60} height={14} />
          </div>
          <div style={{display:"flex",gap:12}}>
            <Skeleton width={80} height={30} /><Skeleton width={80} height={30} /><Skeleton width={80} height={30} />
          </div>
          <div style={{marginTop:8}}>
            <Skeleton width="100%" height={20} />
            <Skeleton width="100%" height={20} style={{marginTop:2}} />
            <Skeleton width="100%" height={20} style={{marginTop:2}} />
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Cortex tab skeletons ──────────────────────────────────────

export function SkeletonCortex() {
  return (
    <div style={{padding:8}}>
      {[1,2].map(i => (
        <div key={i} style={{marginBottom:16,background:"var(--card-bg)",borderRadius:8,padding:14}}>
          <Skeleton width="50%" height={16} style={{marginBottom:10}} />
          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:10}}>
            {[1,2,3,4,5,6].map(j => (
              <div key={j}><Skeleton width="60%" height={20} /><Skeleton width="40%" height={10} style={{marginTop:4}} /></div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Audit tab skeletons ───────────────────────────────────────

export function SkeletonAudit() {
  return (
    <div style={{padding:8}}>
      <div style={{display:"flex",justifyContent:"space-between",marginBottom:16}}>
        <div><Skeleton width={120} height={22} /><Skeleton width={200} height={12} style={{marginTop:4}} /></div>
        <Skeleton width={100} height={32} style={{borderRadius:6}} />
      </div>
      <div style={{display:"flex",gap:16,marginBottom:16}}>
        {[1,2,3,4,5].map(i => (
          <div key={i}><Skeleton width={40} height={24} /><Skeleton width={30} height={10} style={{marginTop:4}} /></div>
        ))}
      </div>
      {[1,2,3].map(i => (
        <div key={i} style={{marginBottom:8,background:"var(--card-bg)",borderRadius:8,padding:10}}>
          <div style={{display:"flex",alignItems:"center",gap:8}}>
            <Skeleton width={20} height={20} style={{borderRadius:4}} />
            <Skeleton width={40} height={14} />
            <Skeleton width="50%" height={14} />
            <Skeleton width={40} height={14} style={{marginLeft:"auto"}} />
            <Skeleton width={16} height={16} style={{borderRadius:"50%"}} />
          </div>
        </div>
      ))}
    </div>
  );
}
