import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Copy, Plus } from "lucide-react"
import { useState } from "react"

import { AdminService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import useCustomToast from "@/hooks/useCustomToast"

export const Route = createFileRoute("/_layout/workers")({
  component: WorkersPage,
  head: () => ({ meta: [{ title: "Workers" }] }),
})

function workersQueryOptions() {
  return {
    queryFn: async () => (await AdminService.readWorkersDetail()).data as any,
    refetchInterval: 10000,
    queryKey: ["workers", "detail"],
  }
}

function ago(seconds: number | null): string {
  if (seconds === null) return "never"
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function WorkersPage() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const [registerOpen, setRegisterOpen] = useState(false)
  const [name, setName] = useState("")
  const [issuedKey, setIssuedKey] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const { data, isLoading } = useQuery(workersQueryOptions())

  const register = useMutation({
    mutationFn: async (workerName: string) =>
      (await AdminService.registerWorker({
        body: { name: workerName },
      })).data as any,
    onSuccess: (res) => {
      // the key is only ever returned here, so keep it on screen until dismissed
      setIssuedKey(res.api_key)
      setName("")
      queryClient.invalidateQueries({ queryKey: ["workers"] })
    },
    onError: () => showErrorToast("Could not register the worker"),
  })

  const toggle = useMutation({
    mutationFn: async ({ id, active }: { id: string; active: boolean }) =>
      AdminService.setActive({ path: { worker_id: id }, query: { active } }),
    onSuccess: () => {
      showSuccessToast("Worker updated")
      queryClient.invalidateQueries({ queryKey: ["workers"] })
    },
    onError: () => showErrorToast("Could not update the worker"),
  })

  const workers = data?.data ?? []

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Workers</h1>
          <p className="text-muted-foreground text-sm">
            {workers.filter((w: any) => w.online).length} online ·{" "}
            {workers.length} registered · a worker offline for more than{" "}
            {data?.offline_after_minutes ?? 3} minutes is not counting on the queue
          </p>
        </div>
        <Button onClick={() => setRegisterOpen(true)}>
          <Plus className="mr-2 h-4 w-4" /> Register worker
        </Button>
      </div>

      {isLoading ? (
        <p className="text-muted-foreground">Loading…</p>
      ) : workers.length === 0 ? (
        <div className="rounded-lg border border-dashed p-10 text-center">
          <p className="font-medium">No workers registered</p>
          <p className="text-muted-foreground text-sm">
            Register one, then start it with worker.py to process the queue.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="p-3 text-left font-medium">Name</th>
                <th className="p-3 text-left font-medium">State</th>
                <th className="p-3 text-left font-medium">Last heartbeat</th>
                <th className="p-3 text-left font-medium">Capabilities</th>
                <th className="p-3 text-left font-medium">Status</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {workers.map((w: any) => (
                <tr className="border-t" key={w.id}>
                  <td className="p-3 font-medium">{w.name}</td>
                  <td className="p-3">
                    {!w.is_active ? (
                      <Badge className="bg-zinc-500 text-white">disabled</Badge>
                    ) : w.online ? (
                      <Badge className="bg-emerald-600 text-white">online</Badge>
                    ) : (
                      <Badge className="bg-amber-600 text-white">offline</Badge>
                    )}
                  </td>
                  <td className="text-muted-foreground p-3 text-xs">
                    {ago(w.seconds_since_heartbeat)}
                  </td>
                  <td className="text-muted-foreground p-3 font-mono text-xs">
                    {w.capabilities ? JSON.stringify(w.capabilities) : "—"}
                  </td>
                  <td className="text-muted-foreground p-3 font-mono text-xs">
                    {w.status ? JSON.stringify(w.status) : "—"}
                  </td>
                  <td className="p-3 text-right">
                    <Button
                      onClick={() =>
                        toggle.mutate({ id: w.id, active: !w.is_active })
                      }
                      size="sm"
                      variant="outline"
                    >
                      {w.is_active ? "Disable" : "Enable"}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-muted-foreground text-xs">
        Disabling a worker stops it claiming new jobs but does not abort the job it
        is already running — the lease has to expire before another worker may take
        it, which is what prevents two workers writing the same result.
      </p>

      {/* register dialog */}
      <Dialog open={registerOpen} onOpenChange={(o) => {
        setRegisterOpen(o)
        if (!o) { setIssuedKey(null); setCopied(false) }
      }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {issuedKey ? "Worker registered" : "Register a worker"}
            </DialogTitle>
            <DialogDescription>
              {issuedKey
                ? "Copy this key now — it is not stored in readable form and cannot be shown again."
                : "Give the worker a unique name. Re-using a name rotates its key."}
            </DialogDescription>
          </DialogHeader>

          {issuedKey ? (
            <div className="grid gap-3 py-2">
              <Label>API key</Label>
              <div className="flex gap-2">
                <Input readOnly value={issuedKey} className="font-mono text-xs" />
                <Button
                  onClick={() => {
                    navigator.clipboard.writeText(issuedKey)
                    setCopied(true)
                  }}
                  type="button"
                  variant="outline"
                >
                  <Copy className="h-4 w-4" />
                  {copied ? "Copied" : "Copy"}
                </Button>
              </div>
              <p className="text-muted-foreground text-xs">
                Start it with:
                <br />
                <code className="break-all">
                  python3 worker.py --broker &lt;broker-url&gt; --key {issuedKey.slice(0, 12)}… --name &lt;name&gt;
                </code>
              </p>
            </div>
          ) : (
            <div className="grid gap-2 py-2">
              <Label htmlFor="worker-name">Name</Label>
              <Input
                id="worker-name"
                onChange={(e) => setName(e.target.value)}
                placeholder="render-node-1"
                value={name}
              />
            </div>
          )}

          <DialogFooter>
            {issuedKey ? (
              <Button onClick={() => setRegisterOpen(false)}>Done</Button>
            ) : (
              <>
                <DialogClose asChild>
                  <Button variant="outline">Cancel</Button>
                </DialogClose>
                <Button
                  disabled={!name || register.isPending}
                  onClick={() => register.mutate(name)}
                >
                  {register.isPending ? "Registering…" : "Register"}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
