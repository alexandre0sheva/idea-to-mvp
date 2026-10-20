import type { ReactNode } from "react";

export const metadata = { title: "{{name}}" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
