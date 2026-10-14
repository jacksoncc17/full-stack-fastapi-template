import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { createFileRoute, Link as RouterLink } from "@tanstack/react-router"
import {
  ChevronDown,
  ChevronRight,
  Download,
  ExternalLink,
  Loader2,
  RefreshCw,
  TriangleAlert,
} from "lucide-react"

import { JobsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import useCustomToast from "@/hooks/useCustomToast"

export const Route = createFileRoute("/_layout/jobs")({
  component: JobsDashboard,
  head: () => ({ meta: [{ title: "Jobs" }] }),
})

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-slate-500",
  claimed: "bg-blue-600",
  running: "bg-amber-600",
  succeeded: "bg-emerald-600",
  failed: "bg-red-600",
  canceled: "bg-zinc-500",
}

/**
 * Download an artifact.
 *
 * NOT a plain <a href>: the API authenticates with a Bearer header, and the token
 * is attached by the generated client's `auth` hook (see main.tsx). A browser
 * navigation sends no such header, so a bare link comes back
 * `{"detail":"Not authenticated"}`. Fetching through the client and saving the
 * blob keeps the token in one place and works for every artifact kind.
 */
function ArtifactLink({ jobId, artifact }: { jobId: string; artifact: any }) {
  const [busy, setBusy] = useState(false)
  const [href, setHref] = useState<string | null>(null)
  const { showErrorToast } = useCustomToast()

  // An artifact is bytes, so most of them are downloads -- but some results ARE a place
  // to go, and downloading a text file to read a URL out of it is a poor way to offer
  // one. A `text/uri-list` artifact says so in its mime type (the standard type for a
  // list of URIs; the extension that produces it is `.uri`), which is a property of the
  // artifact rather than of any one service, so this rule needs no knowledge of who
  // produced it. The kind ends in `_url` in the same spirit, for a worker whose
  // `mimetypes` database is older than the extension.
  const isUriList =
    artifact.mime_type === "text/uri-list" ||
    (typeof artifact.kind === "string" && artifact.kind.endsWith("_url"))

  useEffect(() => {
    if (!isUriList) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await JobsService.downloadArtifact({
          path: { job_id: jobId, artifact_id: artifact.id },
          // the same authenticated fetch as the download, read as text
          responseType: "text",
        })
        // The first URL anywhere in it: a uri-list has it on the first line, and a
        // plain text file is allowed to say a sentence first.
        const raw = typeof res.data === "string" ? res.data : await (res.data as Blob).text()
        const uri = raw.match(/https?:\/\/[^\s"'<>]+/)?.[0]
        if (!cancelled && uri) setHref(uri)
      } catch {
        // unreadable as text is still a file: fall through to the download button
      }
    })()
    return () => {
      cancelled = true
    }
  }, [artifact.id, isUriList, jobId])

  if (href) {
    return (
      <a
        className="text-primary flex items-center gap-1 text-xs hover:underline"
        href={href}
        rel="noreferrer"
        target="_blank"
        title={href}
      >
        <ExternalLink className="h-3 w-3" />
        {artifact.kind} · open
      </a>
    )
  }

  const download = async () => {
    setBusy(true)
    try {
      const res = await JobsService.downloadArtifact({
        path: { job_id: jobId, artifact_id: artifact.id },
        // the endpoint returns raw bytes; with the axios client, responseType
        // "blob" is what makes the body come back as a Blob rather than text
        responseType: "blob",
      })
      const blob = res.data as unknown as Blob
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = artifact.filename ?? `${artifact.kind}.bin`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch {
      showErrorToast("Could not download the artifact")
    } finally {
      setBusy(false)
    }
  }

  return (
    <button
      className="text-primary flex items-center gap-1 text-xs hover:underline disabled:opacity-50"
      disabled={busy}
      onClick={download}
      type="button"
    >
      {busy ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : (
        <Download className="h-3 w-3" />
      )}
      {artifact.kind} · {(artifact.bytes / 1e6).toFixed(1)} MB
    </button>
  )
}

/**
 * The job's progress log, which is what the events endpoint is for.
 *
 * `JobEvent`'s own docstring calls this "append-only progress log, which is what drives
 * the dashboard timeline" -- the endpoint and the generated client call were both there,
 * and no page used them. A worker's progress messages often say the useful things: which
 * step is running, what a measurement came out as, and where the result can be found, so
 * any URL in one is turned into a link. It is fetched only when a row is opened, so
 * polling the queue does not multiply by the number of rows.
 */
