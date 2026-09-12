import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pramana Executive Command Center",
  description: "Read-only institutional command center for Pramana paper trading intelligence and proofs.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="dark">
      <body>{children}</body>
    </html>
  );
}
