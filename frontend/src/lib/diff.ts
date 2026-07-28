/** unified diff 解析:拆成逐文件 + 行级结构,供 diff 视图渲染。 */

export interface DiffLine {
  type: "add" | "del" | "context" | "hunk" | "meta";
  text: string;
  oldNo?: number;
  newNo?: number;
}

export interface FileDiff {
  path: string;
  kind: "add" | "delete" | "update" | "rename";
  lines: DiffLine[];
  adds: number;
  dels: number;
}

/** 解析 unified diff 文本为逐文件结构。 */
export function parseDiff(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  let cur: FileDiff | null = null;
  let oldNo = 0, newNo = 0;

  const push = (l: DiffLine) => { if (cur) cur.lines.push(l); };

  for (const raw of diff.split("\n")) {
    if (raw.startsWith("diff --git") || raw.startsWith("*** ")) {
      // 新文件边界
      if (cur) files.push(cur);
      cur = { path: "", kind: "update", lines: [], adds: 0, dels: 0 };
      continue;
    }
    if (!cur) continue;

    if (raw.startsWith("--- ")) {
      const p = raw.slice(4).replace(/^a\//, "").trim();
      if (p !== "/dev/null") cur.path = p;
      cur.lines.push({ type: "meta", text: raw });
      continue;
    }
    if (raw.startsWith("+++ ")) {
      const p = raw.slice(4).replace(/^b\//, "").trim();
      if (p !== "/dev/null") cur.path = p;
      if (!cur.path) cur.path = p;
      cur.lines.push({ type: "meta", text: raw });
      continue;
    }
    if (raw.startsWith("@@")) {
      const m = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) { oldNo = parseInt(m[1]); newNo = parseInt(m[2]); }
      cur.lines.push({ type: "hunk", text: raw });
      continue;
    }
    if (raw.startsWith("new file")) { cur.kind = "add"; cur.lines.push({ type: "meta", text: raw }); continue; }
    if (raw.startsWith("deleted file")) { cur.kind = "delete"; cur.lines.push({ type: "meta", text: raw }); continue; }
    if (raw.startsWith("rename ")) { cur.kind = "rename"; cur.lines.push({ type: "meta", text: raw }); continue; }
    if (raw.startsWith("index ") || raw.startsWith("Binary")) { cur.lines.push({ type: "meta", text: raw }); continue; }

    if (raw.startsWith("+")) {
      cur.adds++; push({ type: "add", text: raw.slice(1), newNo: newNo++ });
    } else if (raw.startsWith("-")) {
      cur.dels++; push({ type: "del", text: raw.slice(1), oldNo: oldNo++ });
    } else if (raw.startsWith(" ")) {
      push({ type: "context", text: raw.slice(1), oldNo: oldNo++, newNo: newNo++ });
    }
  }
  if (cur && (cur.lines.length || cur.path)) files.push(cur);
  return files;
}

/** codex apply_patch 语法的 *** Add/Update/Delete File 解析(非标准 git diff)。 */
export function parseCodexPatch(patch: string): FileDiff[] {
  const files: FileDiff[] = [];
  let cur: FileDiff | null = null;
  for (const raw of patch.split("\n")) {
    const add = raw.match(/^\*\*\* Add File: (.+)/);
    const upd = raw.match(/^\*\*\* Update File: (.+)/);
    const del = raw.match(/^\*\*\* Delete File: (.+)/);
    if (add || upd || del) {
      if (cur) files.push(cur);
      const m = add || upd || del!;
      cur = { path: m[1], kind: add ? "add" : upd ? "update" : "delete", lines: [], adds: 0, dels: 0 };
      continue;
    }
    if (!cur || raw.startsWith("***")) continue;
    if (raw.startsWith("@@")) { cur.lines.push({ type: "hunk", text: raw }); continue; }
    if (raw.startsWith("+")) { cur.adds++; cur.lines.push({ type: "add", text: raw.slice(1) }); }
    else if (raw.startsWith("-")) { cur.dels++; cur.lines.push({ type: "del", text: raw.slice(1) }); }
    else if (raw.startsWith(" ")) cur.lines.push({ type: "context", text: raw.slice(1) });
  }
  if (cur) files.push(cur);
  return files;
}

/** 自动识别格式并解析。 */
export function parseAnyDiff(text: string): FileDiff[] {
  if (text.includes("*** Begin Patch") || text.includes("*** Add File") || text.includes("*** Update File")) {
    return parseCodexPatch(text);
  }
  return parseDiff(text);
}
