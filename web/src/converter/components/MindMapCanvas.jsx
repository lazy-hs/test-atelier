import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
} from "@xyflow/react";
import { CheckCircle2, FileText, Folder, GitBranch, Info, ListChecks } from "lucide-react";
import { useMemo } from "react";

const NODE_WIDTHS = {
  root: 238,
  directory: 176,
  case: 330,
  step: 270,
  expected: 300,
  topic: 220,
};

const NODE_HEIGHTS = {
  root: 76,
  directory: 52,
  case: 68,
  step: 52,
  expected: 58,
  topic: 52,
};

const NODE_GAP = 18;
const COLUMN_GAP = 76;
const META = {
  root: { label: "主节点", Icon: GitBranch },
  directory: { label: "目录", Icon: Folder },
  case: { label: "用例", Icon: FileText },
  step: { label: "步骤", Icon: ListChecks },
  expected: { label: "预期", Icon: CheckCircle2 },
  topic: { label: "节点", Icon: Folder },
};

function nodeType(node) {
  return META[node?.node_type] ? node.node_type : "topic";
}

function nodeHeight(node) {
  const kind = nodeType(node);
  const titleLength = String(node?.title || "").length;
  const lines = Math.max(1, Math.ceil(titleLength / (kind === "case" ? 24 : 20)));
  return NODE_HEIGHTS[kind] + Math.max(0, lines - 1) * 16;
}

function nodeWidth(node) {
  return NODE_WIDTHS[nodeType(node)];
}

function createLayout(tree) {
  const nodes = [];
  const edges = [];
  const depths = [];
  const subtreeHeights = new Map();

  function measure(node, id, depth) {
    const children = node.children || [];
    const childrenHeight = children.reduce(
      (total, child, index) => total + measure(child, `${id}-${index}`, depth + 1) + (index ? NODE_GAP : 0),
      0,
    );
    const height = nodeHeight(node);
    const subtreeHeight = Math.max(height, childrenHeight);
    subtreeHeights.set(id, subtreeHeight);
    depths[depth] = Math.max(depths[depth] || 0, nodeWidth(node));
    return subtreeHeight;
  }

  function place(node, id, depth, top, parentId = null) {
    const kind = nodeType(node);
    const height = nodeHeight(node);
    const subtreeHeight = subtreeHeights.get(id) || height;
    const x = depths.slice(0, depth).reduce((total, width) => total + width + COLUMN_GAP, 42);
    const y = top + (subtreeHeight - height) / 2;
    const meta = META[kind];

    nodes.push({
      id,
      type: "mindMap",
      position: { x, y },
      draggable: false,
      selectable: false,
      style: { width: nodeWidth(node), height },
      data: {
        title: node.title || "未命名节点",
        kind,
        label: meta.label,
        Icon: meta.Icon,
        notes: node.notes || "",
        markers: node.markers || [],
      },
    });

    if (parentId) {
      edges.push({
        id: `${parentId}->${id}`,
        source: parentId,
        target: id,
        type: "smoothstep",
        pathOptions: { borderRadius: 12, offset: 20 },
      });
    }

    const children = node.children || [];
    const childrenHeight = children.reduce(
      (total, child, index) => total + (subtreeHeights.get(`${id}-${index}`) || nodeHeight(child)) + (index ? NODE_GAP : 0),
      0,
    );
    let childTop = top + (subtreeHeight - childrenHeight) / 2;
    children.forEach((child, index) => {
      const childId = `${id}-${index}`;
      place(child, childId, depth + 1, childTop, id);
      childTop += (subtreeHeights.get(childId) || nodeHeight(child)) + NODE_GAP;
    });
  }

  if (!tree) return { nodes: [], edges: [] };
  measure(tree, "mind-root", 0);
  place(tree, "mind-root", 0, 0);
  return { nodes, edges };
}

function MindMapNode({ data }) {
  const Icon = data.Icon;
  const isRoot = data.kind === "root";
  const isLeaf = data.kind === "expected";
  return (
    <div className={`mind-map-node is-${data.kind}`}>
      {!isRoot ? <Handle type="target" position={Position.Left} isConnectable={false} /> : null}
      <div className="mind-map-node-main">
        <span className="mind-map-icon"><Icon size={16} /></span>
        <span className="mind-map-copy">
          <strong>{data.title}</strong>
          {data.notes ? <span className="mind-map-note"><Info size={12} />{data.notes}</span> : null}
        </span>
        {data.markers.map((marker) => (
          <span className="mind-map-marker" key={marker}>{marker.replace("priority-", "P")}</span>
        ))}
        {!isRoot ? <small>{data.label}</small> : null}
      </div>
      {!isLeaf ? <Handle type="source" position={Position.Right} isConnectable={false} /> : null}
    </div>
  );
}

const nodeTypes = { mindMap: MindMapNode };

export default function MindMapCanvas({ tree, emptyText = "没有可显示的节点" }) {
  const { nodes, edges } = useMemo(() => createLayout(tree), [tree]);

  if (!tree) return <div className="table-empty">{emptyText}</div>;

  return (
    <div className="mind-map-canvas" aria-label="XMind 结构画布">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.04, minZoom: 0.25, maxZoom: 1.2 }}
        minZoom={0.12}
        maxZoom={1.6}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: false }}
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#e9edf5" />
        <Controls showInteractive={false} position="bottom-left" />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => {
            if (node.data?.kind === "root") return "#3947a9";
            if (node.data?.kind === "case") return "#6465da";
            if (node.data?.kind === "expected") return "#159b69";
            return "#cfd5e2";
          }}
          maskColor="rgba(244, 246, 251, 0.76)"
          position="bottom-right"
        />
        <Panel position="top-right" className="mind-map-hint">
          拖拽移动 · 滚轮缩放 · 点击左下角适配
        </Panel>
      </ReactFlow>
    </div>
  );
}
