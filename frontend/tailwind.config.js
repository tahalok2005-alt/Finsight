/** @type {import('tailwindcss').Config} */

// Design tokens: a light, corporate surface with a single accent.
//
// The scale reads the way it always did -- high numbers are surfaces, low
// numbers are text -- so no component had to change when the app moved from a
// dark panel to a white one. Every text tone clears 4.5:1 against the surface
// it sits on; 500 is the faintest permitted, and only for labels.
//
// Colour carries meaning or it is not used: one accent for action, three status
// hues, and otherwise grey. The figures stay the most colourful thing on screen.
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          // surfaces (never text)
          980: "#ffffff",
          950: "#f6f7f9",
          900: "#ffffff",
          850: "#f3f5f8",
          800: "#eceff4",
          // borders
          750: "#e4e8ee",
          700: "#d7dce4",
          600: "#9aa3b2",
          // text -- 500 is the faintest permitted, and only for labels
          500: "#6b7384",
          400: "#5c6475",
          300: "#49515f",
          200: "#333b48",
          100: "#1d242f",
          50: "#0f141c",
        },
        accent: {
          700: "#1e3a8a", 600: "#2563eb", 500: "#3b82f6",
          400: "#1d4ed8", 300: "#1e40af",
        },
        pass: { 600: "#047857", 500: "#059669", 400: "#0f7a52", 900: "#e7f6ef" },
        warn: { 600: "#b45309", 500: "#d97706", 400: "#8a5a00", 900: "#fdf3e2" },
        fail: { 600: "#dc2626", 500: "#e11d48", 400: "#b42318", 900: "#fdecec" },
        // lifecycle states get their own restrained hue so a queue reads at a
        // glance without becoming a colour chart
        run: { 500: "#0284c7", 400: "#0b6ea8", 900: "#e8f3fd" },
        hold: { 500: "#7c3aed", 400: "#5b3fc0", 900: "#f1ecfd" },
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Consolas", "SF Mono", "monospace"],
      },
      fontSize: {
        "3xs": ["0.6875rem", { lineHeight: "1rem", letterSpacing: "0.04em" }],
        "2xs": ["0.75rem", { lineHeight: "1.1rem", letterSpacing: "0.01em" }],
        xs: ["0.8125rem", { lineHeight: "1.2rem" }],
        sm: ["0.875rem", { lineHeight: "1.35rem" }],
        base: ["0.9375rem", { lineHeight: "1.5rem" }],
        lg: ["1.0625rem", { lineHeight: "1.6rem" }],
        xl: ["1.25rem", { lineHeight: "1.75rem" }],
        "2xl": ["1.5rem", { lineHeight: "2rem" }],
      },
      borderRadius: {
        DEFAULT: "4px", md: "6px", lg: "8px", xl: "10px",
      },
      spacing: { 4.5: "1.125rem", 13: "3.25rem" },
      boxShadow: {
        panel: "0 1px 2px rgba(16,24,40,0.05)",
        pop: "0 12px 32px -12px rgba(16,24,40,0.25)",
      },
    },
  },
  plugins: [],
};
