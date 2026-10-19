/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string
  // Where the note-seq page is served under this origin, for the dashboard's link to the
  // example piece. The deployment bakes `/note-seq` (service/Dockerfile's VITE_BASE), which
  // is the default the component uses when this is unset.
  readonly VITE_NOTE_SEQ_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
