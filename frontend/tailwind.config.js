/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class", '[data-theme="dark"]'],
  content: ["./index.html", "./widgets/**/*.html", "./panel/**/*.html", "./src/**/*.{ts,tsx}", "./widgets/**/*.{ts,tsx}", "./panel/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "rgb(from var(--color-border-secondary, hsl(var(--border))) r g b / <alpha-value>)",
        background: "rgb(from var(--color-background-primary, hsl(var(--background))) r g b / <alpha-value>)",
        foreground: "rgb(from var(--color-text-primary, hsl(var(--foreground))) r g b / <alpha-value>)",
        muted: { DEFAULT: "rgb(from var(--color-background-secondary, hsl(var(--muted))) r g b / <alpha-value>)", foreground: "rgb(from var(--color-text-secondary, hsl(var(--muted-foreground))) r g b / <alpha-value>)" },
        accent: { DEFAULT: "rgb(from var(--color-background-tertiary, hsl(var(--accent))) r g b / <alpha-value>)", foreground: "rgb(from var(--color-text-primary, hsl(var(--accent-foreground))) r g b / <alpha-value>)" },
        card: { DEFAULT: "rgb(from var(--color-background-primary, hsl(var(--card))) r g b / <alpha-value>)", foreground: "rgb(from var(--color-text-primary, hsl(var(--card-foreground))) r g b / <alpha-value>)" },
        primary: { DEFAULT: "rgb(from var(--color-background-inverse, hsl(var(--primary))) r g b / <alpha-value>)", foreground: "rgb(from var(--color-text-inverse, hsl(var(--primary-foreground))) r g b / <alpha-value>)" },
        secondary: { DEFAULT: "hsl(var(--secondary) / <alpha-value>)", foreground: "hsl(var(--secondary-foreground) / <alpha-value>)" },
        destructive: { DEFAULT: "rgb(from var(--color-background-danger, hsl(var(--destructive))) r g b / <alpha-value>)", foreground: "rgb(from var(--color-text-inverse, hsl(var(--destructive-foreground))) r g b / <alpha-value>)" },
        input: "rgb(from var(--color-border-primary, hsl(var(--input))) r g b / <alpha-value>)",
        ring: "rgb(from var(--color-ring-primary, hsl(var(--ring))) r g b / <alpha-value>)",
        popover: { DEFAULT: "hsl(var(--popover) / <alpha-value>)", foreground: "hsl(var(--popover-foreground) / <alpha-value>)" },
      },
      borderRadius: { lg: "var(--border-radius-lg, var(--radius))", md: "var(--border-radius-md, calc(var(--radius) - 2px))", sm: "var(--border-radius-sm, calc(var(--radius) - 4px))" },
    },
  },
  plugins: [],
};
