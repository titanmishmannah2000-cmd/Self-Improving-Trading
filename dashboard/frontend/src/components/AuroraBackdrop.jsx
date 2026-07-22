import React from "react";

/**
 * Aurora Observatory backdrop — soft prismatic void + iridescent columns.
 * Ambient only; no rain, CRT, or terminal gimmicks.
 */
export default function AuroraBackdrop() {
  return (
    <div className="aurora-backdrop" aria-hidden="true">
      <div className="aurora-wash aurora-wash-a" />
      <div className="aurora-wash aurora-wash-b" />
      <div className="aurora-wash aurora-wash-c" />
      <div className="aurora-columns">
        {Array.from({ length: 14 }).map((_, i) => (
          <span
            key={i}
            className="aurora-col"
            style={{
              "--h": `${28 + ((i * 17) % 52)}%`,
              "--d": `${(i % 7) * 0.35}s`,
              "--hue": `${210 + (i % 5) * 18}`,
            }}
          />
        ))}
      </div>
      <div className="aurora-vignette" />
    </div>
  );
}
