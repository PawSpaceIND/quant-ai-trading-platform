import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pramana Executive Command Center",
  description:
    "Private paper trading workspace for portfolio evidence, market research and risk controls.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="dark">
      <body>{children}</body>
    </html>
  );
}
