import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import { useState } from "react"

import { JobsService } from "@/client"
import ServiceForm from "@/components/Jobs/ServiceForm"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export const Route = createFileRoute("/_layout/submit")({
  component: SubmitJob,
  head: () => ({ meta: [{ title: "New job" }] }),
})

/**
 * Submit a job to any service.
 *
 * This page knows no service by name: it lists what the broker has been told about
 * and renders whichever form that service's schema describes, so adding a service
 * needs no change here.
 */
function SubmitJob() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string>("")

  const { data, isLoading } = useQuery({
    queryFn: async () => (await JobsService.readServices()).data,
    queryKey: ["services"],
  })
  const { data: workers } = useQuery({
    queryFn: async () => (await JobsService.workersStatus()).data as any,
    queryKey: ["jobs", "workers-status"],
    refetchInterval: 10_000,
  })

  const services = data?.data ?? []
  const active = selected || services[0]?.name || ""
  const current = services.find((s: any) => s.name === active)

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-2xl font-semibold">New job</h1>
      <p className="text-muted-foreground mt-1 text-sm">
        The form below is built from the selected service's own parameter schema, so
        each service describes itself.
      </p>

      {isLoading ? (
        <p className="text-muted-foreground mt-6">Loading services…</p>
      ) : services.length === 0 ? (
        <div className="mt-6 rounded-lg border border-dashed p-8 text-center">
          <p className="font-medium">No services available yet</p>
          <p className="text-muted-foreground mt-1 text-sm">
            A service appears once a worker running it has connected and published its
            parameters.
          </p>
        </div>
      ) : (
        <>
          <div className="mt-6 grid gap-2">
            <label className="text-sm font-medium" htmlFor="service">
              Service
            </label>
            <Select onValueChange={setSelected} value={active}>
              <SelectTrigger id="service">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {services.map((s: any) => (
                  // `title` is the service's own one-line description, which the worker
                  // publishes at startup (the first line of its plugin's docstring) and the
                  // broker stores in the contract. It belongs on the *item* rather than in
                  // its text: Radix mirrors an item's text into the closed trigger, so a
                  // second line here would also appear in the box after you pick it.
                  <SelectItem
                    key={s.name}
                    value={s.name}
                    title={s.description ?? undefined}
                  >
                    {s.title}
                    {s.param_schema ? "" : "  (no worker schema)"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {current ? (
              <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                <Badge variant="outline">{current.name}</Badge>
                {current.description ? <span>{current.description}</span> : null}
                {((current.artifact_kinds ?? []) as unknown[]).map((k, i) => (
                  <Badge key={`${String(k)}-${i}`} variant="secondary">
                    {String(k)}
                  </Badge>
                ))}
              </div>
            ) : null}
          </div>

          {workers && !workers.workers_online ? (
            <div className="border-destructive/40 bg-destructive/10 mt-4 rounded-lg border p-3 text-sm">
              No worker is online, so a submitted job will wait in the queue until one
              starts.
            </div>
          ) : null}

          <div className="mt-6">
            <ServiceForm
              key={active}
              onDone={() => {
                queryClient.invalidateQueries({ queryKey: ["jobs"] })
                navigate({ to: "/jobs" })
              }}
              service={active}
            />
          </div>
        </>
      )}
    </div>
  )
}
