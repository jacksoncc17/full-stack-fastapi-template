import { useMutation, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useRef, useState } from "react"

import { JobsService } from "@/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import useCustomToast from "@/hooks/useCustomToast"

export const Route = createFileRoute("/_layout/submit")({
  component: SubmitJob,
  head: () => ({ meta: [{ title: "New render request" }] }),
})

/**
 * Submit an SGF for rendering.
 *
 * The file goes to POST /jobs/upload as multipart; the parameters are the same
 * flags the pipeline takes, so what is chosen here is what the worker runs.
 */
function SubmitJob() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()
  const fileRef = useRef<HTMLInputElement>(null)

  const [file, setFile] = useState<File | null>(null)
  const [title, setTitle] = useState("")
  const [duration, setDuration] = useState(180)
  const [bpm, setBpm] = useState(69)
  const [musicalKey, setMusicalKey] = useState("D minor")
  const [timeSig, setTimeSig] = useState("3")
  const [seed, setSeed] = useState("")
  const [visits, setVisits] = useState(300)
  const [formats, setFormats] = useState("mp4,webp")

  const mutation = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose an .sgf file first")
      return (
        await JobsService.submitJobFile({
          // multipart: the file is the body, everything else is a query param
          // (the route declares them as Form/Query, not a JSON body)
          body: { file } as any,
          query: {
            title: title || undefined,
            duration_s: duration,
            bpm,
            musical_key: musicalKey || undefined,
            time_sig: timeSig || undefined,
            seed: seed || undefined,
            visits,
            formats,
          },
        })
      ).data
    },
    onSuccess: (job) => {
      showSuccessToast("Job queued")
      queryClient.invalidateQueries({ queryKey: ["jobs"] })
      navigate({ to: "/jobs" })
      void job
    },
    onError: (e: any) => {
      const detail =
        e?.body?.detail ?? e?.message ?? "Could not queue the job"
      showErrorToast(typeof detail === "string" ? detail : "Invalid request")
    },
  })

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-2xl font-semibold">New render request</h1>
      <p className="text-muted-foreground mt-1 text-sm">
        Upload an SGF. The render is timed to the generated soundtrack, so the
        duration below is also the length of the video.
      </p>

      <form
        className="mt-6 flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault()
          mutation.mutate()
        }}
      >
        <div className="grid gap-2">
          <Label htmlFor="sgf">
            SGF file <span className="text-destructive">*</span>
          </Label>
          <Input
            accept=".sgf,application/x-go-sgf,text/plain"
            id="sgf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            ref={fileRef}
            required
            type="file"
          />
          {file ? (
            <p className="text-muted-foreground text-xs">
              {file.name} · {(file.size / 1024).toFixed(1)} KB
            </p>
          ) : null}
        </div>

        <div className="grid gap-2">
          <Label htmlFor="title">Title</Label>
          <Input
            id="title"
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Optional, shown in the video header"
            value={title}
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="grid gap-2">
            <Label htmlFor="duration">Duration (seconds)</Label>
            <Input
              id="duration"
              max={480}
              min={10}
              onChange={(e) => setDuration(Number(e.target.value))}
              type="number"
              value={duration}
            />
            <p className="text-muted-foreground text-xs">
              Max 480s (the music model's limit)
            </p>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="visits">KataGo visits per position</Label>
            <Input
              id="visits"
              min={50}
              onChange={(e) => setVisits(Number(e.target.value))}
              type="number"
              value={visits}
            />
            <p className="text-muted-foreground text-xs">
              Higher is slower and more accurate
            </p>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-4">
          <div className="grid gap-2">
            <Label htmlFor="bpm">BPM</Label>
            <Input
              id="bpm"
              onChange={(e) => setBpm(Number(e.target.value))}
              type="number"
              value={bpm}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="key">Key</Label>
            <Input
              id="key"
              onChange={(e) => setMusicalKey(e.target.value)}
              value={musicalKey}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="timesig">Time signature</Label>
            <Select onValueChange={setTimeSig} value={timeSig}>
              <SelectTrigger id="timesig">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="3">3 (waltz)</SelectItem>
                <SelectItem value="4">4</SelectItem>
                <SelectItem value="6">6</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="grid gap-2">
            <Label htmlFor="seed">Seed</Label>
            <Input
              id="seed"
              onChange={(e) => setSeed(e.target.value)}
              placeholder="Blank = random"
              value={seed}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="formats">Output formats</Label>
            <Input
              id="formats"
              onChange={(e) => setFormats(e.target.value)}
              value={formats}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button
            onClick={() => navigate({ to: "/jobs" })}
            type="button"
            variant="outline"
          >
            Cancel
          </Button>
          <Button disabled={mutation.isPending || !file} type="submit">
            {mutation.isPending ? "Queueing…" : "Queue render"}
          </Button>
        </div>
      </form>
    </div>
  )
}
