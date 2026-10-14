import { useQuery } from "@tanstack/react-query"
import { useMemo, useState } from "react"

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

/**
 * A form built from a service's published parameter schema.
 *
 * Nothing here knows any service by name. The plugin declares its parameters once, in
 * its `param_schema`, and that dict is used for three things: the broker validates
 * submissions against it, the frontend renders this form from it, and the worker
 * publishes it at startup. A new service therefore needs no UI work at all.
 *
 * `format` is read as a rendering hint only -- it selects a widget and carries no
 * validation meaning. An unrecognised format falls back to a plain control rather
 * than breaking the form, so an older frontend still works against a newer service.
 */

type Schema = {
  type?: string | string[]
  properties?: Record<string, Prop>
  required?: string[]
}

type Prop = {
  type?: string | string[]
  title?: string
  description?: string
  default?: unknown
  enum?: unknown[]
  minimum?: number
  maximum?: number
  maxLength?: number
  minLength?: number
  format?: string
  // `accept` is a rendering hint the broker's schema validator carries (see
  // app/param_schema.py) and ServiceForm reads it below to offer the right file types.
  // It was missing from this type, so `tsc` failed on the line that uses it -- and the
  // build script is `tsc && vite build`, so the whole frontend build failed with it.
  accept?: string
}

/** textarea for long strings, so a question is not a one-line box */
const TEXTAREA_OVER = 200

function baseType(p: Prop): string {
  const t = p.type
  if (Array.isArray(t)) return t.find((x) => x !== "null") ?? "string"
  return t ?? "string"
}

/** Which widget to draw. `format` wins, then enum, then the base type. */
export function controlFor(p: Prop): string {
  const t = baseType(p)
  switch (p.format) {
    case "textarea":
      return "textarea"
    case "slider":
      return "slider"
    case "files":
      return "files"
    case "password":
      return "password"
    default:
      break
  }
  if (p.enum) return "select"
  if (t === "boolean") return "checkbox"
  if (t === "string" && (p.maxLength ?? 0) > TEXTAREA_OVER) return "textarea"
  if (t === "number" || t === "integer") return "number"
  return "text"
}

function initial(p: Prop, control: string): unknown {
  if (p.default !== undefined) return p.default
  if (control === "checkbox") return false
  if (control === "files") return null
  return ""
}

