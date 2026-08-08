#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 CodeGraph 的 SQLite 索引中导出全部调用链图谱。

用法:  python export_callgraph.py [--out callgraph_export] [--db .codegraph/codegraph.db]

产物:
  callgraph_full.json    —— 全部可调用节点 + calls 边（机器可读）
  callgraph_full.graphml —— 标准图格式，可导入 Gephi / yEd
  callgraph_full.mmd     —— 全量调用图（Mermaid，可在 GitHub/VS Code/mermaid.live 渲染）
  files/*.mmd            —— 按文件拆分的调用链图（含跨文件目标节点）
  entry/*.mmd            —— 以 main.py 各入口方法为根展开的调用树
  callgraph_INDEX.md     —— 总览与查看指引
"""
import argparse
import json
import os
import sqlite3
import xml.sax.saxutils as sax

CALL_KINDS = {"calls"}


def short_name(qn: str) -> str:
    return qn.split("::")[-1].split(".")[-1]


def load_graph(db_path: str):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    nodes = {}
    for nid, kind, name, qn, fpath, lang, sl, el in cur.execute(
        "SELECT id, kind, name, qualified_name, file_path, language, start_line, end_line "
        "FROM nodes"
    ):
        nodes[nid] = {
            "id": nid, "kind": kind, "name": name, "qualified_name": qn,
            "file_path": fpath, "language": lang, "start_line": sl, "end_line": el,
        }
    edges = []
    for eid, src, tgt, kind, line, col in cur.execute(
        "SELECT id, source, target, kind, line, col FROM edges"
    ):
        if kind not in CALL_KINDS:
            continue
        edges.append({"id": eid, "source": src, "target": tgt, "kind": kind,
                      "line": line, "col": col})
    con.close()
    return nodes, edges


def build_callable_index(nodes):
    """仅保留函数/方法节点，作为调用图顶点。"""
    callable_ids = set()
    for n in nodes.values():
        if n["kind"] in ("function", "method"):
            callable_ids.add(n["id"])
    return callable_ids


def escape_mermaid_label(s: str) -> str:
    return sax.escape(str(s), {"\"": "#quot;", "<": "&lt;", ">": "&gt;"})


def make_mermaid_node_id(idx: int) -> str:
    return f"m{idx}"


def write_full_json(nodes, edges, callable_ids, out):
    call_nodes = [n for n in nodes.values() if n["id"] in callable_ids]
    call_edges = []
    for e in edges:
        if e["source"] in callable_ids and e["target"] in callable_ids:
            call_edges.append({
                "source": nodes[e["source"]]["qualified_name"],
                "source_file": nodes[e["source"]]["file_path"],
                "source_line": nodes[e["source"]]["start_line"],
                "target": nodes[e["target"]]["qualified_name"],
                "target_file": nodes[e["target"]]["file_path"],
                "target_line": nodes[e["target"]]["start_line"],
                "line": e["line"],
            })
    call_edges.sort(key=lambda x: (x["source_file"], x["source_line"], x["line"]))
    payload = {
        "generated_by": "export_callgraph.py",
        "source": "CodeGraph SQLite index",
        "node_count": len(call_nodes),
        "edge_count": len(call_edges),
        "nodes": call_nodes,
        "calls": call_edges,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return len(call_nodes), len(call_edges)


def write_graphml(nodes, edges, callable_ids, out):
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
                 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')
    lines.append('  <key id="k_name" for="node" attr.name="name" attr.type="string"/>')
    lines.append('  <key id="k_qn" for="node" attr.name="qualified_name" attr.type="string"/>')
    lines.append('  <key id="k_kind" for="node" attr.name="kind" attr.type="string"/>')
    lines.append('  <key id="k_file" for="node" attr.name="file" attr.type="string"/>')
    lines.append('  <key id="k_line" for="node" attr.name="line" attr.type="int"/>')
    lines.append('  <key id="k_lang" for="node" attr.name="language" attr.type="string"/>')
    lines.append('  <key id="k_line" for="edge" attr.name="line" attr.type="int"/>')
    lines.append('  <graph id="callgraph" edgedefault="directed">')
    for nid in sorted(callable_ids):
        n = nodes[nid]
        lines.append(f'    <node id="{sax.escape(nid)}">')
        lines.append(f'      <data key="k_name">{sax.escape(n["name"])}</data>')
        lines.append(f'      <data key="k_qn">{sax.escape(n["qualified_name"])}</data>')
        lines.append(f'      <data key="k_kind">{sax.escape(n["kind"])}</data>')
        lines.append(f'      <data key="k_file">{sax.escape(n["file_path"])}</data>')
        lines.append(f'      <data key="k_line">{n["start_line"]}</data>')
        lines.append(f'      <data key="k_lang">{sax.escape(n["language"])}</data>')
        lines.append('    </node>')
    for e in edges:
        if e["source"] in callable_ids and e["target"] in callable_ids:
            lines.append(f'    <edge source="{sax.escape(e["source"])}" target="{sax.escape(e["target"])}">')
            lines.append(f'      <data key="k_line">{e["line"]}</data>')
            lines.append('    </edge>')
    lines.append('  </graph>')
    lines.append('</graphml>')
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_full_mermaid(nodes, edges, callable_ids, out):
    ids = {nid: make_mermaid_node_id(i) for i, nid in enumerate(sorted(callable_ids))}
    with open(out, "w", encoding="utf-8") as f:
        f.write("flowchart TD\n")
        f.write('  %% 全量调用图 — 由 export_callgraph.py 生成\n')
        for i, nid in enumerate(sorted(callable_ids)):
            n = nodes[nid]
            label = n["qualified_name"]
            f.write(f'  {ids[nid]}["{escape_mermaid_label(label)}"]\n')
        for e in edges:
            if e["source"] in ids and e["target"] in ids:
                f.write(f'  {ids[e["source"]]} --> {ids[e["target"]]}\n')


def write_per_file_mermaid(nodes, edges, callable_ids, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    by_file = {}
    for e in edges:
        if e["source"] not in callable_ids or e["target"] not in callable_ids:
            continue
        src_f = nodes[e["source"]]["file_path"]
        by_file.setdefault(src_f, []).append(e)

    index_rows = []
    for fpath in sorted(by_file):
        fedges = by_file[fpath]
        local_ids = {nid for e in fedges for nid in (e["source"], e["target"])
                     if nodes[nid]["file_path"] == fpath}
        external_ids = {nid for e in fedges for nid in (e["source"], e["target"])
                        if nodes[nid]["file_path"] != fpath}
        ids = {}
        for nid in sorted(local_ids | external_ids):
            ids[nid] = make_mermaid_node_id(len(ids))
        name = fpath.replace("/", "_").replace("\\", "_")
        out = os.path.join(out_dir, name + ".mmd")
        with open(out, "w", encoding="utf-8") as f:
            f.write("flowchart TD\n")
            f.write(f'  %% 调用链图: {fpath}  (由 export_callgraph.py 生成)\n')
            for nid in sorted(local_ids, key=lambda x: nodes[x]["start_line"]):
                n = nodes[nid]
                label = f'{n["qualified_name"]}<br/>L{n["start_line"]}'
                f.write(f'  {ids[nid]}["{escape_mermaid_label(label)}"]\n')
            for nid in sorted(external_ids, key=lambda x: nodes[x]["qualified_name"]):
                n = nodes[nid]
                label = f'{n["qualified_name"]}<br/>{n["file_path"]}:L{n["start_line"]}'
                f.write(f'  {ids[nid]}["{escape_mermaid_label(label)}"]:::ext\n')
            if external_ids:
                f.write('  classDef ext fill:#eee,stroke:#999,color:#666,stroke-dasharray:4 3;\n')
            for e in fedges:
                style = "-->" if nodes[e["target"]]["file_path"] == fpath else "-.->"
                f.write(f'  {ids[e["source"]]} {style} {ids[e["target"]]}\n')
        index_rows.append((fpath, len(local_ids), len(external_ids), len(fedges), out))
    return index_rows


def write_entry_trees(nodes, edges, callable_ids, roots, out_dir, depth=14):
    os.makedirs(out_dir, exist_ok=True)
    adj = {}
    for e in edges:
        if e["source"] in callable_ids and e["target"] in callable_ids:
            adj.setdefault(e["source"], []).append(e["target"])

    results = []
    for root in roots:
        root_id = root["id"]
        ids = {root_id: "r0"}
        seq = [1]
        visited_edges = set()

        def add_child(parent, child):
            if child not in ids:
                ids[child] = f"r{seq[0]}"
                seq[0] += 1

        def walk(nid, remaining):
            if remaining <= 0:
                return
            for tgt in adj.get(nid, []):
                if (nid, tgt) in visited_edges:
                    continue
                visited_edges.add((nid, tgt))
                add_child(nid, tgt)
                walk(tgt, remaining - 1)

        walk(root_id, depth)
        name = short_name(root["qualified_name"])
        safe = name.replace("/", "_").replace("\\", "_").replace(" ", "_")
        out = os.path.join(out_dir, f"{safe}.mmd")
        with open(out, "w", encoding="utf-8") as f:
            f.write("flowchart TD\n")
            f.write(f'  %% 调用树根: {root["qualified_name"]}  (由 export_callgraph.py 生成)\n')
            for nid, mid in ids.items():
                n = nodes[nid]
                label = f'{n["qualified_name"]}<br/>{n["file_path"]}:L{n["start_line"]}'
                f.write(f'  {mid}["{escape_mermaid_label(label)}"]\n')
            for (src, tgt) in visited_edges:
                f.write(f'  {ids[src]} --> {ids[tgt]}\n')
        results.append((root["qualified_name"], len(ids) - 1, len(visited_edges), out))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(".codegraph", "codegraph.db"))
    ap.add_argument("--out", default="callgraph_export")
    args = ap.parse_args()

    out_root = args.out
    files_dir = os.path.join(out_root, "files")
    entry_dir = os.path.join(out_root, "entry")
    os.makedirs(out_root, exist_ok=True)

    nodes, edges = load_graph(args.db)
    callable_ids = build_callable_index(nodes)

    # 入口点: main.py 的全部方法 + 顶层函数
    roots = [n for n in nodes.values()
             if n["id"] in callable_ids and n["file_path"] == "main.py"]
    roots.sort(key=lambda x: (x["kind"], x["start_line"]))

    full_json = os.path.join(out_root, "callgraph_full.json")
    fn, fe = write_full_json(nodes, edges, callable_ids, full_json)
    write_graphml(nodes, edges, callable_ids, os.path.join(out_root, "callgraph_full.graphml"))
    write_full_mermaid(nodes, edges, callable_ids, os.path.join(out_root, "callgraph_full.mmd"))
    file_rows = write_per_file_mermaid(nodes, edges, callable_ids, files_dir)
    entry_rows = write_entry_trees(nodes, edges, callable_ids, roots, entry_dir)

    # INDEX
    idx = os.path.join(out_root, "callgraph_INDEX.md")
    with open(idx, "w", encoding="utf-8") as f:
        f.write("# CodeGraph 调用链图谱导出\n\n")
        f.write(f"> 来源: `{args.db}` · 导出时间: 见文件生成时间 · 生成脚本: `export_callgraph.py`\n\n")
        f.write("## 统计\n\n")
        f.write(f"- 可调用节点（函数/方法）: **{len(callable_ids)}**\n")
        f.write(f"- 调用关系边（calls）: **{len(edges)}**\n")
        f.write(f"- 参与调用关系的文件: **{len(file_rows)}**\n\n")
        f.write("## 文件清单\n\n")
        f.write("| 产物 | 说明 |\n|---|---|\n")
        f.write(f"| [callgraph_full.json](callgraph_full.json) | 全部节点+边（机器可读） |\n")
        f.write(f"| [callgraph_full.graphml](callgraph_full.graphml) | 标准图格式，导入 Gephi/yEd |\n")
        f.write(f"| [callgraph_full.mmd](callgraph_full.mmd) | 全量调用图（Mermaid） |\n")
        f.write(f"| [files/](files/) | 按文件拆分的调用链图 |\n")
        f.write(f"| [entry/](entry/) | main.py 各入口方法调用树 |\n\n")
        f.write("## 按文件调用链图\n\n")
        f.write("| 文件 | 内部节点 | 外部节点 | 调用边 | 图 |\n|---|---|---|---|---|\n")
        for fpath, ln, exn, fec, outp in file_rows:
            rel = os.path.relpath(outp, out_root).replace("\\", "/")
            f.write(f"| `{fpath}` | {ln} | {exn} | {fec} | [查看]({rel}) |\n")
        f.write("\n## main.py 入口调用树\n\n")
        f.write("| 入口方法 | 展开节点 | 调用边 | 图 |\n|---|---|---|---|\n")
        for qn, nn, ne, outp in entry_rows:
            rel = os.path.relpath(outp, out_root).replace("\\", "/")
            f.write(f"| `{qn}` | {nn} | {ne} | [查看]({rel}) |\n")
        f.write("\n## 查看方式\n\n")
        f.write("- **Mermaid (.mmd)**：GitHub 直接渲染；VS Code 装 Mermaid 插件；或粘贴到 https://mermaid.live\n")
        f.write("- **GraphML**：Gephi / yEd 打开可交互探索全图\n")
        f.write("- **JSON**：任意脚本处理\n")

    print(f"callable nodes: {len(callable_ids)}")
    print(f"call edges: {len(edges)}")
    print(f"full JSON: {fn} nodes / {fe} edges -> {full_json}")
    print(f"per-file diagrams: {len(file_rows)} -> {files_dir}")
    print(f"entry trees: {len(entry_rows)} -> {entry_dir}")
    print(f"INDEX: {idx}")


if __name__ == "__main__":
    main()
