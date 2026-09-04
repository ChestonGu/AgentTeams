"""Patch script for the agentteams-dashboard fork (run ON the 105 server).

Adds the OpenCode runtime option to the worker-create form. Three edits:

1. lib/agentteams-api.ts   — WorkerRuntime union gains 'opencode';
                              CreateWorkerRequest.runtime becomes optional.
2. worker-create-dialog.tsx — OpenCode SelectItem + bridge image prefill.
3. workers-section.tsx      — submit maps runtime 'opencode' to omitted so
                              the controller applies its configured default
                              (AGENTTEAMS_DEFAULT_WORKER_RUNTIME=opencode);
                              the cluster Worker CRD enum is never sent the
                              literal string.
"""

import io

BASE = "/home/extvdiadmin/dashboard-fork/"


def patch(path: str, old: str, new: str, count: int = 1) -> None:
    with io.open(path, encoding="utf-8") as f:
        s = f.read()
    found = s.count(old)
    assert found == count, (path, old[:60], found)
    s = s.replace(old, new)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print("patched", path)


api = BASE + "src/lib/agentteams-api.ts"
dialog = BASE + "src/components/dashboard/sections/workers/worker-create-dialog.tsx"
section = BASE + "src/components/dashboard/sections/workers-section.tsx"

# 1. types
patch(
    api,
    "export type WorkerRuntime = 'openclaw' | 'copaw' | 'hermes' | 'openhuman' | 'qwenpaw';",
    "export type WorkerRuntime = 'openclaw' | 'copaw' | 'hermes' | 'openhuman' | 'qwenpaw' | 'opencode';",
)
patch(
    api,
    "  model?: RequestModelAlias;\n  runtime: WorkerRuntime;",
    "  model?: RequestModelAlias;\n  runtime?: WorkerRuntime;",
)

# 2. dialog: OpenCode item + bridge image prefill
patch(
    dialog,
    '                <SelectItem value="qwenpaw">QwenPaw</SelectItem>',
    '                <SelectItem value="qwenpaw">QwenPaw</SelectItem>\n'
    '                <SelectItem value="opencode">OpenCode</SelectItem>',
)
patch(
    dialog,
    "              onValueChange={(v) => onChange({ ...value, runtime: v as CreateWorkerRequest['runtime'] })}",
    "              onValueChange={(v) =>\n"
    "                onChange({\n"
    "                  ...value,\n"
    "                  runtime: v as CreateWorkerRequest['runtime'],\n"
    "                  image: v === 'opencode' && !value.image ? 'agentteams/cimicode-bridge:v0.0.9-oct' : value.image,\n"
    "                })\n"
    "              }",
)

# 3. submit: opencode -> omit runtime
patch(
    section,
    "    createWorker.mutate(newWorker, {",
    "    const createReq: CreateWorkerRequest = { ...newWorker };\n"
    "    if (createReq.runtime === 'opencode') {\n"
    "      // OpenCode is not in the cluster Worker CRD runtime enum yet; submit\n"
    "      // with runtime omitted so the controller applies its configured\n"
    "      // default (AGENTTEAMS_DEFAULT_WORKER_RUNTIME=opencode).\n"
    "      delete createReq.runtime;\n"
    "    }\n"
    "    createWorker.mutate(createReq, {",
)

print("all patches applied")
