import { Link } from "@tanstack/react-router"

import { useTheme } from "@/components/theme-provider"
import { cn } from "@/lib/utils"
import icon from "/assets/images/fastapi-icon.svg"
import iconLight from "/assets/images/fastapi-icon-light.svg"

/** What the app calls itself. Kept here rather than in the page title so the header, the
 *  collapsed sidebar and the accessible name cannot drift apart. */
const APP_NAME = "MuseServices"

interface LogoProps {
  variant?: "full" | "icon" | "responsive"
  className?: string
  asLink?: boolean
}

/**
 * The wordmark is *text* now, not the drawn `fastapi-logo*.svg`.
 *
 * A name that is a picture has to be redrawn whenever it changes, at whatever sizes the
 * layout happens to need, in both themes. As text it is one string, it inherits the theme's
 * colour and the page's type size, and it is read aloud correctly by a screen reader.
 *
 * The icon is still an asset (`fastapi-icon.svg` / `fastapi-icon-light.svg`): replace those
 * two files with your own mark and nothing here changes.
 */
export function Logo({
  variant = "full",
  className,
  asLink = true,
}: LogoProps) {
  const { resolvedTheme } = useTheme()
  const isDark = resolvedTheme === "dark"

  const iconLogo = isDark ? iconLight : icon

  const wordmark = (extra?: string) => (
    <span
      className={cn("text-base font-semibold tracking-tight", extra, className)}
    >
      {APP_NAME}
    </span>
  )

  const content =
    variant === "responsive" ? (
      <>
        {wordmark("group-data-[collapsible=icon]:hidden")}
        <img
          src={iconLogo}
          alt={APP_NAME}
          className={cn(
            "size-5 hidden group-data-[collapsible=icon]:block",
            className,
          )}
        />
      </>
    ) : variant === "full" ? (
      <span className={cn("flex items-center gap-2", className)}>
        <img src={iconLogo} alt="" className="size-5" aria-hidden="true" />
        {wordmark()}
      </span>
    ) : (
      <img src={iconLogo} alt={APP_NAME} className={cn("size-5", className)} />
    )

  if (!asLink) {
    return content
  }

  return <Link to="/">{content}</Link>
}
