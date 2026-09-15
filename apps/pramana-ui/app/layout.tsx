import type { Metadata } from "next";
import "./globals.css";
import { HostedStatus } from "@/components/hosted-status";

export const metadata: Metadata = {
  manifest: "/manifest.webmanifest",
  appleWebApp: { capable: true, title: "Pramana" },
  title: "Pramana Executive Command Center",
  description:
    "Private paper trading workspace for portfolio evidence, market research and risk controls.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="dark">
      <body>
        <HostedStatus />
        {children}
      </body>
    </html>
  );
}
