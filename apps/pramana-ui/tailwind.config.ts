import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{js,ts,jsx,tsx,mdx}", "./components/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        obsidian: "#070b10",
        panel: "#0d141d",
        cyan: "#22d3ee",
        crimson: "#fb4568",
      },
    },
  },
  plugins: [],
};

export default config;