function Field({
  name, prop, value, onChange, required,
}: {
  name: string
  prop: Prop
  value: unknown
  onChange: (v: unknown) => void
  required: boolean
}) {
  const control = controlFor(prop)
  const label = prop.title ?? name
  const id = `param-${name}`

  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>
        {label}
        {required ? <span className="text-destructive"> *</span> : null}
      </Label>

      {control === "checkbox" ? (
        <div className="flex items-center gap-2">
          <input
            checked={Boolean(value)}
            id={id}
            onChange={(e) => onChange(e.target.checked)}
            type="checkbox"
          />
          <span className="text-muted-foreground text-xs">
            {prop.description ?? ""}
          </span>
        </div>
      ) : control === "select" ? (
        <Select onValueChange={onChange} value={String(value ?? "")}>
          <SelectTrigger id={id}>
            <SelectValue placeholder="Choose…" />
          </SelectTrigger>
          <SelectContent>
            {(prop.enum ?? []).map((opt) => (
              <SelectItem key={String(opt)} value={String(opt)}>
                {String(opt)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : control === "textarea" ? (
        <textarea
          className="border-input min-h-24 w-full rounded-md border bg-transparent px-3 py-2 text-sm"
          id={id}
          maxLength={prop.maxLength}
          onChange={(e) => onChange(e.target.value)}
          value={String(value ?? "")}
        />
      ) : control === "files" ? (
        <Input
          accept={prop.accept ?? ".sgf,.zip,text/plain"}
          id={id}
          multiple
          onChange={(e) => onChange(e.target.files)}
          type="file"
        />
      ) : control === "slider" ? (
        <div className="flex items-center gap-3">
          <input
            className="flex-1"
            id={id}
            max={prop.maximum}
            min={prop.minimum}
            onChange={(e) => onChange(Number(e.target.value))}
            type="range"
            value={Number(value ?? prop.minimum ?? 0)}
          />
          <span className="w-12 text-right text-sm tabular-nums">
            {String(value ?? prop.minimum ?? 0)}
          </span>
        </div>
      ) : (
        <Input
          id={id}
          max={prop.maximum}
          maxLength={prop.maxLength}
          min={prop.minimum}
          onChange={(e) =>
            onChange(
              control === "number"
                ? e.target.value === ""
                  ? ""
                  : Number(e.target.value)
                : e.target.value,
            )
          }
          type={control === "password" ? "password" : control === "number" ? "number" : "text"}
          value={String(value ?? "")}
        />
      )}

      {prop.description && control !== "checkbox" ? (
        <p className="text-muted-foreground text-xs">{prop.description}</p>
      ) : null}
    </div>
  )
}

export function ServiceForm({ service, onDone }: { service: string; onDone: () => void }) {
  const { showSuccessToast, showErrorToast } = useCustomToast()

  // the schema comes from the broker, which got it from the worker at registration
  const { data } = useQuery({
    queryFn: async () => (await JobsService.readServices()).data,
    queryKey: ["services"],
    staleTime: 30_000,
  })
  const svc = (data?.data ?? []).find((s: any) => s.name === service)
  const schema: Schema | undefined = svc?.param_schema ?? undefined

  const [values, setValues] = useState<Record<string, unknown>>({})
  const [busy, setBusy] = useState(false)

  // seed once the schema arrives; defaults are declared by the service, not here
  const seeded = useMemo(() => {
    if (!schema?.properties) return {}
    const out: Record<string, unknown> = {}
    for (const [name, prop] of Object.entries(schema.properties)) {
      out[name] = initial(prop, controlFor(prop))
    }
    return out
  }, [schema])

  const current = { ...seeded, ...values }

  if (!schema) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-sm">
        <p className="font-medium">This service has no published parameters.</p>
        <p className="text-muted-foreground mt-1">
          A service's form is built from the schema its worker publishes at startup,
          so this usually means no worker is running for {service}.
        </p>
      </div>
    )
  }

  const required = new Set(schema.required ?? [])

  // The service declares WHICH fields take files, via format: "files". The frontend
  // must not assume a name -- that would be per-service knowledge, which is exactly
  // what this whole design avoids. There can be more than one (intonationCheck takes
  // a reference clip, a clip to check and a score), so this collects all of them:
  // picking just the first silently dropped the other inputs.
  const fileFields = Object.entries(schema.properties ?? {})
    .filter(([, p]) => controlFor(p) === "files")
    .map(([name]) => name)

  const submit = async () => {
    setBusy(true)
    try {
      const params: Record<string, unknown> = { ...current }
      const files: File[] = []
      const fileFieldsByIndex: string[] = []
      for (const name of fileFields) {
        const picked = current[name] as FileList | null
        if (picked && picked.length) {
          for (const f of Array.from(picked)) {
            files.push(f)
            fileFieldsByIndex.push(name)
          }
        }
        // the files travel as multipart parts, not inside the params JSON
        delete params[name]
      }
      // the generated client applies the multipart serializer, so the body is the
      // fields themselves rather than hand-built FormData. file_fields is one name
      // per file, positionally, so the broker can put each part on its own field.
      await JobsService.submitJobGeneric({
        body: {
          service,
          params: JSON.stringify(params),
          file_field: fileFields[0] ?? "input",
          file_fields: fileFieldsByIndex.join(","),
          files,
        },
      })
      showSuccessToast("Job queued")
      onDone()
    } catch (e: any) {
      const detail = e?.body?.detail ?? e?.message ?? "Could not queue the job"
      showErrorToast(
        typeof detail === "string" ? detail : JSON.stringify(detail).slice(0, 200),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      {Object.entries(schema.properties ?? {}).map(([name, prop]) => (
        <Field
          key={name}
          name={name}
          onChange={(v) => setValues((prev) => ({ ...prev, [name]: v }))}
          prop={prop}
          required={required.has(name)}
          value={current[name]}
        />
      ))}
      {fileFields.length ? (
        <p className="text-muted-foreground text-xs">
          Files are sent with the job and fetched by the worker on claim.
        </p>
      ) : null}
      <div className="flex justify-end">
        <Button disabled={busy} type="submit">
          {busy ? "Queueing…" : "Queue job"}
        </Button>
      </div>
    </form>
  )
}

export default ServiceForm
