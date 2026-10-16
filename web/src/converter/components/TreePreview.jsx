import { memo } from "react";
import { CheckCircle2, Circle, FileText, Folder, GitBranch, ListChecks } from "lucide-react";

const NODE_META = {
  root: { label: "主节点", Icon: GitBranch },
  directory: { label: "目录", Icon: Folder },
  case: { label: "用例", Icon: FileText },
  step: { label: "步骤", Icon: ListChecks },
  expected: { label: "预期", Icon: CheckCircle2 },
  field: { label: "字段", Icon: Circle },
  topic: { label: "节点", Icon: Circle },
};

function TreeNode({ node, depth, forceOpen }) {
  const children = node.children || [];
  const meta = NODE_META[node.node_type] || NODE_META.topic;
  const content = (
    <>
      <span className={`tree-node-icon is-${node.node_type || "topic"}`}><meta.Icon size={14} /></span>
      <span className="tree-node-title">{node.title || "未命名节点"}</span>
      {(node.markers || []).map((marker) => <span className="tree-marker" key={marker}>{marker.replace("priority-", "P")}</span>)}
      <small className="tree-node-type">{meta.label}</small>
    </>
  );

  return (
    <li className={`tree-node depth-${Math.min(depth, 4)}`}>
      {children.length ? (
        <details open={forceOpen || depth < 2}>
          <summary>{content}</summary>
          {node.notes ? <div className="tree-note"><strong>前置条件</strong><span>{node.notes}</span></div> : null}
          <ul>{children.map((child, index) => <TreeNode node={child} depth={depth + 1} forceOpen={forceOpen} key={`${child.title}-${index}`} />)}</ul>
        </details>
      ) : (
        <div className="tree-leaf">{content}</div>
      )}
      {!children.length && node.notes ? <div className="tree-note"><strong>前置条件</strong><span>{node.notes}</span></div> : null}
    </li>
  );
}

function TreePreview({ tree, forceOpen = false, emptyText = "没有可显示的节点" }) {
  if (!tree) return <div className="table-empty">{emptyText}</div>;
  return (
    <div className="tree-scroll">
      <ul className="tree-root"><TreeNode node={tree} depth={0} forceOpen={forceOpen} /></ul>
    </div>
  );
}

export default memo(TreePreview);
