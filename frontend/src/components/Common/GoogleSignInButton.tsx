import { useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { LoginService } from "@/client"
import { Button } from "@/components/ui/button"

/**
 * "Sign in with Google".
 *
 * The backend hands back an authorization URL rather than redirecting directly, so
 * the flow can be started from an XHR-free navigation while still letting the app
 * decide whether to show the button at all. When Google is not configured the
 * button renders nothing, so this is safe to drop into the login page
 * unconditionally.
 */
const GoogleSignInButton = () => {
  const [busy, setBusy] = useState(false)

  const { data } = useQuery({
    queryFn: async () => (await LoginService.googleStatus()).data as {
      enabled: boolean
      login_url: string | null
    },
    queryKey: ["google", "status"],
    staleTime: 5 * 60 * 1000,
  })

  if (!data?.enabled) return null

  const start = async () => {
    setBusy(true)
    try {
      const res = (await LoginService.googleLogin()).data as {
        authorization_url: string
        state: string
      }
      // the backend validates this on the way back; storing it is what makes the
      // single-use check meaningful
      sessionStorage.setItem("google_oauth_state", res.state)
      window.location.assign(res.authorization_url)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-3 pt-2">
      <div className="flex items-center gap-3">
        <span className="bg-border h-px flex-1" />
        <span className="text-muted-foreground text-xs uppercase">or</span>
        <span className="bg-border h-px flex-1" />
      </div>
      <Button
        disabled={busy}
        onClick={start}
        type="button"
        variant="outline"
        className="w-full"
      >
        <svg aria-hidden="true" className="mr-2 h-4 w-4" viewBox="0 0 24 24">
          <path
            d="M21.35 11.1H12v2.9h5.3c-.5 2.4-2.6 3.7-5.3 3.7a5.8 5.8 0 1 1 0-11.6c1.5 0 2.8.5 3.8 1.5l2.1-2.1A8.7 8.7 0 0 0 12 2.8a9.2 9.2 0 1 0 0 18.4c5.3 0 8.9-3.7 8.9-9 0-.4 0-.7-.1-1.1z"
            fill="currentColor"
          />
        </svg>
        {busy ? "Redirecting…" : "Continue with Google"}
      </Button>
    </div>
  )
}

export default GoogleSignInButton
