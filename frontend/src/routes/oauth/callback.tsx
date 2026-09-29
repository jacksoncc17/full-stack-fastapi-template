import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState } from "react"

/**
 * Landing point for the Google OAuth callback.
 *
 * The backend redirects here with the JWT in the query string. The rest of the app
 * authenticates by reading `localStorage.access_token` and sending it as a Bearer
 * header (see hooks/useAuth.ts), so this route's only job is to move the token
 * from the URL into that key -- reusing the existing session mechanism rather than
 * inventing a second one.
 *
 * The token is removed from the URL immediately with `replaceState`, so it does
 * not linger in the address bar or in browser history.
 */
export const Route = createFileRoute("/oauth/callback")({
  component: OAuthCallback,
  head: () => ({ meta: [{ title: "Signing in…" }] }),
})

function OAuthCallback() {
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const token = params.get("access_token")
    const err = params.get("error")

    // strip the query string either way: it should not persist in history
    window.history.replaceState({}, "", window.location.pathname)

    if (err) {
      setError(err)
      return
    }
    if (!token) {
      setError("no_token")
      return
    }
    localStorage.setItem("access_token", token)
    navigate({ to: "/jobs" })
  }, [navigate])

  if (error) {
    const messages: Record<string, string> = {
      invalid_state:
        "The sign-in attempt expired or was tampered with. Please try again.",
      email_not_verified:
        "Google has not verified that email address, so it cannot be linked to an existing account.",
      no_code: "Google did not return an authorization code.",
      no_token: "The server did not return a session token.",
      inactive_user: "That account is inactive.",
    }
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
        <h1 className="text-lg font-semibold">Sign-in failed</h1>
        <p className="text-muted-foreground max-w-md">
          {messages[error] ?? `Google sign-in failed (${error}).`}
        </p>
        <a className="text-primary underline" href="/login">
          Back to sign in
        </a>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <p className="text-muted-foreground">Signing you in…</p>
    </div>
  )
}
