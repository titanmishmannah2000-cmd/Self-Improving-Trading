import React, { useEffect, useRef, useState } from "react";

/**
 * Classic digital-rain canvas backdrop. Sits behind the app; pointer-events none.
 * Respects prefers-reduced-motion by freezing on a static frame.
 * Follows data-theme: dark = green rain, light = Construct.
 */
export default function MatrixRain() {
  const canvasRef = useRef(null);
  const [construct, setConstruct] = useState(
    () => document.documentElement.getAttribute("data-theme") === "light"
  );

  useEffect(() => {
    const obs = new MutationObserver(() => {
      setConstruct(document.documentElement.getAttribute("data-theme") === "light");
    });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const glyphs = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789ABCDEF<>{}[]|/\\";
    const fontSize = 14;
    let columns = [];
    let raf = 0;
    let running = true;

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      const cols = Math.ceil(canvas.width / fontSize);
      columns = Array.from({ length: cols }, () => Math.random() * -40);
    };

    const draw = () => {
      if (!running) return;
      ctx.fillStyle = construct ? "rgba(232, 245, 233, 0.08)" : "rgba(0, 0, 0, 0.08)";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.font = `${fontSize}px "Share Tech Mono", "JetBrains Mono", monospace`;

      for (let i = 0; i < columns.length; i++) {
        const ch = glyphs[Math.floor(Math.random() * glyphs.length)];
        const x = i * fontSize;
        const y = columns[i] * fontSize;
        const head = Math.random() > 0.975;
        ctx.fillStyle = construct
          ? (head ? "#0A0A0A" : "rgba(0, 80, 20, 0.55)")
          : (head ? "#C8FFC8" : `rgba(0, ${140 + Math.floor(Math.random() * 80)}, 40, ${0.35 + Math.random() * 0.45})`);
        ctx.fillText(ch, x, y);
        if (y > canvas.height && Math.random() > 0.975) columns[i] = 0;
        else columns[i]++;
      }

      if (!reduceMotion) raf = requestAnimationFrame(draw);
    };

    resize();
    ctx.fillStyle = construct ? "#E8F5E9" : "#000000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    draw();
    if (reduceMotion) {
      for (let i = 0; i < 40; i++) draw();
      running = false;
    }

    window.addEventListener("resize", resize);
    return () => {
      running = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, [construct]);

  return (
    <canvas
      ref={canvasRef}
      className={`matrix-rain ${construct ? "matrix-rain-construct" : ""}`}
      aria-hidden="true"
    />
  );
}