function JobTimeline({ jobId }: { jobId: string }) {
  const { data } = useQuery({
    queryFn: async () =>
      (await JobsService.readJobEvents({ path: { job_id: jobId } })).data as any[],
    queryKey: ["jobs", jobId, "events"],
    refetchInterval: 5000,
  })
  const events = data ?? []
  if (events.length === 0) {
    return <p className="text-muted-foreground text-xs">no progress messages yet</p>
  }
  return (
    <ol className="flex flex-col gap-1">
      {events.map((event: any) => (
        <li className="flex items-start gap-2 text-xs" key={event.id}>
          <span className="text-muted-foreground w-16 shrink-0 tabular-nums">
            {event.at ? new Date(event.at).toLocaleTimeString() : "--:--:--"}
          </span>
          <span
            className={event.level === "error" ? "text-destructive" : undefined}
          >
            {linkify(event.message)}
          </span>
          {typeof event.progress === "number" ? (
            <span className="text-muted-foreground shrink-0 tabular-nums">
              {event.progress}%
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  )
}

/** A message with its URLs turned into links, so "the page is at http://…" is usable. */
function linkify(message: string) {
  const parts = String(message).split(/(https?:\/\/[^\s]+)/g)
  return parts.map((part, index) =>
    index % 2 === 1 ? (
      <a
        className="text-primary underline underline-offset-2"
        href={part}
        key={`${part}-${index}`}
        rel="noreferrer"
        target="_blank"
      >
        {part}
      </a>
    ) : (
      part
    ),
  )
}

function StatusBadge({ status }: { status: string }) {
  return (
    <Badge className={`${STATUS_STYLES[status] ?? "bg-slate-500"} text-white`}>
      {status}
    </Badge>
  )
}

function jobsQueryOptions() {
  return {
    queryFn: async () =>
      (await JobsService.readJobs({ query: { skip: 0, limit: 100 } })).data,
    // a queue is only interesting while it moves, so poll while work is pending
    refetchInterval: 5000,
    queryKey: ["jobs"],
  }
}

function workersStatusQueryOptions() {
  return {
    queryFn: async () => (await JobsService.workersStatus()).data as any,
    refetchInterval: 10000,
    queryKey: ["jobs", "workers-status"],
  }
}

function summaryQueryOptions() {
  return {
    queryFn: async () => (await JobsService.jobsSummary()).data,
    refetchInterval: 5000,
    queryKey: ["jobs", "summary"],
  }
}

function JobsDashboard() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data, isLoading } = useQuery(jobsQueryOptions())
  const { data: summary } = useQuery(summaryQueryOptions())
  const { data: workers } = useQuery(workersStatusQueryOptions())

  const cancel = useMutation({
    mutationFn: (id: string) => JobsService.cancelJob({ path: { job_id: id } }),
    onSuccess: () => {
      showSuccessToast("Job canceled")
      queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: () => showErrorToast("Could not cancel the job"),
  })

  const counts = (summary?.counts ?? {}) as Record<string, number>
  const jobs = data?.data ?? []

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Render jobs</h1>
          <p className="text-muted-foreground text-sm">
            {data?.count ?? 0} total · refreshes every 5s
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={() => queryClient.invalidateQueries({ queryKey: ["jobs"] })}
          >
            <RefreshCw className="mr-2 h-4 w-4" /> Refresh
          </Button>
          <RouterLink to="/submit">
            <Button>New request</Button>
          </RouterLink>
        </div>
      </div>

      {workers?.stalled ? (
        <div className="border-destructive/40 bg-destructive/10 flex items-start gap-3 rounded-lg border p-4">
          <TriangleAlert className="text-destructive mt-0.5 h-5 w-5 shrink-0" />
          <div className="text-sm">
            <p className="font-medium">
              No worker is online, so these jobs will not run.
            </p>
            <p className="text-muted-foreground mt-1">
              {workers.pending_jobs} job
              {workers.pending_jobs === 1 ? "" : "s"} waiting
              {workers.workers_registered === 0
                ? " and no worker has ever been registered."
                : `; ${workers.workers_registered} registered but none has sent a heartbeat in ${workers.offline_after_minutes} minutes.`}{" "}
              Start one with <code>python3 worker.py --broker ... --key ...</code>
            </p>
          </div>
        </div>
      ) : (
        <div className="text-muted-foreground flex items-center gap-2 text-xs">
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              workers?.workers_online ? "bg-emerald-500" : "bg-zinc-400"
            }`}
          />
          {workers?.workers_online ?? 0} worker
          {workers?.workers_online === 1 ? "" : "s"} online
          {workers?.pending_jobs
            ? ` · ${workers.pending_jobs} pending`
            : ""}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {["queued", "claimed", "running", "succeeded", "failed", "canceled"].map(
          (s) => (
            <div key={s} className="rounded-lg border p-3">
              <div className="text-muted-foreground text-xs uppercase tracking-wide">
                {s}
              </div>
              <div className="text-2xl font-semibold tabular-nums">
                {counts[s] ?? 0}
              </div>
            </div>
          ),
        )}
      </div>

      {isLoading ? (
        <p className="text-muted-foreground">Loading…</p>
      ) : jobs.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <p className="font-medium">No jobs yet</p>
          <p className="text-muted-foreground text-sm">
            Submit an SGF to queue a render.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="p-3 text-left font-medium">Job</th>
                <th className="p-3 text-left font-medium">Status</th>
                <th className="p-3 text-left font-medium">Progress</th>
                <th className="p-3 text-left font-medium">Created</th>
                <th className="p-3 text-left font-medium">Artifacts</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  onCancel={() => cancel.mutate(job.id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function JobRow({ job, onCancel }: { job: any; onCancel: () => void }) {
  const done = ["succeeded", "failed", "canceled"].includes(job.status)
  const [open, setOpen] = useState(false)
  const { data: artifacts } = useQuery({
    // only fetch artifacts once there could be some
    enabled: job.status === "succeeded",
    queryFn: async () =>
      (await JobsService.readJobArtifacts({ path: { job_id: job.id } })).data,
    queryKey: ["jobs", job.id, "artifacts"],
  })

  return (
    <>
    <tr className="border-t">
      <td className="p-3">
        <div className="font-medium">{job.title || job.sgf_name}</div>
        <div className="text-muted-foreground font-mono text-xs">
          {job.id.slice(0, 8)} · {job.duration_s}s · {job.formats}
        </div>
        {job.error ? (
          <div className="text-destructive mt-1 max-w-md text-xs">
            {job.error.slice(0, 200)}
          </div>
        ) : null}
      </td>
      <td className="p-3">
        <StatusBadge status={job.status} />
      </td>
      <td className="p-3 w-40">
        <div className="bg-muted h-2 w-full overflow-hidden rounded">
          <div
            className="bg-primary h-full transition-all"
            style={{ width: `${job.progress ?? 0}%` }}
          />
        </div>
        <div className="text-muted-foreground mt-1 text-xs tabular-nums">
          {job.progress ?? 0}%
          {job.attempts > 1 ? ` · attempt ${job.attempts}` : ""}
        </div>
      </td>
      <td className="text-muted-foreground p-3 text-xs">
        {job.created_at ? new Date(job.created_at).toLocaleString() : "—"}
      </td>
      <td className="p-3">
        <div className="flex flex-col gap-1">
          {(artifacts ?? []).map((a: any) => (
            <ArtifactLink artifact={a} jobId={job.id} key={a.id} />
          ))}
          {job.status === "succeeded" && (artifacts ?? []).length === 0 ? (
            <span className="text-muted-foreground text-xs">none</span>
          ) : null}
        </div>
      </td>
      <td className="p-3 text-right">
        <div className="flex items-center justify-end gap-1">
          {!done ? (
            <Button variant="outline" size="sm" onClick={onCancel}>
              Cancel
            </Button>
          ) : null}
          <Button
            aria-expanded={open}
            size="sm"
            title="progress log"
            variant="ghost"
            onClick={() => setOpen((was) => !was)}
          >
            {open ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
          </Button>
        </div>
      </td>
    </tr>
    {open ? (
      <tr className="bg-muted/30 border-t">
        <td className="p-3" colSpan={6}>
          <JobTimeline jobId={job.id} />
        </td>
      </tr>
    ) : null}
    </>
  )
}
