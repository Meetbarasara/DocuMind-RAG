import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "KYC Compliance — Gap Analysis",
  description:
    "Upload your KYC policy, pick an RBI circular, and get a cited requirement-by-requirement gap table.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
