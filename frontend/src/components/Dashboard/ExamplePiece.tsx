import { useQuery } from "@tanstack/react-query"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

/**
 * A link to the example piece, on the dashboard, for everybody who signs in.
 *
 * The note-seq page lives under this origin (`/note-seq`), which is the whole reason it
 * shares the broker's login: the token this browser already holds is the token it expects,
 * so reading the piece list from here is a same-origin fetch with no CORS and no second
 * login. Pieces marked `"public": true` are the examples -- visible to every account, and
 * the only ones worth showing to someone who has not analysed anything yet.
 *
 * It renders nothing when there is no example, or when note-seq is not deployed behind this
 * origin at all. A dashboard that breaks on an optional service is worse than one without
 * the card.
 */
const NOTE_SEQ = import.meta.env.VITE_NOTE_SEQ_BASE ?? "/note-seq"

type Piece = {
  slug: string
  title: string
  composer?: string | null
  bars?: number
  public?: boolean
}

async function fetchExamples(): Promise<Piece[]> {
  const token = localStorage.getItem("access_token")
  if (!token) return []
  try {
    const response = await fetch(`${NOTE_SEQ}/api/v1/pieces`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!response.ok) return []
    const body = (await response.json()) as { pieces?: Piece[] }
    return (body.pieces ?? []).filter((piece) => piece.public)
  } catch {
    return []
  }
}

export function ExamplePieces() {
  const { data: examples = [] } = useQuery({
    queryKey: ["note-seq", "examples"],
    queryFn: fetchExamples,
    refetchInterval: 60_000,
    retry: false,
  })

  if (examples.length === 0) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Try it on a real piece</CardTitle>
        <CardDescription>
          Analysed by this service: every bar timed against the recording, and any passage
          playable on its own.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-1">
        {examples.map((piece) => (
          <a
            key={piece.slug}
            href={`${NOTE_SEQ}/pieces/${piece.slug}`}
            className="flex items-center justify-between gap-3 rounded-md px-2 py-2 text-sm hover:bg-muted"
          >
            <span className="min-w-0 flex-1 truncate">
              <span className="font-medium">{piece.title}</span>
              <span className="text-muted-foreground">
                {" "}
                {[piece.composer, piece.bars ? `${piece.bars} bars` : null]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </span>
            <span className="text-muted-foreground text-xs">open →</span>
          </a>
        ))}
      </CardContent>
    </Card>
  )
}
