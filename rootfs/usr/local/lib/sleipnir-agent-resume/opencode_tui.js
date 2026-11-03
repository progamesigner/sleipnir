// Installed by Sleipnir; container starts overwrite this file.
//
// OpenCode V2 runs server plugins in one shared background service, outside
// the terminal that shows a session. This TUI plugin runs inside each TUI
// process, so its parent chain and HC_TERMINAL_ID belong to that terminal. It
// reports the root session the TUI shows to `sleipnir-agent-resume hook
// opencode`, which labels the terminal for recovery after a restart.
import { spawn } from "node:child_process"

const HOOK = "/usr/local/bin/sleipnir-agent-resume"
const POLL_MS = 250
const RETRY_MS = [1000, 5000, 15000]

function setup(api) {
  if (!process.env.HC_TERMINAL_ID || process.env.SLEIPNIR_ENABLE_AGENT_RESUME !== "1") return

  let disposed = false
  let reported = ""
  let running = false
  let attempts = 0
  let retryAt = 0

  function selected() {
    let route
    try { route = api.ui.router.current() } catch { return undefined }
    if (!route || route.type !== "session" || !route.sessionID) return undefined
    // A subagent view belongs to its root conversation.
    let id = route.sessionID
    try { id = api.data.session.root(id) || id } catch {}
    let info
    try { info = api.data.session.get(id) } catch {}
    const cwd = info?.location?.directory
    // Wait for the session's folder: without it the resume would start elsewhere.
    return typeof cwd === "string" && cwd ? { id, cwd } : undefined
  }

  function report(session) {
    running = true
    let child
    try {
      child = spawn(HOOK, ["hook", "opencode"], { stdio: ["pipe", "ignore", "ignore"] })
    } catch {
      running = false
      return failed()
    }
    child.on("error", () => { running = false; failed() })
    child.on("exit", (code) => {
      running = false
      if (code === 0) {
        reported = session.id
        attempts = 0
      } else {
        failed()
      }
    })
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify({ hook_event_name: "SessionStart", session_id: session.id, cwd: session.cwd }))
  }

  function failed() {
    retryAt = Date.now() + RETRY_MS[Math.min(attempts, RETRY_MS.length - 1)]
    attempts += 1
  }

  function sync() {
    if (disposed || running || Date.now() < retryAt) return
    const session = selected()
    if (!session || session.id === reported) return
    report(session)
  }

  const timer = setInterval(sync, POLL_MS)
  timer.unref?.()
  sync()
  return () => {
    disposed = true
    clearInterval(timer)
  }
}

export default { id: "sleipnir-agent-resume", setup }
