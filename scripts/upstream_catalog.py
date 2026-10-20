#!/usr/bin/env python3
"""Extract an immutable upstream inventory from local, pinned official source snapshots; never execute them."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re


def extract_ceph(source,path,revision):
    """Keep each test identity, body fingerprint, assertion count and parameter decorators for later audited migration."""
    tree=ast.parse(source)
    rows=[]
    def visit(nodes,scope=""):
        for node in nodes:
            if isinstance(node,ast.ClassDef):visit(node.body,scope+node.name+".")
            elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                segment=ast.get_source_segment(source,node)
                rows.append({"id":path+"::"+scope+node.name+f"@L{node.lineno}","qualified_name":scope+node.name,"name":node.name,"line":node.lineno,
                    "sha256":hashlib.sha256(segment.encode()).hexdigest(),
                    "assertions":sum(isinstance(item,ast.Assert) for item in ast.walk(node)),
                    "markers":[ast.unparse(item) for item in node.decorator_list],
                    "parameters":[ast.unparse(item) for item in node.decorator_list if isinstance(item,ast.Call) and "parametrize" in ast.unparse(item.func)],
                    "source":f"https://github.com/ceph/s3-tests/blob/{revision}/{path}#L{node.lineno}"})
    visit(tree.body)
    last={row["qualified_name"]:row["id"] for row in rows}
    for row in rows: row["active_definition"]=last[row["qualified_name"]]==row["id"]
    return rows


def extract_juice(source,revision):
    rows=[]
    for match in re.finditer(r'(?:runCase|funFSCase)\("([^\"]+)"',source):
        rows.append({"id":"functional/"+match.group(1),"name":match.group(1),"kind":"functional",
                     "line":source[:match.start()].count("\n")+1})
    block=source[source.index("apis := []apiInfo{"):source.index("for _, api := range apis")]
    for name in re.findall(r'name:\s*"([^\"]+)"',block):
        rows.append({"id":"performance/"+name,"name":name,"kind":"performance"})
    for row in rows:row["source"]=f"https://github.com/juicedata/juicefs/blob/{revision}/cmd/objbench.go"+(f"#L{row['line']}" if "line" in row else "")
    return rows


def generate(directory):
    directory=Path(directory)
    juice=json.loads((directory/"juicefs.json").read_text())
    ceph=json.loads((directory/"ceph.json").read_text())
    juice["cases"]=extract_juice((directory/"juicefs.source").read_text(),juice["revision"])
    ceph["files"]=[];ceph["cases"]=[]
    for path in sorted((directory/"s3tests/functional").glob("test_*.py")):
        relative=str(path.relative_to(directory));source=path.read_text()
        ceph["files"].append({"path":relative,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
        ceph["cases"].extend(extract_ceph(source,relative,ceph["revision"]))
    return {"schema_version":1,"counting_unit":"upstream test definition including shadowed names; active_definition distinguishes collected functions; not expanded pytest parameter instances",
            "juicefs":juice,"ceph":ceph}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_directory");parser.add_argument("--output",required=True)
    args=parser.parse_args()
    Path(args.output).write_text(json.dumps(generate(args.snapshot_directory),indent=2,ensure_ascii=False)+"\n")


if __name__=="__main__":main()
