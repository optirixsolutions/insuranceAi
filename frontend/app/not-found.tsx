import Link from "next/link";

export default function NotFound() {
  return (
    <main className="grid min-h-dvh place-items-center px-6">
      <div className="text-center">
        <p className="font-mono text-sm text-muted-foreground">404</p>
        <h1 className="mt-2 text-xl font-semibold tracking-tight">Page not found</h1>
        <Link href="/" className="mt-6 inline-block text-sm font-medium text-primary hover:underline">
          Back to overview
        </Link>
      </div>
    </main>
  );
}
