/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#080b14", 900: "#0d1220", 850: "#111828",
          800: "#161e31", 700: "#1e2842", 600: "#2b3654",
          500: "#3d4a6b", 400: "#5b6a8c", 300: "#8593b1",
          200: "#b3bed4", 100: "#dbe2ee",
        },
        accent: {
          500: "#3b82f6", 400: "#60a5fa", 600: "#2563eb", 300: "#93c5fd",
        },
        pass: { 500: "#10b981", 400: "#34d399", 900: "#052e22" },
        warn: { 500: "#f59e0b", 400: "#fbbf24", 900: "#3a2506" },
        fail: { 500: "#ef4444", 400: "#f87171", 900: "#3f1212" },
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Consolas", "SF Mono", "monospace"],
      },
      fontSize: { "2xs": "0.6875rem" },
    },
  },
  plugins: [],
};
