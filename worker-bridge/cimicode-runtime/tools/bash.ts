/**
 * Custom bash tool — replaces the built-in bash tool (same-name custom tools
 * take precedence in opencode).
 *
 * cimicode pod 内置执行环境：命令经 SANDBOX_EXEC_URL（默认同容器回环
 * :4097 的 sandbox helper POST /exec）执行，全套协作工具链（taskflow /
 * agentteams-sync / mc / skills）就位——不再有独立的 sandbox pod。
 */
import { tool } from "@opencode-ai/plugin"

export default tool({
  description:
    "Execute shell commands in the agent execution environment (in-pod helper with all collaboration tools preinstalled)",
  args: {
    command: tool.schema.string().describe("The shell command to execute"),
    timeout: tool.schema
      .number()
      .optional()
      .describe("Timeout in seconds (max 900)"),
  },
  async execute(args) {
    const base = process.env.SANDBOX_EXEC_URL
    if (!base) {
      return "sandbox exec unavailable: SANDBOX_EXEC_URL is not configured"
    }
    const timeout = Math.min(Math.max(args.timeout ?? 600, 1), 900)
    try {
      const res = await fetch(`${base.replace(/\/$/, "")}/exec`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ command: args.command, timeout }),
        signal: AbortSignal.timeout((timeout + 15) * 1000),
      })
      if (!res.ok) {
        const text = await res.text().catch(() => "")
        return `sandbox exec failed: HTTP ${res.status} ${text}`.trim()
      }
      const data = (await res.json()) as {
        exitCode: number
        stdout: string
        stderr: string
      }
      let out = ""
      if (data.stdout) out += `${data.stdout}\n`
      if (data.stderr) out += `${data.stderr}\n`
      out += `[exit code: ${data.exitCode}]`
      return out
    } catch (err) {
      return `sandbox exec error: ${err instanceof Error ? err.message : String(err)}`
    }
  },
})
